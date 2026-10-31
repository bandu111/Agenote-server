"""会议持久化、账户隔离和多 Agent 结果边界回归。"""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock
import pytest
from app.modules.meetings.schemas.meeting import AgentReport
from app.modules.meetings.services.meeting_agents import (
    validate_report,
    select_context,
    MeetingAgents,
)
from app.modules.meetings.services.meeting_agents import explicit_change
from app.integrations.hermes.provider import HermesUnavailableError
from app.modules.meetings.services.transcript_polish_service import checked_polish


async def account(client, email="meeting@example.com"):
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "name": "会议测试",
            "email": email,
            "password": "meeting-test-password-2026",
        },
    )
    assert response.status_code == 201
    return {"Authorization": "Bearer " + response.json()["data"]["access_token"]}


def meeting(revision=1):
    return {
        "id": "phone-meeting-1",
        "revision": revision,
        "title": "交付会议",
        "startedAt": "2026-10-19T10:00:00+08:00",
        "elapsed": 61000,
        "ended": False,
        "utterances": [{"id": "u1", "speaker": "我", "raw": "验收我们先……", "at": 0}],
    }


async def test_meeting_sync_history_tenant_isolation_and_archive(client):
    first = await account(client)
    second = await account(client, "other@example.com")
    response = await client.post("/api/v1/meetings/sync", headers=first, json=meeting())
    assert response.status_code == 200
    assert response.json()["data"]["id"] == "phone-meeting-1"
    assert response.json()["data"]["archived"] is False
    assert (
        len((await client.get("/api/v1/meetings", headers=first)).json()["data"]) == 1
    )
    assert (await client.get("/api/v1/meetings", headers=second)).json()["data"] == []
    assert (
        await client.get("/api/v1/meetings/phone-meeting-1", headers=second)
    ).status_code == 404
    assert (
        await client.post("/api/v1/meetings/phone-meeting-1/analysis", headers=second)
    ).status_code == 404
    assert (
        await client.get("/api/v1/meetings/phone-meeting-1/commitments", headers=second)
    ).status_code == 404
    other = await client.post("/api/v1/meetings/sync", headers=second, json=meeting())
    assert other.status_code == 200
    assert other.json()["data"]["sessionId"] != response.json()["data"]["sessionId"]
    assert other.json()["data"]["ownerUserId"] != response.json()["data"]["ownerUserId"]
    payload = meeting(2)
    payload.update(ended=True, minutes="会议纪要：验收标准仍待补全。")
    final = await client.post("/api/v1/meetings/sync", headers=first, json=payload)
    assert final.json()["data"]["archived"] is True
    assert (
        await client.post("/api/v1/meetings/sync", headers=first, json=payload)
    ).status_code == 200
    payload["revision"] = 3
    payload["ended"] = False
    assert (
        await client.post("/api/v1/meetings/sync", headers=first, json=payload)
    ).status_code == 409


async def test_legacy_record_without_optional_fields_keeps_history_and_detail_readable(client, app):
    from sqlalchemy import select
    from app.modules.meetings.models.meeting import Meeting

    first = await account(client)
    second = await account(client, "separate-history@example.com")
    for client_id in ("legacy-record", "healthy-record"):
        payload = {**meeting(), "id": client_id, "ended": True}
        assert (await client.post("/api/v1/meetings/sync", headers=first, json=payload)).status_code == 200
    async with app.state.database.sessions() as db:
        row = await db.scalar(select(Meeting).where(Meeting.client_id == "legacy-record"))
        row.content = {key: value for key, value in row.content.items()
                       if key not in {"minutes", "paused", "revision"}}
        await db.commit()

    history = await client.get("/api/v1/meetings", headers=first)
    assert history.status_code == 200
    assert {r["id"] for r in history.json()["data"]} == {"legacy-record", "healthy-record"}
    detail = await client.get("/api/v1/meetings/legacy-record", headers=first)
    assert detail.status_code == 200
    value = detail.json()["data"]
    assert value["minutes"] == "" and value["paused"] is False
    assert value["revision"] == 1 and value["archived"] is False
    assert value["utterances"][0]["raw"] == meeting()["utterances"][0]["raw"]
    assert (await client.get("/api/v1/meetings/legacy-record", headers=second)).status_code == 404
    assert (await client.get("/api/v1/meetings", headers=second)).json()["data"] == []


