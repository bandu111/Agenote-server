"""协议测试不依赖真实付费识别，覆盖证据边界和签名。"""
import base64
import hashlib
import hmac
from urllib.parse import urlsplit, parse_qs
import pytest
from app.core.config import Settings
from app.modules.meetings.services.tencent_asr import signed_url, transcript_events, AsrProtocolError


def configuration(**kwargs):
    return Settings(_env_file=None, database_url="postgresql+psycopg://test@localhost/test",
                    jwt_secret="test-asr-signature-key-01234567890123456789",
                    tencent_asr_app_id="1234567890", tencent_asr_secret_id="test-id",
                    tencent_asr_secret_key="test-key", **kwargs)


def test_signature_encoding_and_unique_connection():
    url = urlsplit(signed_url(configuration()))
    args = {k: v[0] for k, v in parse_qs(url.query).items()}
    signature = args.pop("signature")
    raw = url.netloc + url.path + "?" + "&".join(f"{k}={v}" for k, v in sorted(args.items()))
    expected = base64.b64encode(hmac.new(b"test-key", raw.encode(), hashlib.sha1).digest()).decode()
    assert signature == expected
    assert args["engine_model_type"] == "16k_zh_en_2.0"
    assert args["voice_format"] == "1"
    assert args["filter_modal"] == "0"
    assert int(args["expired"]) - int(args["timestamp"]) == 60
    assert parse_qs(urlsplit(signed_url(configuration())).query)["voice_id"][0] != args["voice_id"]


def test_speaker_engine_explicitly_enables_official_sentence_and_diarization_modes():
    args = parse_qs(urlsplit(signed_url(configuration(
        tencent_asr_engine_model_type='16k_zh_en_speaker_2.0'))).query)
    for key in ['result_mod', 'speaker_diarization', 'enable_speaker_context']:
        assert args[key] == ['1']
    assert args['sentence_strategy'] == ['0']
    # 明确参数必须参与签名，不能在签名后拼接导致鉴权失败。
    flattened = {k: v[0] for k, v in args.items()}
    signature = flattened.pop('signature')
    raw = 'asr.cloud.tencent.com/asr/v2/1234567890?' + '&'.join(f'{k}={v}' for k, v in sorted(flattened.items()))
    expected = base64.b64encode(hmac.new(b'test-key', raw.encode(), hashlib.sha1).digest()).decode()
    assert signature == expected
    plain = parse_qs(urlsplit(signed_url(configuration())).query)
    assert not {'speaker_diarization', 'result_mod', 'sentence_strategy'}.intersection(plain)


@pytest.mark.parametrize("shape", ["list", "nested", "single"])
def test_partial_final_and_original_negation(shape):
    row = dict(sentence="考虑改到周五，但还没确定，不要取消原计划", sentence_id=3,
               sentence_type=0, start_time=1000, end_time=4000)
    def payload():
        rows = [row] if shape == "list" else ({"sentence_list": [row]} if shape == "nested" else row)
        return {"code": 0, "sentences": rows}
    result = transcript_events(payload())[0]
    assert result["is_final"] is False
    assert result["text"] == row["sentence"]
    assert result["start_ms"] == 1000
    row["sentence_type"] = 1
    assert transcript_events(payload())[0]["is_final"] is True


@pytest.mark.parametrize("text_key", ["voice_text", "voice_text_str"])
def test_classic_plain_engine_response(text_key):
    event = transcript_events({"code": 0, "result": {text_key: "未批准", "index": 2,
                               "slice_type": 2, "start_time": 10, "end_time": 20}})[0]
    assert event["text"] == "未批准" and event["is_final"]


@pytest.mark.parametrize("payload", [
    {"code": 4004, "message": "do not expose signed URL"},
    {"sentences": "unexpected"},
    {"sentences": [{"sentence": "原话", "sentence_id": 0, "start_time": 20, "end_time": 10}]},
])
def test_invalid_response_never_becomes_evidence(payload):
    with pytest.raises(AsrProtocolError):
        transcript_events(payload)


def test_empty_handshake_and_final_have_no_transcript():
    assert transcript_events({"code": 0}) == []
    assert transcript_events({"code": 0, "final": 1}) == []


async def test_asr_capabilities_requires_authenticated_account(client):
    assert (await client.get("/api/v1/meetings/asr/capabilities")).status_code == 401
    response = await client.post("/api/v1/auth/register", json={"name": "语音协议测试", "email": "asr-test@example.com", "password": "Synthetic-Asr-Test-2026!"})
    token = response.json()["data"]["access_token"]
    result = await client.get("/api/v1/meetings/asr/capabilities", headers={"Authorization": "Bearer " + token})
    assert result.status_code == 200
    assert result.json()["data"] == {"protocol": 1, "provider": "tencent", "sample_rate": 16000}
    assert "secret" not in result.text.lower()


async def test_stream_lock_is_per_account_and_released_on_failure(app):
    from app.modules.meetings.services.asr_stream_service import owner_stream_lock
    async with owner_stream_lock(app.state.database, "owner-a"):
        with pytest.raises(AsrProtocolError, match="asr_account_busy"):
            async with owner_stream_lock(app.state.database, "owner-a"):
                pytest.fail("同一账户不应重复占用")
        async with owner_stream_lock(app.state.database, "owner-b"):
            pass
    with pytest.raises(RuntimeError):
        async with owner_stream_lock(app.state.database, "owner-a"):
            raise RuntimeError("模拟供应商断线")
    async with owner_stream_lock(app.state.database, "owner-a"):
        pass


@pytest.mark.parametrize("speaker,expected", [(-1,None),(0,0),(2,2),(99,None)])
def test_anonymous_speaker_protocol_keeps_unknown_unknown(speaker,expected):
    event=transcript_events({"code":0,"sentences":{"sentence_list":[{"sentence":"验收稍后确认","sentence_id":1,"sentence_type":1,"speaker_id":speaker,"start_time":0,"end_time":1000}]}})[0]
    assert event["speaker_id"] == expected


async def test_speaker_correction_is_versioned_and_raw_immutable(client):
    from tests.test_meetings import account,meeting
    from copy import deepcopy
    headers=await account(client)
    value=meeting()
    value["utterances"][0].update(speaker="待区分发言人",speakerId="unseparated",speakerSeparated=False,
        speakerRevision=0,speakerHistory=[{"revision":0,"speaker":"待区分发言人","speakerId":"unseparated","speakerSeparated":False}])
    assert (await client.post("/api/v1/meetings/sync",headers=headers,json=value)).status_code == 200
    updated=deepcopy(value); updated["revision"]=2
    u=updated["utterances"][0];u.update(speaker="发言人 A",speakerId="asr:scope:0",speakerSeparated=True,speakerRevision=1)
    u["speakerHistory"].append({"revision":1,"speaker":"发言人 A","speakerId":"asr:scope:0","speakerSeparated":True})
    response=await client.post("/api/v1/meetings/sync",headers=headers,json=updated)
    assert response.status_code == 200
    saved=response.json()["data"]["utterances"][0]
    assert saved["raw"] == value["utterances"][0]["raw"]
    assert len(saved["speaker_history"]) == 2
    for mutation in ["erase_history","change_raw","unversioned"]:
        invalid=deepcopy(updated);invalid["revision"]=3
        row=invalid["utterances"][0]
        if mutation=="erase_history":row["speakerHistory"]=[]
        elif mutation=="change_raw":row["raw"]="篡改原话"
        else:row["speaker"]="另一个人"
        assert (await client.post("/api/v1/meetings/sync",headers=headers,json=invalid)).status_code == 409
