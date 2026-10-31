"""跨会议日程去重以及争议暂停，不把撤回候选直接变成业务撤销。"""

from sqlalchemy import select
from tests.test_actions import prepared
from tests.test_meetings import meeting
from app.modules.actions.models import ActionRun
from app.modules.meetings.models.meeting import Meeting


async def test_same_calendar_in_another_meeting_reuses_action(client, app):
    headers, first = await prepared(client, app)
    source = meeting()
    source["id"] = "another-meeting"
    source["utterances"][0]["raw"] = "确定2030年10月23日14点到15点开进度会。"
    await client.post("/api/v1/meetings/sync", headers=headers, json=source)
    result = await client.post("/api/v1/meetings/another-meeting/analysis", headers=headers)
    assert result.status_code == 200, result.text
    actions = (await client.get("/api/v1/actions", headers=headers)).json()["data"]
    assert len(actions) == 1 and actions[0]["id"] == first[0]["id"]


async def test_pending_withdrawal_blocks_action_without_cancelling_business(client, app):
    headers, actions = await prepared(client, app)
    action = actions[0]
    async with app.state.database.sessions() as db:
        row = await db.get(ActionRun, action["id"])
        source = await db.get(Meeting, row.meeting_id)
        source.analysis = {**source.analysis, "withdrawal_candidates": [{"target_id": "u1"}]}
        await db.commit()
    result = await client.post(f"/api/v1/actions/{action['id']}/decision", headers=headers,
        json={"fingerprint": action["fingerprint"], "allow": True, "deviceId": "synthetic-test-device-1"})
    assert result.status_code == 409
    assert (await client.get("/api/v1/actions", headers=headers)).json()["data"][0]["status"] == "pending"
