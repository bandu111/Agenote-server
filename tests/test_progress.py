"""真实 PostgreSQL 验证进展投影、版本冲突、人工覆盖与跨账户隔离。"""

from copy import deepcopy
from unittest.mock import AsyncMock
import asyncio
import pytest
from test_meetings import account
from app.modules.progress.services.progress_service import deadline_from_text
from app.modules.progress.models.progress_item import ProgressItem
from app.modules.meetings.models.meeting import Meeting
from sqlalchemy import select


def meeting(revision=1):
    return {
        "id": "real-progress-session",
        "revision": revision,
        "title": "项目讨论",
        "startedAt": "2026-10-19T10:00:00+08:00",
        "elapsed": 10000,
        "utterances": [
            {
                "id": "g",
                "speaker": "发言人 A",
                "at": 0,
                "raw": "交付目标是完成双端测试。验收满足合同指标。交期2026年10月30日，里程碑范围确认、双端测试。",
            },
            {
                "id": "c",
                "speaker": "发言人 A",
                "at": 1000,
                "raw": "为完成双端测试，我方承诺周三提交报告。",
            },
        ],
    }


def report():
    return {
        "goals": [
            {
                "origin_id": "g",
                "title": "完成双端测试",
                "condition": "完成双端测试",
                "acceptance": "满足合同指标",
                "deadline_text": "2026年10月30日",
                "phase": None,
                "milestones": ["范围确认", "双端测试"],
                "evidence_ids": ["g"],
            }
        ],
        "commitments": [
            {
                "origin_id": "c",
                "action": "提交报告",
                "speaker": "发言人 A",
                "side": "ours",
                "status": "proposed",
                "deadline_text": "周三",
                "goal_origin_id": "g",
                "missing": ["责任来源待确认"],
                "events": [
                    {"kind": "proposed", "evidence_ids": ["c"], "summary": "待确认"}
                ],
            }
        ],
    }


async def projected(client, app, headers):
    await client.post("/api/v1/meetings/sync", headers=headers, json=meeting())
    app.state.meeting_analysis.agents.analyze = AsyncMock(return_value=report())
    response = await client.post(
        "/api/v1/meetings/real-progress-session/analysis", headers=headers
    )
    assert response.status_code == 200
    return (await client.get("/api/v1/progress", headers=headers)).json()["data"]


async def test_new_account_empty_no_demo_seed_and_no_anonymous_access(client):
    assert (await client.get("/api/v1/progress")).status_code == 401
    headers = await account(client)
    data = (await client.get("/api/v1/progress", headers=headers)).json()["data"]
    assert data == {"goals": [], "commitments": [], "artifacts": [], "pending": []}


async def test_goal_removal_is_owner_scoped_versioned_persistent_and_keeps_related_records(client, app):
    headers = await account(client)
    data = await projected(client, app, headers)
    goal = data['goals'][0]
    path = f"/api/v1/progress/goals/{goal['id']}/remove"
    assert (await client.post(path, json={'version': goal['version']})).status_code == 401
    other = await account(client, 'goal-remove-other@example.com')
    assert (await client.post(path, headers=other, json={'version': goal['version']})).status_code == 404
    assert (await client.post(path, headers=headers, json={'version': goal['version'] + 100})).status_code == 409
    response = await client.post(path, headers=headers, json={'version': goal['version']})
    assert response.status_code == 200
    removed = response.json()['data']
    assert removed['removed'] is True and removed['status'] == 'paused'
    assert removed['history'][-1]['changes']['removed'] is True
    again = await client.post(path, headers=headers, json={'version': goal['version']})
    assert again.status_code == 200 and again.json()['data']['version'] == removed['version']
    fresh = (await client.get('/api/v1/progress', headers=headers)).json()['data']
    assert fresh['goals'] == []
    assert not any(p['id'] == goal['id'] for p in fresh['pending'])
    assert len(fresh['commitments']) == len(data['commitments'])
    assert len(fresh['artifacts']) == len(data['artifacts'])
    await client.post('/api/v1/meetings/real-progress-session/analysis', headers=headers)
    assert (await client.get('/api/v1/progress', headers=headers)).json()['data']['goals'] == []
    assert (await client.post('/api/v1/progress/goals/' + goal['id'], headers=headers, json={
        'version': removed['version'], 'note': '旧客户端尝试改回',
        'goal': {'title': goal['title'], 'condition': goal['condition'], 'status': 'active'},
    })).status_code == 409


