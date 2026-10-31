"""Stage C calendar authority, preparation policy, recovery, and delivery contracts."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import hashlib
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import func, select

from app.core.database.mixins import now
from app.modules.actions.calendar_authority import authoritative_action
from app.modules.actions.models import ActionRun
from app.modules.actions.service import digest
from app.modules.identity.models.identity import User
from app.modules.meetings.models.meeting import Meeting
from app.modules.outcomes.meeting_preparation import (
    KIND, enqueue_preparation, prepare_meeting, reconcile_preparations, schedule_policy,
)
from app.modules.outcomes.models import OutcomeRun
from app.modules.outcomes.wechat_jobs import WechatJobs
from app.modules.outcomes.wechat_models import WechatBinding
from app.modules.outcomes.wechat_service import cipher_for
from app.modules.outcomes.weixin_transport import ChannelError
from tests.test_actions import prepared
from tests.test_auth import bearer, register
from tests.test_meetings import account
from tests.test_wechat_binding import transport

DEVICE = "stage-c-test-device-0001"


def receipt_for(action, *, event="native-event-1", calendar="calendar-1", deleted=False, previous=None):
    return {
        "fingerprint": action["fingerprint"], "verified": True,
        "eventId": event, "previousEventId": previous, "calendarId": calendar,
        "deleted": deleted, "title": action["title"], "start": action["start"],
        "end": action["end"], "deviceId": DEVICE,
    }


async def completed_projected(client, app):
    app.state.settings.meeting_preparation_enabled = True
    headers, actions = await prepared(client, app)
    action = actions[0]
    approved = await client.post(
        f"/api/v1/actions/{action['id']}/decision", headers=headers,
        json={"fingerprint": action["fingerprint"], "allow": True, "deviceId": DEVICE,
              "version": action["version"], "mutationId": "approve-stage-c-0001"},
    )
    assert approved.status_code == 200, approved.text
    approved_action = approved.json()["data"]
    completed = await client.post(
        f"/api/v1/actions/{action['id']}/receipt", headers=headers,
        json={**receipt_for(approved_action), "version": approved_action["version"],
              "mutationId": "receipt-stage-c-0001"},
    )
    assert completed.status_code == 200, completed.text
    return headers, completed.json()["data"]


async def stored_authority(app, owner, suffix, start, *, verified=True):
    end = start + timedelta(hours=1)
    start_text, end_text = start.isoformat(), end.isoformat()
    raw = "确认该会议安排，并仅把这句话作为日程来源证据。"
    meeting = Meeting(
        id="stage-c-meeting-" + suffix, owner_id=owner, client_id="stage-c-client-" + suffix,
        revision=1, analysis_revision=1, deleted=False,
        content={"title": "来源会议", "ended": False, "utterances": [
            {"id": "u1", "raw": raw, "at": 1, "turn_status": "complete"}
        ]}, analysis={"source_hashes": {}},
    )
    fingerprint = hashlib.sha256((suffix + start_text).encode()).hexdigest()
    content = {
        "operation": "create", "title": "Stage C 会议 " + suffix,
        "start": start_text, "end": end_text, "timeZone": "UTC", "agenda": None,
        "evidence_ids": ["u1"], "sources": {"u1": digest(raw)}, "fingerprint": fingerprint,
        "deviceId": DEVICE,
    }
    if verified:
        content["result"] = {
            "verified": True, "eventId": "event-" + suffix, "calendarId": "calendar-" + suffix,
            "deleted": False, "previousEventId": None, "title": content["title"],
            "start": start_text, "end": end_text, "fingerprint": fingerprint,
            "deviceId": DEVICE,
        }
    row = ActionRun(
        id="stage-c-action-" + suffix, owner_id=owner, meeting_id=meeting.id,
        intent_key=hashlib.sha256(("intent-" + suffix).encode()).hexdigest(), version=3,
        status="completed", content=content, operation="create", starts_at=start,
        ends_at=end, time_zone="UTC", agenda=None,
        verified_at=now() if verified else None,
    )
    async with app.state.database.sessions() as session:
        session.add(meeting)
        await session.flush()
        session.add(row)
        await session.commit()
    return row.id


async def test_analysis_approval_verified_receipt_enters_authority_home_and_preparation(client, app):
    app.state.settings.meeting_preparation_enabled = True
    headers, actions = await prepared(client, app)
    pending = actions[0]
    home = (await client.get("/api/v1/home", headers=headers)).json()["data"]
    assert home["schedules"] == []
    assert any(item["id"] == "action:" + pending["id"] for item in home["decisions"])
    approved = (await client.post(
        f"/api/v1/actions/{pending['id']}/decision", headers=headers,
        json={"fingerprint": pending["fingerprint"], "allow": True, "deviceId": DEVICE,
              "version": 1, "mutationId": "analysis-approve-0001"},
    )).json()["data"]
    assert approved["status"] == "awaiting_device" and approved["version"] == 2
    approved_replay = await client.post(
        f"/api/v1/actions/{pending['id']}/decision", headers=headers,
        json={"fingerprint": pending["fingerprint"], "allow": True, "deviceId": DEVICE,
              "version": 1, "mutationId": "analysis-approve-0001"},
    )
    assert approved_replay.status_code == 200
    assert approved_replay.json()["data"]["version"] == 2
    wrong_version = await client.post(
        f"/api/v1/actions/{pending['id']}/receipt", headers=headers,
        json={**receipt_for(approved), "version": 1, "mutationId": "wrong-version-0001"},
    )
    assert wrong_version.status_code == 409
    completed = await client.post(
        f"/api/v1/actions/{pending['id']}/receipt", headers=headers,
        json={**receipt_for(approved), "version": 2, "mutationId": "analysis-receipt-0001"},
    )
    assert completed.status_code == 200, completed.text
    action = completed.json()["data"]
    assert action["status"] == "completed" and action["version"] == 3 and action["verifiedAt"]
    completed_replay = await client.post(
        f"/api/v1/actions/{pending['id']}/receipt", headers=headers,
        json={**receipt_for(approved), "version": 2, "mutationId": "analysis-receipt-0001"},
    )
    assert completed_replay.status_code == 200
    assert completed_replay.json()["data"]["version"] == 3
    async with app.state.database.sessions() as session:
        run = await session.scalar(select(OutcomeRun).where(
            OutcomeRun.owner_id == (await session.scalar(select(User.id))),
            OutcomeRun.content["kind"].astext == KIND,
        ))
        assert run and run.status == "queued"
        assert run.content["actionVersion"] == 3
        assert datetime.fromisoformat(run.content["sendAt"]) == datetime.fromisoformat(action["start"]).astimezone(timezone.utc) - timedelta(minutes=60)
        run_id = run.id
    home = (await client.get("/api/v1/home", headers=headers)).json()["data"]
    assert len(home["schedules"]) == 1
    assert home["schedules"][0]["authority"] == "authenticated_device_receipt_fields_matched"
    detail = await client.get("/api/v1/assistant/outcomes/" + run_id, headers=headers)
    assert detail.status_code == 200 and detail.json()["data"]["kind"] == KIND


async def test_wrong_receipt_and_legacy_completed_never_enqueue_or_schedule(client, app):
    app.state.settings.meeting_preparation_enabled = True
    headers, actions = await prepared(client, app)
    pending = actions[0]
    approved = (await client.post(
        f"/api/v1/actions/{pending['id']}/decision", headers=headers,
        json={"fingerprint": pending["fingerprint"], "allow": True, "deviceId": DEVICE},
    )).json()["data"]
    wrong = await client.post(
        f"/api/v1/actions/{pending['id']}/receipt", headers=headers,
        json={**receipt_for(approved), "title": "错误标题"},
    )
    assert wrong.status_code == 200 and wrong.json()["data"]["status"] == "awaiting_device"
    async with app.state.database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(OutcomeRun)) == 0
        row = await session.get(ActionRun, pending["id"])
        row.status = "completed"
        row.verified_at = None
        await session.commit()
    await app.state.outcome_jobs.scan()
    async with app.state.database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(OutcomeRun)) == 0
    assert (await client.get("/api/v1/home", headers=headers)).json()["data"]["schedules"] == []


@pytest.mark.parametrize(
    "delta,verified_age,expected,send_now",
    [
        (timedelta(minutes=60), timedelta(0), "queued", False),
        (timedelta(minutes=59, seconds=59), timedelta(0), "queued", True),
        (timedelta(minutes=15), timedelta(minutes=5), "queued", True),
        (timedelta(minutes=15), timedelta(minutes=5, seconds=1), "skipped", False),
        (timedelta(minutes=14, seconds=59), timedelta(0), "skipped", False),
    ],
)
def test_preparation_schedule_boundaries(delta, verified_age, expected, send_now):
    received = datetime(2030, 1, 1, tzinfo=timezone.utc)
    action = SimpleNamespace(starts_at=received + delta, verified_at=received - verified_age)
    status, send_at, reason, _ = schedule_policy(action, received)
    assert status == expected
    if expected == "queued":
        assert send_at == (received if send_now else action.starts_at - timedelta(minutes=60))
    else:
        assert reason in {"lead_time_below_15_minutes", "near_window_verification_too_old"}


async def test_same_authority_version_enqueues_once_and_restart_scan_does_not_duplicate(client, app):
    await register(client)
    app.state.settings.meeting_preparation_enabled = True
    async with app.state.database.sessions() as session:
        owner = await session.scalar(select(User.id))
    action_id = await stored_authority(app, owner, "once", now() + timedelta(hours=2))
    async with app.state.database.sessions() as session:
        action = await session.get(ActionRun, action_id)
        first = await enqueue_preparation(session, action, app.state.settings)
        second = await enqueue_preparation(session, action, app.state.settings)
        assert first.id == second.id
        await session.commit()
    await app.state.outcome_jobs.scan()
    await app.state.outcome_jobs.scan()
    async with app.state.database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(OutcomeRun).where(
            OutcomeRun.content["kind"].astext == KIND)) == 1


async def test_reschedule_keeps_old_until_verified_then_stales_old_and_projects_new(client, app):
    headers, old = await completed_projected(client, app)
    before = (await client.get("/api/v1/home", headers=headers)).json()["data"]["schedules"]
    assert [item["sourceId"] for item in before] == [old["id"]]
    request = {
        "currentVersion": old["version"], "requestId": "reschedule-request-0001",
        "start": "2030-10-24T14:00:00+08:00", "end": "2030-10-24T15:00:00+08:00",
        "timeZone": "Asia/Shanghai", "title": "改期后的项目进度会", "agenda": "核对项目进度",
    }
    response = await client.post(f"/api/v1/actions/{old['id']}/reschedule", headers=headers, json=request)
    assert response.status_code == 200, response.text
    mutation = response.json()["data"]
    replay = await client.post(f"/api/v1/actions/{old['id']}/reschedule", headers=headers, json=request)
    assert replay.status_code == 200 and replay.json()["data"]["id"] == mutation["id"]
    during = (await client.get("/api/v1/home", headers=headers)).json()["data"]["schedules"]
    assert [item["sourceId"] for item in during] == [old["id"]]
    approved = (await client.post(
        f"/api/v1/actions/{mutation['id']}/decision", headers=headers,
        json={"fingerprint": mutation["fingerprint"], "allow": True, "deviceId": DEVICE,
              "version": 1, "mutationId": "reschedule-approve-0001"},
    )).json()["data"]
    receipt = receipt_for(
        approved, event="native-event-1", calendar="calendar-1", previous="native-event-1"
    )
    done = await client.post(
        f"/api/v1/actions/{mutation['id']}/receipt", headers=headers,
        json={**receipt, "version": 2, "mutationId": "reschedule-receipt-0001"},
    )
    assert done.status_code == 200, done.text
    current = done.json()["data"]
    assert current["status"] == "completed"
    after = (await client.get("/api/v1/home", headers=headers)).json()["data"]["schedules"]
    assert [item["sourceId"] for item in after] == [current["id"]]
    async with app.state.database.sessions() as session:
        old_row = await session.get(ActionRun, old["id"])
        old_runs = list(await session.scalars(select(OutcomeRun).where(
            OutcomeRun.content["actionId"].astext == old["id"])))
        new_runs = list(await session.scalars(select(OutcomeRun).where(
            OutcomeRun.content["actionId"].astext == current["id"])))
        assert old_row.status == "superseded"
        assert old_runs and all(run.status == "stale" for run in old_runs)
        assert len(new_runs) == 1


async def test_cancel_verified_receipt_and_deleted_source_invalidate_authority(client, app):
    headers, old = await completed_projected(client, app)
    request = {"currentVersion": old["version"], "requestId": "cancel-request-0001"}
    mutation = (await client.post(
        f"/api/v1/actions/{old['id']}/cancel", headers=headers, json=request
    )).json()["data"]
    approved = (await client.post(
        f"/api/v1/actions/{mutation['id']}/decision", headers=headers,
        json={"fingerprint": mutation["fingerprint"], "allow": True, "deviceId": DEVICE},
    )).json()["data"]
    result = await client.post(
        f"/api/v1/actions/{mutation['id']}/receipt", headers=headers,
        json=receipt_for(approved, event="native-event-1", calendar="calendar-1",
                         deleted=True, previous="native-event-1"),
    )
    assert result.status_code == 200 and result.json()["data"]["status"] == "completed"
    assert (await client.get("/api/v1/home", headers=headers)).json()["data"]["schedules"] == []
    async with app.state.database.sessions() as session:
        old_row = await session.get(ActionRun, old["id"])
        runs = list(await session.scalars(select(OutcomeRun).where(
            OutcomeRun.content["actionId"].astext == old["id"])))
        assert old_row.status == "cancelled" and all(run.status == "cancelled" for run in runs)


async def test_no_related_history_never_calls_model_or_invents_details(monkeypatch):
    class Search:
        def __init__(self, session): pass
        async def search(self, *args, **kwargs): return []
    monkeypatch.setattr("app.modules.search.service.UnifiedSearch", Search)
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))
    provider = SimpleNamespace(run=AsyncMock())
    action = SimpleNamespace(
        meeting_id="missing", agenda=None, time_zone="UTC",
        content={"title": "真实会议", "start": "2030-01-01T12:00:00+00:00", "evidence_ids": []},
    )
    result, timing = await prepare_meeting(provider, session, "owner-a", action, {})
    assert provider.run.await_count == 0
    assert result["meeting"] == {"title": "真实会议", "start": "2030-01-01T12:00:00+00:00", "timeZone": "UTC", "agenda": None}
    assert result["sections"] == [] and result["reading"] == []
    assert result["dataGaps"] and timing["modelStartedAt"] is None


async def test_owner_isolation_and_deleted_source_hide_preparation(client, app):
    first = await register(client)
    second = await register(client, "stage-c-other@example.com")
    app.state.settings.meeting_preparation_enabled = True
    async with app.state.database.sessions() as session:
        owner = await session.scalar(select(User.id).where(User.email == "yan@example.com"))
    action_id = await stored_authority(app, owner, "isolation", now() + timedelta(hours=2))
    async with app.state.database.sessions() as session:
        action = await session.get(ActionRun, action_id)
        run = await enqueue_preparation(session, action, app.state.settings)
        run_id, meeting_id = run.id, action.meeting_id
        await session.commit()
    assert (await client.get("/api/v1/assistant/outcomes/" + run_id, headers=bearer(first))).status_code == 200
    assert (await client.get("/api/v1/assistant/outcomes/" + run_id, headers=bearer(second))).status_code == 404
    assert (await client.post(
        f"/api/v1/actions/{action_id}/cancel", headers=bearer(second),
        json={"currentVersion": 3, "requestId": "cross-owner-cancel-0001"},
    )).status_code == 404
    async with app.state.database.sessions() as session:
        meeting = await session.get(Meeting, meeting_id)
        meeting.deleted = True
        await session.commit()
    await app.state.outcome_jobs.scan()
    assert (await client.get("/api/v1/assistant/outcomes/" + run_id, headers=bearer(first))).status_code == 404


@pytest.mark.parametrize(
    "binding,reason",
    [
        (None, "wechat_not_bound"),
        (SimpleNamespace(status="reauth_required", state={}, encrypted=None), "wechat_not_bound"),
        (SimpleNamespace(status="bound", state={"generation":"g","contextReady":True,"preferences":{"paused":True}}, encrypted="x"), "wechat_paused"),
        (SimpleNamespace(status="bound", state={"generation":"g","contextReady":True,"preferences":{"preMeeting":False}}, encrypted="x"), "pre_meeting_disabled"),
    ],
)
async def test_preparation_delivery_gate_blocks_unbound_reauth_paused_and_opt_out(binding, reason):
    settings = SimpleNamespace(meeting_preparation_recovery_minutes=15)
    worker = WechatJobs(SimpleNamespace(), settings, SimpleNamespace())
    report = SimpleNamespace(content={"sendAt": now().isoformat(), "policy":{"recoveryMinutes":15}})
    state, actual = await worker._preparation_gate(None, binding, report)
    assert state == "suppressed" and actual == reason


async def test_preparation_uncertain_transport_is_not_retried(client, app):
    await register(client)
    app.state.settings.meeting_preparation_enabled = True
    app.state.settings.wechat_binding_key = SecretStr(Fernet.generate_key().decode())
    fake = transport()
    fake.send.side_effect = ChannelError("transport_timeout", uncertain=True)
    async with app.state.database.sessions() as session:
        owner = await session.scalar(select(User.id))
    action_id = await stored_authority(app, owner, "uncertain", now() + timedelta(minutes=40))
    async with app.state.database.sessions() as session:
        action = await session.get(ActionRun, action_id)
        run = await enqueue_preparation(session, action, app.state.settings, scheduler_received_at=now())
        run.status = "succeeded"
        run.content = {**run.content, "sendAt": (now()-timedelta(seconds=1)).isoformat(),
            "result":{"title":"会前准备","summary":"仅依据真实资料","meeting":{"title":"会议","start":action.content["start"],"timeZone":"UTC","agenda":None},
                      "sections":[],"checklist":[],"reading":[],"dataGaps":[],"sources":[],
                      "notification_summary":"仅依据真实资料","notify":True},
            "delivery":{"status":"not_sent"}}
        binding = WechatBinding(
            owner_id=owner, version=1, status="bound", peer_hash="stage-c-uncertain-peer",
            encrypted=cipher_for(app.state.settings).encode({"base":"https://ilinkai.weixin.qq.com",
                "bot":"test-bot","peer":"verified-peer","token":"secret","context":"ctx","cursor":""}),
            state={"boundAt":(now()-timedelta(hours=1)).isoformat(),"generation":"stage-c-generation",
                   "contextReady":True,"preferences":{"paused":False,"preMeeting":True,"importantOnly":False}},
            updated_at=now()-timedelta(seconds=3),
        )
        session.add(binding)
        await session.commit()
        run_id=run.id
    worker=WechatJobs(app.state.database,app.state.settings,fake)
    assert await worker.process_one()
    assert fake.send.await_count == 1
    async with app.state.database.sessions() as session:
        run=await session.get(OutcomeRun,run_id)
        assert run.content["delivery"]["status"] == "uncertain"
        binding=await session.get(WechatBinding,owner)
        binding.updated_at=now()-timedelta(seconds=3)
        await session.commit()
    await worker.process_one()
    assert fake.send.await_count == 1


async def test_worker_persists_fallback_with_timings_and_zero_model_calls(client, app, monkeypatch):
    from app.modules.outcomes.jobs import OutcomeJobs
    class Search:
        def __init__(self, session): pass
        async def search(self, *args, **kwargs): return []
    monkeypatch.setattr("app.modules.search.service.UnifiedSearch", Search)
    auth = await register(client)
    app.state.settings.meeting_preparation_enabled = True
    async with app.state.database.sessions() as session:
        owner = await session.scalar(select(User.id))
    action_id = await stored_authority(app, owner, "worker-fallback", now() + timedelta(hours=2))
    async with app.state.database.sessions() as session:
        action = await session.get(ActionRun, action_id)
        run = await enqueue_preparation(session, action, app.state.settings)
        run_id = run.id
        await session.commit()
    provider = SimpleNamespace(run=AsyncMock())
    worker = OutcomeJobs(app.state.database, provider, app.state.settings)
    assert await worker.process_one()
    assert provider.run.await_count == 0
    async with app.state.database.sessions() as session:
        saved = await session.get(OutcomeRun, run_id)
        assert saved.status == "succeeded"
        assert saved.content["result"]["dataGaps"]
        assert saved.content["timing"]["retrievalStartedAt"]
        assert saved.content["timing"]["finishedAt"]
        assert saved.content["cost"]["calls"] == []
    home = (await client.get("/api/v1/home", headers=bearer(auth))).json()["data"]
    schedule = next(item for item in home["schedules"] if item["sourceId"] == action_id)
    assert schedule["preparationId"] == run_id
    assert schedule["preparationStatus"] == "succeeded"
    assert schedule["preparationSummary"]
    assert schedule["preparationRoute"] == "/outcomes/" + run_id


async def test_preparation_engine_rejects_non_verbatim_quote(monkeypatch):
    class Search:
        def __init__(self, session): pass
        async def search(self, *args, **kwargs):
            return [{"kind":"document","id":"doc1","title":"真实资料","text":"只包含真实证据",
                     "path":"/knowledge/documents/doc1","version":1}]
    monkeypatch.setattr("app.modules.search.service.UnifiedSearch", Search)
    provider = SimpleNamespace(run=AsyncMock(return_value={
        "title":"准备","titleSourceIds":["related:document:doc1"],
        "summary":"摘要","summarySourceIds":["related:document:doc1"],
        "sections":[],"checklist":[],"reading":[],"dataGaps":[],"dataGapSourceIds":[],
        "sources":[{"sourceId":"related:document:doc1","quote":"模型编造引文"}],
        "notification_summary":"准备摘要","notificationSourceIds":["related:document:doc1"],"notify":True,
    }))
    action = SimpleNamespace(
        meeting_id="none", agenda=None, time_zone="UTC",
        content={"title":"真实会议","start":"2030-01-01T12:00:00+00:00","evidence_ids":[]},
    )
    with pytest.raises(ValueError, match="invalid_preparation_quote"):
        await prepare_meeting(provider, SimpleNamespace(scalar=AsyncMock(return_value=None)), "owner", action, {})


async def test_delivery_gate_expires_after_configured_recovery_window():
    worker = WechatJobs(SimpleNamespace(), SimpleNamespace(meeting_preparation_recovery_minutes=15), SimpleNamespace())
    binding = SimpleNamespace(status="bound", state={"generation":"g","contextReady":True,
        "preferences":{"preMeeting":True}}, encrypted="x")
    report = SimpleNamespace(content={"sendAt":(now()-timedelta(minutes=15,seconds=1)).isoformat(),
                                      "policy":{"recoveryMinutes":15}})
    state, reason = await worker._preparation_gate(None, binding, report)
    assert (state, reason) == ("expired", "recovery_window_elapsed")


async def test_only_one_live_calendar_mutation_can_target_a_version(client, app):
    headers, old = await completed_projected(client, app)
    first = await client.post(f"/api/v1/actions/{old['id']}/reschedule", headers=headers, json={
        "currentVersion":old["version"],"requestId":"first-live-mutation-0001",
        "start":"2030-11-01T10:00:00+08:00","end":"2030-11-01T11:00:00+08:00",
        "timeZone":"Asia/Shanghai"})
    assert first.status_code == 200
    mutation = first.json()["data"]
    wrong_device = await client.post(f"/api/v1/actions/{mutation['id']}/decision", headers=headers, json={
        "fingerprint":mutation["fingerprint"],"allow":True,"deviceId":"stage-c-other-device-0002"})
    assert wrong_device.status_code == 409
    second = await client.post(f"/api/v1/actions/{old['id']}/cancel", headers=headers, json={
        "currentVersion":old["version"],"requestId":"second-live-mutation-0001"})
    assert second.status_code == 409
    assert second.json()["message"] == "action_mutation_pending"


async def test_authority_rejects_persisted_create_receipt_marked_deleted(client, app):
    await register(client)
    async with app.state.database.sessions() as session:
        owner = await session.scalar(select(User.id))
    action_id = await stored_authority(app, owner, "deleted-receipt", now()+timedelta(hours=2))
    async with app.state.database.sessions() as session:
        action = await session.get(ActionRun, action_id)
        action.content = {**action.content, "result": {**action.content["result"], "deleted": True}}
        await session.commit()
    async with app.state.database.sessions() as session:
        assert await authoritative_action(session, owner, action_id) is None


async def test_below_minimum_lead_persists_terminal_skipped_reason(client, app):
    await register(client)
    app.state.settings.meeting_preparation_enabled = True
    async with app.state.database.sessions() as session:
        owner = await session.scalar(select(User.id))
    action_id = await stored_authority(app, owner, "too-close", now()+timedelta(minutes=14))
    async with app.state.database.sessions() as session:
        action = await session.get(ActionRun, action_id)
        run = await enqueue_preparation(session, action, app.state.settings, scheduler_received_at=now())
        assert run.status == "skipped"
        assert run.content["terminalReason"] == "lead_time_below_15_minutes"
        assert run.content["sendAt"] is None
        await session.commit()


async def test_inflight_model_result_cannot_overwrite_stale_preparation(client, app, monkeypatch):
    import asyncio
    from app.modules.outcomes.jobs import OutcomeJobs
    from app.modules.outcomes.meeting_preparation import invalidate_action_preparations
    class Search:
        def __init__(self, session): pass
        async def search(self, *args, **kwargs):
            return [{"kind":"document","id":"race-doc","title":"真实资料","text":"真实准备依据",
                     "path":"/knowledge/documents/race-doc","version":1}]
    monkeypatch.setattr("app.modules.search.service.UnifiedSearch", Search)
    await register(client)
    app.state.settings.meeting_preparation_enabled = True
    async with app.state.database.sessions() as session:
        owner = await session.scalar(select(User.id))
    action_id = await stored_authority(app, owner, "claim-race", now()+timedelta(hours=2))
    async with app.state.database.sessions() as session:
        action = await session.get(ActionRun, action_id)
        run = await enqueue_preparation(session, action, app.state.settings)
        run_id = run.id
        await session.commit()
    started, release = asyncio.Event(), asyncio.Event()
    source_id = "related:document:race-doc"
    class Provider:
        async def run(self, *args):
            started.set()
            await release.wait()
            return {"title":"准备","titleSourceIds":[source_id],"summary":"真实准备依据",
                "summarySourceIds":[source_id],"sections":[],"checklist":[],"reading":[],
                "dataGaps":[],"dataGapSourceIds":[],
                "sources":[{"sourceId":source_id,"quote":"真实准备依据"}],
                "notification_summary":"真实准备依据","notificationSourceIds":[source_id],"notify":True}
    task = asyncio.create_task(OutcomeJobs(app.state.database, Provider(), app.state.settings).process_one())
    await asyncio.wait_for(started.wait(), 2)
    async with app.state.database.sessions() as session:
        action = await session.get(ActionRun, action_id, with_for_update=True)
        action.status = "cancelled"
        action.version += 1
        await invalidate_action_preparations(session, action_id, cancelled=True)
        await session.commit()
    release.set()
    assert await task
    async with app.state.database.sessions() as session:
        run = await session.get(OutcomeRun, run_id)
        assert run.status == "cancelled"
        assert run.content.get("result") is None
        assert run.content["terminalReason"] == "calendar_cancelled"


async def test_retryable_preparation_channel_failure_stops_after_three_attempts(client, app):
    await register(client)
    app.state.settings.meeting_preparation_enabled = True
    app.state.settings.wechat_binding_key = SecretStr(Fernet.generate_key().decode())
    fake = transport()
    fake.send.side_effect = ChannelError("temporary_channel_failure", uncertain=False)
    async with app.state.database.sessions() as session:
        owner = await session.scalar(select(User.id))
    action_id = await stored_authority(app, owner, "bounded-channel", now()+timedelta(minutes=40))
    async with app.state.database.sessions() as session:
        action = await session.get(ActionRun, action_id)
        run = await enqueue_preparation(session, action, app.state.settings, scheduler_received_at=now())
        run.status = "succeeded"
        run.content = {**run.content, "sendAt":(now()-timedelta(seconds=1)).isoformat(),
            "result":{"title":"会前准备","summary":"真实资料","meeting":{"title":"会议","start":action.content["start"],"timeZone":"UTC","agenda":None},
                "sections":[],"checklist":[],"reading":[],"dataGaps":[],"sources":[],
                "notification_summary":"真实资料","notify":True},"delivery":{"status":"not_sent"}}
        session.add(WechatBinding(owner_id=owner,version=1,status="bound",peer_hash="bounded-channel-peer",
            encrypted=cipher_for(app.state.settings).encode({"base":"https://ilinkai.weixin.qq.com",
                "bot":"bot","peer":"peer","token":"secret","context":"ctx","cursor":""}),
            state={"boundAt":(now()-timedelta(hours=1)).isoformat(),"generation":"bounded-generation",
                "contextReady":True,"preferences":{"paused":False,"preMeeting":True,"importantOnly":False}},
            updated_at=now()-timedelta(seconds=3)))
        run_id=run.id
        await session.commit()
    worker=WechatJobs(app.state.database,app.state.settings,fake)
    for expected_calls in (1,2,3):
        assert await worker.process_one()
        assert fake.send.await_count == expected_calls
        async with app.state.database.sessions() as session:
            run=await session.get(OutcomeRun,run_id)
            assert run.content["delivery"]["status"] == "retryable"
            run.content={**run.content,"delivery":{**run.content["delivery"],
                "nextAt":(now()-timedelta(seconds=1)).isoformat()}}
            binding=await session.get(WechatBinding,owner)
            binding.updated_at=now()-timedelta(seconds=3)
            await session.commit()
    assert await worker.process_one()
    assert fake.send.await_count == 3
    client_ids=[call.args[2] for call in fake.send.await_args_list]
    assert len(set(client_ids)) == 1
    async with app.state.database.sessions() as session:
        run=await session.get(OutcomeRun,run_id)
        assert run.content["delivery"]["status"] == "failed"
        assert run.content["delivery"]["reason"] == "delivery_retries_exhausted"
