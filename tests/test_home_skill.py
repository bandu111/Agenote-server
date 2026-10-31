"""首页真实投影、租户隔离及官方 Skill 适配的回归验证。"""

from copy import deepcopy
import pytest
from sqlalchemy import select
from app.integrations.hermes.meeting_skill import meeting_skill
from app.modules.home.services.home_service import build_home
from app.modules.meetings.models.meeting import Meeting
from test_meetings import account
from test_progress import projected, meeting as progress_meeting
from test_commitment_agent import source, candidate, event, checked


def test_pinned_skill_is_extraction_only_and_dates_not_invented():
    prompt, meta = meeting_skill()
    assert meta["name"] == "meeting-action-items" and len(meta["commit"]) == 40
    assert "proposals not decided" in prompt
    assert "never invent one" in prompt
    assert "### 4." not in prompt
    assert "不执行" in prompt


@pytest.mark.parametrize("model_date", [None, "2030-10-28"])
def test_explicit_date_survives_missing_or_normalized_model_field(model_date):
    sources = [source("u1", "我方负责2030年10月28日提交双端测试报告。", separated=False),
               source("u2", "可以考虑改为2030年10月30日提交，但还没确定。", separated=False)]
    item = candidate([event("committed", "u1", model_date), event("proposed", "u2", "2030年10月30日")],
                     action="提交双端测试报告", action_quote="提交双端测试报告")
    # 未分离仍然保持候选，但不能因后续提议覆盖起源原话的日期。
    result = checked(item, sources)[0]
    assert result["deadline_text"] == "2030年10月28日"
    assert result["status"] == "proposed"
    assert "交付时间待明确" not in result["missing"]


@pytest.mark.parametrize("raw", [
    "可以考虑2030年10月28日提交双端测试报告。",
    "我们不负责2030年10月28日提交双端测试报告。",
    "我方负责2030年2月30日提交双端测试报告。",
    "我方负责2030年10月28日或2030年10月30日提交双端测试报告。",
    "我方负责2030年10月28日提交双端测试报告，2030年10月30日提供方案。",
])
def test_ambiguous_or_invalid_date_not_recovered(raw):
    result = checked(candidate([event("proposed", "u1")], action="提交双端测试报告"), [source("u1", raw)])[0]
    assert result["deadline_text"] is None


def test_candidate_not_scheduled_and_calendar_execution_not_meeting_completion():
    item = {"kind": "commitment", "id": "c", "title": "提交报告", "version": 1,
            "deadline": "2030-10-28T23:59:00+08:00", "status": "pending", "side": "ours",
            "authorizationStatus": "pending"}
    action = {"id": "a", "version": 1, "status": "pending", "title": "进度会",
              "start": "2030-10-28T14:00:00+08:00", "end": "2030-10-28T15:00:00+08:00"}
    original = deepcopy([item, action])
    result = build_home([item], [action])
    assert result["schedules"] == []
    assert any(decision["id"] == "action:a" for decision in result["decisions"])
    # A legacy completed row still has no verified authority marker.
    action["status"] = "completed"
    result = build_home([item], [action])
    assert result["schedules"] == []
    action.update(authority="authenticated_device_receipt_fields_matched",
                  verifiedAt="2030-10-01T00:00:00+00:00", timeZone="Asia/Shanghai", agenda=None)
    result = build_home([item], [action])
    assert len(result["schedules"]) == 1
    assert result["schedules"][0]["completed"] is False
    assert result["modelCallsOnRead"] == 0
    assert item == original[0]


async def test_home_api_reads_progress_isolates_owner_and_omits_stale_analysis(client, app):
    assert (await client.get("/api/v1/home")).status_code == 401
    first = await account(client)
    second = await account(client, "home-other@example.com")
    data = await projected(client, app, first)
    result = await client.get("/api/v1/home", headers=first)
    assert result.status_code == 200
    home = result.json()["data"]
    # The fixture deadline may already be past on the machine running the suite;
    # an expired goal correctly falls back to the current meeting title.
    assert home["title"] in {data["goals"][0]["title"], progress_meeting()["title"]}
    assert home["schedules"] == []  # 模型候选不能冒充已排期。
    other = (await client.get("/api/v1/home", headers=second)).json()["data"]
    assert other["rows"] == other["decisions"] == other["schedules"] == []
    async with app.state.database.sessions() as session:
        row = await session.scalar(select(Meeting).where(Meeting.client_id == "real-progress-session"))
        row.analysis = {**row.analysis, "knowledge_sources": [{"nodeId": "stale-source"}], "knowledge_revision": -1}
        await session.commit()
    changed = (await client.get("/api/v1/home", headers=first)).json()["data"]
    assert changed["sessionId"] is None
    # 后续转写已变，旧风险不得继续标为当前会议结果。
    await client.post("/api/v1/meetings/sync", headers=first, json=progress_meeting(2))
    stale = (await client.get("/api/v1/home", headers=first)).json()["data"]
    assert stale["sessionId"] is None
    assert stale["modelCallsOnRead"] == 0


async def test_manual_goal_date_appears_without_fabricating_completion(client):
    headers = await account(client)
    response = await client.post("/api/v1/progress/goals", headers=headers, json={
        "title": "真实手工目标", "condition": "完成测试", "deadline": "2030-10-28T18:00:00+08:00"})
    assert response.status_code == 201
    data = (await client.get("/api/v1/home", headers=headers)).json()["data"]
    assert data["schedules"][0]["title"] == "真实手工目标"
    assert data["schedules"][0]["completed"] is False