def test_removed_goal_is_excluded_from_weekly_rhythm_and_continuity():
    from types import SimpleNamespace
    from datetime import datetime, timezone, timedelta
    from app.modules.home.services.weekly_rhythm import weekly_rhythm
    from app.modules.progress.services.goal_continuity import ongoing
    now = datetime(2026, 10, 31, 12, tzinfo=timezone(timedelta(hours=8)))
    meeting_row = SimpleNamespace(id='m', client_id='session', revision=1, content={
        'started_at': now.isoformat(), 'ended': True, 'utterances': [{'raw': '项目讨论'}]})
    active = {'kind': 'goal', 'status': 'active', 'sessionId': 'session', 'missing': ['交期']}
    baseline = weekly_rhythm([meeting_row], {}, [active], [], now=now)['days'][-1]
    assert baseline['activeGoals'] == 1 and baseline['missingItems'] == 1
    removed = {**active, 'removed': True}
    day = weekly_rhythm([meeting_row], {}, [removed], [], now=now)['days'][-1]
    assert day['activeGoals'] == day['missingItems'] == 0
    assert not ongoing(removed, now)


async def test_analysis_transaction_projects_real_goals_commitments_and_idempotency(
    client, app
):
    headers = await account(client)
    data = await projected(client, app, headers)
    assert len(data["goals"]) == len(data["commitments"]) == 1
    goal, item = data["goals"][0], data["commitments"][0]
    assert item["goalId"] == goal["id"]
    assert item["status"] == "pending"
    assert item["person"] == "发言人 A" and not item["evidence"]["speakerConfirmed"]
    assert item["deadline"].startswith("2026-10-21")
    assert goal["deadline"].startswith("2026-10-30")
    assert all(m["status"] == "pending" for m in goal["milestones"])
    await client.post(
        "/api/v1/meetings/real-progress-session/analysis", headers=headers
    )
    again = (await client.get("/api/v1/progress", headers=headers)).json()["data"]
    assert again == data
    other = await account(client, "other-progress@example.com")
    assert (await client.get("/api/v1/progress", headers=other)).json()["data"][
        "goals"
    ] == []
    assert (
        await client.post(
            f"/api/v1/progress/commitments/{item['id']}/status",
            headers=other,
            json={"version": 1, "status": "planned", "note": "越权尝试"},
        )
    ).status_code == 404