@pytest.mark.parametrize(
    "change",
    [
        "raw",
        "speaker",
        "speakerId",
        "speakerSeparated",
        "at",
        "reorder",
        "startedAt",
        "elapsed",
    ],
)
async def test_original_source_cannot_be_rewritten(client, change):
    headers = await account(client)
    await client.post("/api/v1/meetings/sync", headers=headers, json=meeting())
    data = meeting(2)
    if change in ["raw", "speaker", "at"]:
        data["utterances"][0][change] = 12 if change == "at" else "被替换"
    elif change in ["speakerId", "speakerSeparated"]:
        data["utterances"][0][change] = (
            True if change == "speakerSeparated" else "other"
        )
    elif change == "reorder":
        data["utterances"].insert(
            0, {"id": "new", "speaker": "客户", "raw": "插入原话之前", "at": 0}
        )
    elif change == "startedAt":
        data[change] = "2026-10-20T10:00:00+08:00"
    else:
        data[change] = 0
    response = await client.post("/api/v1/meetings/sync", headers=headers, json=data)
    assert response.status_code == 409
    assert response.json()["message"] == "meeting_revision_conflict"


async def test_sync_stale_duplicate_and_parallel_initial_requests(client):
    headers = await account(client)
    results = await asyncio.gather(
        *[
            client.post("/api/v1/meetings/sync", headers=headers, json=meeting())
            for _ in range(2)
        ]
    )
    assert [r.status_code for r in results] == [200, 200]
    assert (
        len((await client.get("/api/v1/meetings", headers=headers)).json()["data"]) == 1
    )
    changed = meeting()
    changed["title"] = "同版本不同内容"
    assert (
        await client.post("/api/v1/meetings/sync", headers=headers, json=changed)
    ).status_code == 409
    await client.post("/api/v1/meetings/sync", headers=headers, json=meeting(2))
    stale = await client.post("/api/v1/meetings/sync", headers=headers, json=meeting(1))
    assert stale.json()["data"]["revision"] == 2


@pytest.mark.parametrize("field", ["audioPaths", "audioPath", "owner_id", "token"])
async def test_local_audio_and_unexpected_fields_rejected(client, field):
    headers = await account(client)
    data = meeting()
    data[field] = "禁止上传"
    response = await client.post("/api/v1/meetings/sync", headers=headers, json=data)
    assert response.status_code == 422
    assert "禁止上传" not in response.text


async def test_analysis_cache_failure_and_stale_result(client, app):
    headers = await account(client)
    await client.post("/api/v1/meetings/sync", headers=headers, json=meeting())
    agents = app.state.meeting_analysis.agents
    agents.analyze = AsyncMock(
        return_value={"insights": [], "repairs": [], "withdrawals": []}
    )
    url = "/api/v1/meetings/phone-meeting-1/analysis"
    first = await client.post(url, headers=headers)
    assert first.status_code == 200
    assert first.json()["data"]["cached"] is False
    assert (await client.post(url, headers=headers)).json()["data"]["cached"] is True
    assert agents.analyze.await_count == 1
    await client.post("/api/v1/meetings/sync", headers=headers, json=meeting(2))
    agents.analyze.side_effect = HermesUnavailableError()
    assert (await client.post(url, headers=headers)).status_code == 503
    assert (
        await client.get("/api/v1/meetings/phone-meeting-1", headers=headers)
    ).json()["data"]["utterances"][0]["raw"] == "验收我们先……"

    async def advance(_, **_kwargs):
        await client.post("/api/v1/meetings/sync", headers=headers, json=meeting(3))
        return {"insights": [], "repairs": [], "withdrawals": []}

    agents.analyze.side_effect = advance
    assert (await client.post(url, headers=headers)).status_code == 409
    saved = (
        await client.get("/api/v1/meetings/phone-meeting-1", headers=headers)
    ).json()["data"]
    assert saved["revision"] == 3 and saved["analysisRevision"] == 1


