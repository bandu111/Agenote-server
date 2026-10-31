"""腾讯云 V2 协议适配；凭据与签名不得返回客户端或写入日志。"""
import base64
import hashlib
import hmac
import secrets
import time
import uuid
from urllib.parse import urlencode
from app.core.config import Settings


class AsrProtocolError(Exception):
    """供应商协议失败；对外只暴露稳定错误码。"""


def signed_url(settings: Settings, speaker_context=None) -> str:
    if not all((settings.tencent_asr_app_id, settings.tencent_asr_secret_id, settings.tencent_asr_secret_key)):
        raise AsrProtocolError("asr_not_configured")
    if not settings.tencent_asr_app_id.isdigit():
        raise AsrProtocolError("asr_invalid_configuration")
    stamp = int(time.time())
    params = dict(secretid=settings.tencent_asr_secret_id.get_secret_value(),
                  timestamp=stamp, expired=stamp + 60, nonce=secrets.randbelow(9999999999) + 1,
                  engine_model_type=settings.tencent_asr_engine_model_type,
                  voice_id=str(uuid.uuid4()), voice_format=1, needvad=1,
                  filter_modal=0, filter_dirty=0, convert_num_mode=0)
    if settings.tencent_asr_engine_model_type == "16k_zh_en_speaker_2.0":
        # 与官方 SpeakerRecognizer 对齐，显式请求带话者的句子结果，避免依赖默认模式。
        params["result_mod"] = 1
        params["speaker_diarization"] = 1
        # 断点与换人保留细粒度来源，展示合段交给 Flutter，不在供应商层攒成长段。
        params["sentence_strategy"] = 0
        params["enable_speaker_context"] = 1
        if speaker_context:
            params["speaker_context_id"] = speaker_context
    endpoint = f"asr.cloud.tencent.com/asr/v2/{settings.tencent_asr_app_id}"
    # 仅使用管理员明确配置且已上线的资源；不上传用户录音进行自动训练。
    if settings.tencent_asr_hotword_id:
        params['hotword_id'] = settings.tencent_asr_hotword_id
    elif settings.tencent_asr_hotword_list:
        params['hotword_list'] = settings.tencent_asr_hotword_list
    if settings.tencent_asr_customization_id:
        params['customization_id'] = settings.tencent_asr_customization_id
    plain = endpoint + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    params["signature"] = base64.b64encode(hmac.new(
        settings.tencent_asr_secret_key.get_secret_value().encode(), plain.encode(), hashlib.sha1).digest()).decode()
    return "wss://" + endpoint + "?" + urlencode(params)


def transcript_events(payload: dict) -> list[dict]:
    if payload.get("code", 0) != 0:
        raise AsrProtocolError("asr_provider_rejected")
    rows = payload.get("sentences", [])
    if isinstance(rows, dict):
        rows = rows.get("sentence_list", [rows] if "sentence" in rows else [])
    # 无说话人引擎也可能返回经典 result；只接受供应商确定句标记。
    if "result" in payload and not rows:
        r = payload["result"]
        rows = [dict(sentence=r.get("voice_text_str", r.get("voice_text", "")), sentence_id=r.get("index", 0),
                     sentence_type=1 if r.get("slice_type") == 2 else 0,
                     start_time=r.get("start_time", 0), end_time=r.get("end_time", 0))]
    if not isinstance(rows, list):
        raise AsrProtocolError("asr_invalid_response")
    events = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("sentence"), str):
            raise AsrProtocolError("asr_invalid_response")
        if not row["sentence"].strip():
            continue
        start, end = int(row.get("start_time", 0)), int(row.get("end_time", 0))
        if start < 0 or end < start:
            raise AsrProtocolError("asr_invalid_timestamps")
        events.append(dict(type="transcript", segment=int(row["sentence_id"]),
                           text=row["sentence"], is_final=row.get("sentence_type") == 1,
                           start_ms=start, end_ms=end,
                           speaker_id=row.get("speaker_id") if isinstance(row.get("speaker_id"), int) and 0 <= row["speaker_id"] <= 9 else None))
    return events