async def test_human_changes_persist_after_reanalysis_and_stale_clients_cannot_overwrite(
    client, app, monkeypatch
):
    # This scenario checks confirmation/versioning, not expiry. Keep its fixed
    # 2026 meeting fixture before the deadline regardless of the wall clock.
    from datetime import datetime as RealDateTime, timezone
    from app.modules.progress.services import progress_service
    class FixtureTime(RealDateTime):
        @classmethod
        def now(cls, tz=None):
            return RealDateTime(2026, 10, 19, 12, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(progress_service, 'datetime', FixtureTime)
    headers = await account(client)
    data = await projected(client, app, headers)
    item = data["commitments"][0]
    path = f"/api/v1/progress/commitments/{item['id']}"
    first = await client.post(
        path + "/status",
        headers=headers,
        json={
            "version": item["version"],
            "status": "planned",
            "note": "人工核对原话确认承诺",
        },
    )
    assert first.status_code == 200
    assert (
        await client.post(
            path + "/status",
            headers=headers,
            json={"version": 1, "status": "withdrawn", "note": "旧客户端"},
        )
    ).status_code == 409
    second = await client.post(
        path + "/reschedule",
        headers=headers,
        json={
            "version": 2,
            "deadline": "2026-11-02T18:00:00+08:00",
            "reason": "双方确认改期",
        },
    )
    assert second.status_code == 200
    await client.post("/api/v1/meetings/sync", headers=headers, json=meeting(2))
    await client.post(
        "/api/v1/meetings/real-progress-session/analysis", headers=headers
    )
    current = (await client.get("/api/v1/progress", headers=headers)).json()["data"][
        "commitments"
    ][0]
    assert current["status"] == "planned" and current["deadline"].startswith(
        "2026-11-02"
    )
    assert len(current["history"]) == 2
    assert current["evidence"]["transcript"] == meeting()['utterances'][1]['raw']
    assert (
        await client.post(
            path + "/status",
            headers=headers,
            json={
                "version": current["version"],
                "status": "verified",
                "note": "跳过完成声明",
            },
        )
    ).status_code == 409
    results = await asyncio.gather(
        *[
            client.post(
                path + "/status",
                headers=headers,
                json={
                    "version": current["version"],
                    "status": "claimed",
                    "note": "自述已提交",
                },
            )
            for _ in range(2)
        ]
    )
    assert sorted(r.status_code for r in results) == [200, 409]


async def test_incomplete_and_fabricated_goals_are_kept_out_of_renderable_lists(
    client, app
):
    headers = await account(client)
    value = report()
    value["goals"][0]["condition"] = "模型编造内容"
    value["commitments"][0].update(side="unknown", deadline_text=None)
    payload = meeting()
    # 测试真正缺失日期的原话，不能只把模型日期置空但仍在原话中留下明确交期。
    payload["utterances"][1]["raw"] = "我方承诺提交报告。"
    await client.post("/api/v1/meetings/sync", headers=headers, json=payload)
    app.state.meeting_analysis.agents.analyze = AsyncMock(return_value=value)
    await client.post(
        "/api/v1/meetings/real-progress-session/analysis", headers=headers
    )
    data = (await client.get("/api/v1/progress", headers=headers)).json()["data"]
    assert data["goals"] == data["commitments"] == []
    assert len(data["pending"]) == 1 and data["pending"][0]["deadline"] is None


async def test_earlier_goal_link_survives_when_goal_is_outside_recent_analysis(
    client, app
):
    headers = await account(client)
    original = await projected(client, app, headers)
    value = report()
    value["goals"] = []
    app.state.meeting_analysis.agents.analyze = AsyncMock(return_value=value)
    await client.post("/api/v1/meetings/sync", headers=headers, json=meeting(2))
    await client.post(
        "/api/v1/meetings/real-progress-session/analysis", headers=headers
    )
    result = (await client.get("/api/v1/progress", headers=headers)).json()["data"]
    assert result["commitments"][0]["goalId"] == original["goals"][0]["id"]
    assert result["goals"][0]["id"] == original["goals"][0]["id"]


async def test_explicit_goal_creation_update_and_account_isolation(client):
    headers = await account(client)
    payload = {
        "title": "真实业务目标",
        "condition": "完成验收",
        "deadline": "2026-11-01T18:00:00+08:00",
        "milestones": [{"title": "验收", "weight": 1}],
    }
    response = await client.post(
        "/api/v1/progress/goals", headers=headers, json=payload
    )
    assert response.status_code == 201
    goal = response.json()["data"]
    update = await client.post(
        "/api/v1/progress/goals/" + goal["id"],
        headers=headers,
        json={
            "version": 1,
            "note": "人工验证完成",
            "goal": {
                **payload,
                "status": "completed",
                "milestones": [{"title": "验收", "weight": 1, "status": "completed"}],
            },
        },
    )
    assert update.status_code == 200
    assert (await client.get("/api/v1/progress", headers=headers)).json()["data"][
        "goals"
    ][0]["status"] == "completed"
    assert (
        await client.post(
            "/api/v1/progress/goals",
            headers=headers,
            json={**payload, "owner_id": "spoof"},
        )
    ).status_code == 422


@pytest.mark.parametrize("value", ["下次会议", "尽快", "2026年2月30日", "明天或后天"])
def test_ambiguous_deadline_is_not_fabricated(value):
    assert deadline_from_text(value, "2026-10-19T10:00:00+08:00") is None


async def test_complete_missing_partial_then_ready_without_guessing_or_confirming(
    client, app
):
    headers = await account(client)
    value = report()
    value["commitments"][0].update(
        side="unknown", deadline_text=None, missing=["履约方待明确", "交付时间待明确"]
    )
    app.state.meeting_analysis.agents.analyze = AsyncMock(return_value=value)
    payload = meeting()
    payload["utterances"][1]["raw"] = "我方承诺提交报告。"
    await client.post("/api/v1/meetings/sync", headers=headers, json=payload)
    await client.post(
        "/api/v1/meetings/real-progress-session/analysis", headers=headers
    )
    before = (await client.get("/api/v1/progress", headers=headers)).json()["data"]
    item = before["pending"][0]
    path = f"/api/v1/progress/{item['id']}/complete"
    assert (
        await client.post(path, headers=headers, json={"version": 1, "side": "theirs"})
    ).status_code == 200
    partial = (await client.get("/api/v1/progress", headers=headers)).json()["data"][
        "pending"
    ][0]
    assert partial["side"] == "theirs" and partial["deadline"] is None
    assert (
        await client.post(
            path,
            headers=headers,
            json={"version": 1, "deadline": "2026-11-01T18:00:00+08:00"},
        )
    ).status_code == 409
    assert (
        await client.post(
            path,
            headers=headers,
            json={"version": 2, "deadline": "2026-11-01T18:00:00+08:00"},
        )
    ).status_code == 200
    after = (await client.get("/api/v1/progress", headers=headers)).json()["data"]
    assert after["pending"] == []
    saved = after["commitments"][0]
    assert saved["side"] == "theirs" and saved["status"] == "pending"
    assert saved["evidence"]["transcript"] == item["evidence"]["transcript"]
    assert "履约方待明确" not in saved["missing"] and "交付时间待明确" not in saved["missing"]
    assert "是否承诺待确认" in saved["missing"] and len(saved["history"]) == 2
    await client.post("/api/v1/meetings/sync", headers=headers, json=meeting(2))
    await client.post(
        "/api/v1/meetings/real-progress-session/analysis", headers=headers
    )
    assert (await client.get("/api/v1/progress", headers=headers)).json()["data"][
        "commitments"
    ][0]["side"] == "theirs"
    other = await account(client, "completion-other@example.com")
    assert (
        await client.post(path, headers=other, json={"version": 3, "side": "ours"})
    ).status_code == 404
    assert (
        await client.post(path, headers=headers, json={"version": 3, "side": "ours"})
    ).status_code == 409


async def test_complete_goal_date_and_reject_invented_party_or_unzoned_date(client):
    headers = await account(client)
    goal = (
        await client.post(
            "/api/v1/progress/goals",
            headers=headers,
            json={"title": "待补交期目标", "condition": "完成交付"},
        )
    ).json()["data"]
    path = f"/api/v1/progress/{goal['id']}/complete"
    assert (
        await client.post(path, headers=headers, json={"version": 1})
    ).status_code == 422
    assert (
        await client.post(path, headers=headers, json={"version": 1, "side": "ours"})
    ).status_code == 409
    assert (
        await client.post(
            path, headers=headers, json={"version": 1, "side": "技术负责人"}
        )
    ).status_code == 422
    assert (
        await client.post(
            path,
            headers=headers,
            json={"version": 1, "deadline": "2026-11-01T18:00:00"},
        )
    ).status_code == 422
    assert (
        await client.post(
            path,
            headers=headers,
            json={"version": 1, "deadline": "2026-11-01T18:00:00+08:00"},
        )
    ).status_code == 200
    assert (
        len(
            (await client.get("/api/v1/progress", headers=headers)).json()["data"][
                "goals"
            ]
        )
        == 1
    )


async def test_archive_enters_notification_data_and_approval_is_version_bound(
    client, app
):
    headers = await account(client)
    await projected(client, app, headers)
    async with app.state.database.sessions() as db:
        meeting_row = await db.scalar(select(Meeting))
        owner_id = meeting_row.owner_id
        # 已有成果生产者写入待确认版本，不通过公开接口伪造审批对象。
        db.add(
            ProgressItem(
                id="approval-test",
                owner_id=owner_id,
                meeting_id=meeting_row.id,
                source_key="artifact:approval-test",
                kind="artifact",
                source_revision=1,
                version=1,
                overrides={},
                history=[],
                content={
                    "title": "测试报告",
                    "summary": "待确认报告",
                    "versionLabel": "r1",
                    "createdAt": meeting_row.content["started_at"],
                    "status": "confirmation",
                    "content": "真实版本内容",
                    "risk": "",
                    "goalId": "",
                },
            )
        )
        await db.commit()
    path = "/api/v1/progress/artifacts/approval-test/approve"
    assert (
        await client.post(
            path,
            headers=headers,
            json={"version": 1, "artifactVersion": "r0", "note": "过期确认"},
        )
    ).status_code == 409
    assert (
        await client.post(
            path,
            headers=headers,
            json={"version": 1, "artifactVersion": "r1", "note": "用户确认当前报告"},
        )
    ).status_code == 200
    data = (await client.get("/api/v1/progress", headers=headers)).json()["data"]
    assert data["artifacts"][0]["status"] == "approved"
    assert (
        await client.post(
            path,
            headers=headers,
            json={"version": 2, "artifactVersion": "r1", "note": "不能重复确认"},
        )
    ).status_code == 409