async def test_pause_resume_and_auth_required(client):
    assert (await client.get("/api/v1/meetings")).status_code == 401
    headers = await account(client)
    data = meeting()
    data["paused"] = True
    assert (
        await client.post("/api/v1/meetings/sync", headers=headers, json=data)
    ).json()["data"]["paused"] is True
    data.update(revision=2, paused=False)
    assert (
        await client.post("/api/v1/meetings/sync", headers=headers, json=data)
    ).json()["data"]["paused"] is False


def test_evidence_guards_gap_offsets_and_explicit_retraction():
    sources = [
        {"id": "u1", "raw": "😀验收……", "withdrawn": False},
        {"id": "u2", "raw": "预算先不讨论"},
        {"id": "u3", "raw": "回到验收，周三确认指标。"},
        {"id": "u4", "raw": "原定周五交付"},
        {"id": "u5", "raw": "取消周五交付，改为下周一。"},
    ]
    for i, u in enumerate(sources):
        u.update(speaker="发言人 A", speaker_id="a", speaker_separated=True, at=i*1000)
    report = AgentReport.model_validate(
        {
            "insights": [
                {
                    "id": "x",
                    "kind": "risk",
                    "title": "无依据",
                    "body": "不应保留",
                    "evidence_ids": ["missing"],
                }
            ],
            "repairs": [
                {
                    "target_id": "u1",
                    "text": "周三确认指标",
                    "evidence_ids": ["u1", "u3"],
                    "start": 99,
                    "end": 99,
                },
                {
                    "target_id": "u2",
                    "text": "虚构补齐",
                    "evidence_ids": ["u2"],
                    "start": 0,
                    "end": 0,
                },
            ],
            "withdrawals": [
                {"target_id": "u4", "evidence_ids": ["u5"], "reason": "明确改期"},
                {"target_id": "u2", "evidence_ids": ["u3"], "reason": "没有撤回证据"},
            ],
        }
    )
    original = deepcopy(sources)
    result = validate_report(report, sources)
    assert sources == original
    assert result["insights"] == []
    assert len(result["repairs"]) == 1
    assert result["repairs"][0]["start"] == 4 and result["repairs"][0]["end"] == 6
    assert result["withdrawals"] == []
    assert [item["target_id"] for item in result["withdrawal_candidates"]] == ["u4"]


def test_interrupted_topic_survives_many_intervening_turns():
    data = [{"id": "gap", "raw": "验收先……"}]
    data.extend({"id": f"other{i}", "raw": "讨论预算与现场安排"} for i in range(40))
    data.append({"id": "resume", "raw": "接回验收，我们周三确认。"})
    selected = select_context(data)
    assert selected[0]["id"] == "gap"
    assert selected[-1]["id"] == "resume"
    assert len(selected) == 33


async def test_archive_generates_once_and_keeps_originals_private(client, app):
    headers = await account(client)
    other = await account(client, "archive-other@example.com")
    await client.post("/api/v1/meetings/sync", headers=headers, json=meeting())
    url = "/api/v1/meetings/phone-meeting-1/archive"
    assert (await client.post(url, headers=headers)).status_code == 409
    data = meeting(2)
    data["ended"] = True
    await client.post("/api/v1/meetings/sync", headers=headers, json=data)
    provider = app.state.meeting_analysis.agents.provider
    provider.run = AsyncMock(
        return_value={
            "decisions": [],
            "actions": [],
            "open_questions": [{"text": "验收指标待补齐", "evidence_ids": ["u1"]}],
        }
    )
    first = await client.post(url, headers=headers)
    assert first.status_code == 200
    saved = first.json()["data"]
    assert saved["visibility"] == "private" and saved["retrievalAllowed"] is True
    assert "验收我们先……" in saved["markdown"]
    assert "验收指标待补齐" in saved["markdown"]
    again = (await client.post(url, headers=headers)).json()["data"]
    assert again["documentId"] == saved["documentId"] and again["cached"] is True
    assert provider.run.await_count == 1
    history = (await client.get("/api/v1/meetings", headers=headers)).json()["data"]
    assert (
        history[0]["archived"] is True
        and history[0]["documentId"] == saved["documentId"]
    )
    assert (await client.get(url, headers=other)).status_code == 404
    assert (await client.get(url, headers=headers)).json()["data"]["stale"] is False


