"""复盘、跟进与备选建议的证据、权限、去重和业务不变性。"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from sqlalchemy import select, func
import pytest
from app.modules.followup.models import FollowupRun
from app.modules.followup.service import FollowupService
from app.modules.followup.context import week_window, phase
from app.modules.followup.agent import FollowupAgent
from app.modules.progress.models.progress_item import ProgressItem
from tests.test_meetings import account, meeting


async def fixture(client, app, status="active", confirmed=True):
    headers = await account(client)
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()["data"]["id"]
    key = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    async with app.state.database.sessions() as session:
        row = ProgressItem(
            id=key,
            owner_id=owner,
            meeting_id=None,
            source_key="manual:" + key,
            kind="goal",
            source_revision=-1,
            version=1,
            content={
                "title": "双端交付",
                "status": status,
                "deadline": (now + timedelta(hours=2)).isoformat(),
                "phase": "测试",
                "condition": "完成双端测试",
                "acceptance": "合同指标",
                "milestones": [],
            },
            overrides={},
            history=[],
        )
        if not confirmed:
            row.source_key = "unconfirmed:" + key
        session.add(row)
        await session.commit()
    return headers, owner, key


def provider():
    async def run(role, prompt, data):
        key = data["sources"][0]["id"]
        if role == "decision":
            return {
                "needed": True,
                "problem": "交付范围是否分期？",
                "options": [
                    {
                        "code": c,
                        "title": title,
                        "pros": "保持范围可核验",
                        "cons": "需要进一步确认",
                    }
                    for c, title in [
                        ("A", "维持双端"),
                        ("B", "单独评估桌面"),
                        ("C", "分期交付"),
                    ]
                ],
                "recommendation": "B",
                "reason": "先明确影响",
                "source_ids": [key],
                "done": ["确认范围"],
                "plan": ["评估影响"],
            }
        return {
            "findings": [
                {
                    "section": "plan",
                    "text": "建议先核对交付准备情况，不变更原定交期。",
                    "source_ids": [key],
                }
            ],
            "gaps": ["未读取完整手机日历。"],
        }

    return SimpleNamespace(run=AsyncMock(side_effect=run))


def test_phase_requires_confirmed_and_unfinished_item():
    now = datetime.now(timezone.utc)
    item = {
        "status": "pending",
        "deadline": (now + timedelta(hours=2)).isoformat(),
        "confirmed": False,
    }
    assert phase(item, now) is None
    item.update(confirmed=True, status="active")
    assert phase(item, now) == "due"
    item["status"] = "verified"
    assert phase(item, now) is None


def test_week_window_is_shanghai_monday_not_server_timezone():
    window = week_window(datetime(2026, 10, 18, 17, tzinfo=timezone.utc))
    assert window["plan_start"] == "2026-10-19T00:00:00+08:00"
    assert window["review_start"] == "2026-10-12T00:00:00+08:00"


async def test_weekly_real_database_archive_and_account_scope(client, app):
    headers, owner, key = await fixture(client, app)
    other = await account(client, "other@example.com")
    response = await client.post(
        "/api/v1/knowledge/followup", headers=headers, json={"kind": "weekly"}
    )
    assert response.status_code == 200, response.text
    again = await client.post(
        "/api/v1/knowledge/followup", headers=headers, json={"kind": "weekly"}
    )
    assert response.json()["data"]["id"] == again.json()["data"]["id"]
    p = provider()
    app.state.followup_jobs.agent.provider = p
    assert await app.state.followup_jobs.process_one()
    library = (await client.get("/api/v1/knowledge", headers=headers)).json()["data"][
        "snapshot"
    ]
    doc = library["documents"][0]
    assert doc["source"].startswith("assistant:") and doc["allowed"]
    assert (
        doc["versions"][0]["state"] == "review"
        and "未读取" in doc["versions"][0]["content"]
    )
    notices = (
        await client.get("/api/v1/knowledge/assistance", headers=headers)
    ).json()["data"]
    n = next(n for n in notices if n["kind"] == "followup")
    decision = {k: n[k] for k in ["id", "fingerprint"]} | {"allow": True}
    assert (
        await client.post(
            "/api/v1/knowledge/assistance/decision", headers=other, json=decision
        )
    ).status_code == 409
    allowed = await client.post(
        "/api/v1/knowledge/assistance/decision", headers=headers, json=decision
    )
    assert allowed.status_code == 200, allowed.text
    assert (
        await client.post(
            "/api/v1/knowledge/assistance/decision", headers=headers, json=decision
        )
    ).status_code == 200
    async with app.state.database.sessions() as session:
        row = await session.get(ProgressItem, key)
        assert (
            row.version == 1 and row.content["status"] == "active" and not row.overrides
        )
    assert p.run.await_count == 1


async def test_no_change_no_second_model_call_and_completion_hides_old_notice(
    client, app
):
    headers, owner, key = await fixture(client, app)
    await client.post(
        "/api/v1/knowledge/followup", headers=headers, json={"kind": "proactive"}
    )
    app.state.followup_jobs.agent.provider = provider()
    assert await app.state.followup_jobs.process_one()
    await client.post(
        "/api/v1/knowledge/followup", headers=headers, json={"kind": "proactive"}
    )
    assert not await app.state.followup_jobs.process_one()
    assert app.state.followup_jobs.agent.provider.run.await_count == 1
    async with app.state.database.sessions() as session:
        row = await session.get(ProgressItem, key)
        row.overrides = {"status": "verified"}
        row.version += 1
        await session.commit()
    assert (await client.get("/api/v1/knowledge/assistance", headers=headers)).json()[
        "data"
    ] == []


async def test_source_changes_during_model_run_are_not_published(client, app):
    headers, owner, key = await fixture(client, app)
    await client.post(
        "/api/v1/knowledge/followup", headers=headers, json={"kind": "proactive"}
    )
    original = provider()

    async def change(role, prompt, data):
        async with app.state.database.sessions() as session:
            row = await session.get(ProgressItem, key)
            row.version += 1
            row.overrides = {
                "deadline": (
                    datetime.now(timezone.utc) + timedelta(days=20)
                ).isoformat()
            }
            await session.commit()
        return await original.run(role, prompt, data)

    app.state.followup_jobs.agent.provider = SimpleNamespace(run=change)
    assert await app.state.followup_jobs.process_one()
    async with app.state.database.sessions() as session:
        row = await session.scalar(select(FollowupRun))
        assert row.status == "stale"
    assert (await client.get("/api/v1/knowledge/assistance", headers=headers)).json()[
        "data"
    ] == []


async def test_heartbeat_quiet_hours_and_read_poll_does_not_run_models(client, app):
    headers, owner, key = await fixture(client, app)
    app.state.followup_jobs.agent.provider = provider()
    await app.state.followup_jobs.scan(datetime(2026, 10, 18, 16, tzinfo=timezone.utc))
    async with app.state.database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(FollowupRun)) == 0
    for _ in range(3):
        await client.get("/api/v1/knowledge/assistance", headers=headers)
    assert app.state.followup_jobs.agent.provider.run.await_count == 0


async def test_review_rejects_forged_sources_and_unverified_wins():
    p = SimpleNamespace(
        run=AsyncMock(
            return_value={
                "findings": [
                    {"section": "wins", "text": "全部已完成", "source_ids": ["x"]},
                    {
                        "section": "plan",
                        "text": "伪造来源",
                        "source_ids": ["other-account"],
                    },
                ],
                "gaps": [],
            }
        )
    )
    result = await FollowupAgent(p).analyze(
        "weekly", {"sources": [{"id": "x", "status": "pending"}]}
    )
    assert result["findings"] == []


async def test_decision_uses_exactly_three_distinct_option_codes_and_citations():
    p = provider()
    result = await FollowupAgent(p).analyze("decision", {"sources": [{"id": "risk:x"}]})
    assert (
        len(result["options"]) == 3
        and result["recommendation"] == "B"
        and result["advisory"]
    )
    invalid = dict(result)
    invalid.pop("skill")
    invalid.pop("runtime")
    invalid.pop("advisory")
    invalid.pop("format_validation")
    invalid["options"][2]["code"] = "B"
    p.run = AsyncMock(return_value=invalid)
    with pytest.raises(ValueError):
        await FollowupAgent(p).analyze("decision", {"sources": [{"id": "risk:x"}]})


async def test_invalid_followup_format_is_corrected_once_without_relaxing_evidence():
    p = SimpleNamespace(run=AsyncMock(side_effect=[
        {"findings": [{"section": "计划", "text": "核对准备", "source_ids": ["x"]}], "gaps": []},
        {"findings": [{"section": "plan", "text": "核对准备", "source_ids": ["x"]}], "gaps": []},
    ]))
    result = await FollowupAgent(p).analyze("proactive", {"sources": [{"id": "x"}]})
    assert result["format_validation"]["calls"] == 2
    assert result["format_validation"]["corrected"] is True
    assert result["findings"][0]["source_ids"] == ["x"]
    assert p.run.await_count == 2
    assert "input" not in str(result["format_validation"])


async def test_invalid_followup_format_stops_after_one_correction():
    from app.modules.followup.agent import FollowupOutputError
    p = SimpleNamespace(run=AsyncMock(return_value={"findings": "private content"}))
    with pytest.raises(FollowupOutputError) as caught:
        await FollowupAgent(p).analyze("proactive", {"sources": [{"id": "x"}]})
    assert p.run.await_count == 2
    assert "private content" not in str(caught.value.diagnostics)


async def test_proactive_deadline_fact_uses_server_clock_instead_of_model_claim():
    p = SimpleNamespace(run=AsyncMock(return_value={"findings": [
        {"section": "stalled", "text": "已超过截止时间", "source_ids": ["x"]},
        {"section": "plan", "text": "建议核对准备", "source_ids": ["x"]},
    ], "gaps": []}))
    result = await FollowupAgent(p).analyze("proactive", {
        "assessment_at": "2026-10-20T05:30:00+00:00",
        "sources": [{"id": "x", "confirmed": True, "status": "pending",
                     "deadline": "2026-10-20T07:30:00+00:00"}],
    })
    assert result["findings"][0]["text"] == "该履约项将在24小时内到期，尚未核验完成。"
    assert all("已超过" not in f["text"] for f in result["findings"])
