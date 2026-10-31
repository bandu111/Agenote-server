"""临时说话人上下文只能由供应商生成，并严格限制在当前账号的当前会议。"""
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, parse_qs
from test_meetings import account
from test_tencent_asr import configuration
from app.modules.meetings.services.tencent_asr import signed_url
from app.modules.meetings.services.speaker_context import SpeakerContextStore
from app.modules.meetings.models.speaker_context import SpeakerContext
from app.modules.identity.models.identity import User
from sqlalchemy import select


def test_context_is_signed_only_for_speaker_engine():
    args = parse_qs(urlsplit(signed_url(configuration(tencent_asr_engine_model_type="16k_zh_en_speaker_2.0"), "provider-context")).query)
    assert args["enable_speaker_context"] == ["1"]
    assert args["speaker_context_id"] == ["provider-context"]
    plain = parse_qs(urlsplit(signed_url(configuration(), "ignored")).query)
    assert "speaker_context_id" not in plain and "enable_speaker_context" not in plain


async def test_context_survives_worker_restart_but_not_owner_meeting_or_expiry(client, app):
    await account(client)
    async with app.state.database.sessions() as db:
        owner = (await db.scalar(select(User))).id
    store = SpeakerContextStore(app.state.database, owner, "meeting-a")
    assert await store.load() is None
    await store.save("a" * 32, {"speaker_context_id": "opaque-provider-context"})
    restarted = SpeakerContextStore(app.state.database, owner, "meeting-a")
    assert await restarted.load() == ("a" * 32, "opaque-provider-context")
    assert await SpeakerContextStore(app.state.database, "different-owner", "meeting-a").load() is None
    assert await SpeakerContextStore(app.state.database, owner, "meeting-b").load() is None
    async with app.state.database.sessions() as db:
        row = await db.get(SpeakerContext, store.key)
        expiry = row.expires_at
    await store.save("a" * 32, {"speaker_context_id": "opaque-provider-context"})
    async with app.state.database.sessions() as db:
        row = await db.get(SpeakerContext, store.key)
        assert row.expires_at == expiry
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
    assert await restarted.load() is None


async def test_invalid_or_absent_context_does_not_invent_continuity(client, app):
    await account(client)
    async with app.state.database.sessions() as db:
        owner = (await db.scalar(select(User))).id
    store = SpeakerContextStore(app.state.database, owner, "meeting-c")
    for payload in [{}, {"speaker_context_id": []}, {"speaker_context_id": "unsafe?query"}]:
        await store.save("b" * 32, payload)
        assert await store.load() is None
    await store.save("b" * 32, {"sentences": {"speaker_context_id": "valid-id"}})
    await store.clear()
    assert await store.load() is None