async def test_archive_rejects_unknown_citations_without_losing_transcript(client, app):
    headers = await account(client)
    data = meeting()
    data["ended"] = True
    await client.post("/api/v1/meetings/sync", headers=headers, json=data)
    app.state.meeting_analysis.agents.provider.run = AsyncMock(
        return_value={
            "decisions": [{"text": "编造的决定", "evidence_ids": ["unknown"]}],
            "actions": [],
            "open_questions": [],
        }
    )
    url = "/api/v1/meetings/phone-meeting-1/archive"
    assert (await client.post(url, headers=headers)).status_code == 503
    assert (await client.get(url, headers=headers)).status_code == 404
    assert (
        await client.get("/api/v1/meetings/phone-meeting-1", headers=headers)
    ).status_code == 200


async def test_mobile_job_returns_immediately_deduplicates_and_is_owner_scoped(
    client, app
):
    headers = await account(client)
    other = await account(client, "jobs-other@example.com")
    await client.post("/api/v1/meetings/sync", headers=headers, json=meeting())
    release = asyncio.Event()

    async def slow(_, **_kwargs):
        await release.wait()
        return {"insights": [], "repairs": [], "withdrawals": []}

    app.state.meeting_analysis.agents.analyze = AsyncMock(side_effect=slow)
    url = "/api/v1/meetings/phone-meeting-1/jobs"
    first = await client.post(url, headers=headers, json={"operation": "analysis"})
    assert first.status_code == 202
    job = first.json()["data"]
    again = await client.post(url, headers=headers, json={"operation": "analysis"})
    assert again.json()["data"]["id"] == job["id"]
    status_url = "/api/v1/meetings/jobs/" + job["id"]
    assert (await client.get(status_url, headers=other)).status_code == 404
    assert (await client.get(status_url, headers=headers)).json()["data"]["status"] in {
        "queued",
        "running",
    }
    release.set()
    await asyncio.gather(*list(app.state.meeting_jobs.tasks))
    final = (await client.get(status_url, headers=headers)).json()["data"]
    assert final["status"] == "succeeded" and final["result"]["revision"] == 1
    assert app.state.meeting_analysis.agents.analyze.await_count == 1


@pytest.mark.parametrize(
    "text",
    [
        "不要取消周五交付。",
        "如果取消周五交付会怎样？",
        "是否改为下周一？",
        "我们不改为下周一。",
    ],
)
def test_hypothetical_or_negated_change_cannot_retract(text):
    assert explicit_change(text) is False


def test_asr_without_ellipsis_can_anchor_at_end_only_with_explicit_resumption():
    sources = [
        {"id": "a", "speaker": "我", "speaker_id": "a", "speaker_separated": True, "at": 0, "raw": "验收我们先"},
        {"id": "b", "speaker": "我", "speaker_id": "a", "speaker_separated": True, "at": 1000, "raw": "接着刚才验收的话题，先在周三确认。"},
    ]
    report = AgentReport.model_validate(
        {
            "repairs": [
                {
                    "target_id": "a",
                    "text": "在周三确认",
                    "start": 0,
                    "end": 0,
                    "evidence_ids": ["a", "b"],
                }
            ]
        }
    )
    repair = validate_report(report, sources)["repairs"][0]
    assert repair["start"] == repair["end"] == len("验收我们先")
    assert sources[0]["raw"] == "验收我们先"
    sources[1]["speaker"] = "其他人"
    sources[1]["speaker_id"] = "b"
    assert validate_report(report, sources)["repairs"] == []


