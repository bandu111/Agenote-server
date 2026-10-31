"""履约投影不会把候选、相似短词或未验收目标自动批准。"""
from app.modules.progress.services.goal_progress import milestone_projection


def test_all_related_commitments_must_be_verified():
    milestones = [{"title": "双端测试报告", "weight": 1, "status": "pending"}]
    base = {"id": "a", "version": 2, "title": "提交双端测试报告", "status": "verified"}
    active = {**base, "id": "b", "status": "active"}
    assert milestone_projection(milestones, [base, active])["milestones"][0]["status"] == "active"
    assert milestone_projection(milestones, [base])["milestones"][0]["status"] == "completed"
    assert milestones[0]["status"] == "pending"
    assert milestone_projection(milestones, [{**base, "title": "测试"}])["milestones"][0]["status"] == "pending"


async def test_goal_progress_updates_from_human_verified_commitment(client, app):
    from test_meetings import account
    from test_progress import projected
    from app.modules.progress.models.progress_item import ProgressItem
    headers = await account(client)
    data = await projected(client, app, headers)
    goal, commitment = data["goals"][0], data["commitments"][0]
    async with app.state.database.sessions() as session:
        row = await session.get(ProgressItem, commitment["id"])
        row.content = {**row.content, "title": "完成双端测试"}
        await session.commit()
    for status in ["planned", "active", "claimed", "verified"]:
        response = await client.post(f"/api/v1/progress/commitments/{commitment['id']}/status",
            headers=headers, json={"version": commitment["version"], "status": status, "note": "用户核对依据"})
        assert response.status_code == 200
        commitment = response.json()["data"]
    current = (await client.get("/api/v1/progress", headers=headers)).json()["data"]["goals"][0]
    assert current["milestones"][1]["status"] == "completed"
    assert current["milestones"][0]["status"] == "pending"
    assert current["status"] == "active"
    assert current["history"][-1]["basis"][0]["id"] == commitment["id"]
    from app.modules.meetings.models.meeting import Meeting
    from app.modules.progress.services.progress_service import ProgressService
    from app.modules.progress.services.goal_continuity import continue_explicit_goals
    from app.modules.progress.services.goal_progress import refresh_goal_progress
    async with app.state.database.sessions() as session:
        g = await session.get(ProgressItem, goal['id'])
        version = g.version
        source = await session.get(Meeting, g.meeting_id)
        await continue_explicit_goals(ProgressService(session), source)
        await refresh_goal_progress(session, g.owner_id)
        assert g.version == version  # 无新进度不反复撤掉再重建核验投影。