@pytest.mark.parametrize(
    "cleaned",
    [
        "我们同意周三交付，预算10万元。",
        "我们可能周四交付，预算10万元。",
        "我们可能周三交付，预算12万元。",
        "我们可能周三交付，预算10天。",
    ],
)
def test_polish_cannot_change_commitment_date_amount_or_unit(cleaned):
    raw = "嗯，我们可能周三交付，预算10万元。"
    result, accepted = checked_polish(raw, cleaned)
    assert accepted is False and result == raw


def test_polish_removes_fillers_without_changing_negation_or_acceptance():
    raw = "嗯，我们我们先评估 Windows 需求，还没有同意。"
    cleaned = "我们先评估 Windows 需求，还没有同意。"
    assert checked_polish(raw, cleaned) == (cleaned, True)
    assert checked_polish(raw, "我们我们先评估 Windows 需求，还没有同意。") == (
        cleaned,
        True,
    )
    assert checked_polish("通过率95%。", "通过率95。")[1] is False


async def test_polish_job_authenticated_scoped_and_not_executing_actions(client, app):
    headers = await account(client)
    other = await account(client, "polish-other@example.com")
    app.state.meeting_analysis.agents.provider.run = AsyncMock(
        return_value={"cleaned": "我们先评估，还没有同意。"}
    )
    payload = {"meetingId": "m1", "id": "u1", "raw": "嗯，我们我们先评估，还没有同意。"}
    assert (
        await client.post("/api/v1/meetings/polish", json=payload)
    ).status_code == 401
    response = await client.post(
        "/api/v1/meetings/polish", headers=headers, json=payload
    )
    assert response.status_code == 202
    job_id = response.json()["data"]["id"]
    await asyncio.gather(*list(app.state.meeting_jobs.tasks))
    url = "/api/v1/meetings/jobs/" + job_id
    assert (await client.get(url, headers=other)).status_code == 404
    job = (await client.get(url, headers=headers)).json()["data"]
    assert job["status"] == "succeeded"
    assert job["result"]["polished"]["cleaned"] == "我们先评估，还没有同意。"
    assert job["result"]["polished"]["accepted"] is True


async def test_agent_output_format_repair_is_bounded():
    from types import SimpleNamespace
    from pydantic import ValidationError

    provider = SimpleNamespace(run=AsyncMock(side_effect=[{"invalid": []}, {}]))
    assert await MeetingAgents(provider).run_role("state", {}) == {}
    assert provider.run.await_count == 2
    provider.run = AsyncMock(return_value={"invalid": []})
    with pytest.raises(ValidationError):
        await MeetingAgents(provider).run_role("state", {})
    assert provider.run.await_count == 2

async def test_role_output_discards_only_known_empty_legacy_fields_before_downstream():
    from types import SimpleNamespace
    from app.modules.meetings.schemas.agent_roles import InsightOutput
    from pydantic import ValidationError
    provider = SimpleNamespace(run=AsyncMock(return_value={
        'insights': [], 'state': [], 'commitments': [], 'questions': []}))
    value = await MeetingAgents(provider).run_role('risk', {})
    assert value == {'insights': []} and provider.run.await_count == 1
    assert InsightOutput.model_validate(value).insights == []
    provider.run = AsyncMock(side_effect=[{'insights': [], 'commitments': [{'invalid': True}]}, {'insights': []}])
    assert await MeetingAgents(provider).run_role('risk', {}) == {'insights': []}
    assert provider.run.await_count == 2
    provider.run = AsyncMock(return_value={'insights': [], 'commitments': [{'invalid': True}]})
    with pytest.raises(ValidationError):
        await MeetingAgents(provider).run_role('risk', {})
    assert provider.run.await_count == 2
