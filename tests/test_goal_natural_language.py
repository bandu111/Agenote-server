"""不靠“目标”二字召回，同时保留提议、否定和歧义的业务边界。"""
from copy import deepcopy
from unittest.mock import AsyncMock
import pytest
from app.modules.progress.services.goal_evidence import supported_goal, explicit_milestones
from app.modules.progress.services.goal_progress import milestone_projection
from test_meetings import account
from test_progress import meeting, report


@pytest.mark.parametrize("raw,quote,expected", [
    ("本期交付移动应用，验收通过双端测试。", "交付移动应用", True),
    ("我们交付Android和iOS到预发环境。", "交付Android和iOS到预发环境", True),
    ("考虑本期交付移动应用，但还没确定。", "交付移动应用", False),
    ("本期不要交付Windows应用。", "交付Windows应用", False),
    ("如果本期交付移动应用，就增加预算。", "交付移动应用", False),
    ("本期交付移动应用吗？", "交付移动应用", False),
    ("我方周三提交报告。", "提交报告", False),
])
def test_natural_deliverable_is_candidate_not_approval(raw, quote, expected):
    assert supported_goal({"origin_id": "u", "evidence_ids": ["u"], "condition": quote},
        {"u": {"raw": raw}}) is expected


def test_explicit_milestone_reference_supports_differently_worded_action():
    goal = {"milestones": [{"title": "双端联调验收", "weight": 1, "status": "pending"}]}
    names = explicit_milestones(goal, ["针对双端联调验收，我方会提供检查清单。"])
    commitment = {"id": "c", "title": "提供检查清单", "status": "verified", "version": 2, "milestoneTitles": names}
    # 明确引用支持关联与推进，但提交清单不等于完成整个联调验收。
    assert milestone_projection(goal["milestones"], [commitment])["milestones"][0]["status"] == "active"
    assert explicit_milestones(goal, ["不要完成双端联调验收。"] ) == []


def test_one_verified_item_cannot_complete_multiple_ambiguous_nodes():
    milestones = [{"title": t, "weight": 1, "status": "pending"} for t in ["双端测试", "双端测试报告"]]
    c = {"id": "c", "title": "提交双端测试报告", "status": "verified", "version": 1}
    assert [m["status"] for m in milestone_projection(milestones, [c])["milestones"]] == ["pending", "completed"]
    c["milestoneTitles"] = ["双端测试", "双端测试报告"]
    assert all(m["status"] == "pending" for m in milestone_projection(milestones, [c])["milestones"])


def test_verified_report_is_not_verified_test_or_different_programming_language():
    from app.modules.progress.services.goal_progress import related
    assert not related("双端测试", "提交双端测试报告")
    assert related("双端测试报告", "提交双端测试报告")
    assert related("双端测试", "完成双端测试")
    assert not related("部署C++服务", "部署C#服务")


async def test_natural_goal_projects_and_unique_source_link_survives_reanalysis(client, app):
    headers = await account(client)
    value, analysis = meeting(), report()
    value["utterances"][0]["raw"] = "本期完成双端测试。验收满足合同指标。交期2026年10月30日，里程碑范围确认、双端测试。"
    value["utterances"][1]["raw"] = "针对完成双端测试，我方承诺周三提交报告。"
    analysis["commitments"][0]["goal_origin_id"] = None
    app.state.meeting_analysis.agents.analyze = AsyncMock(return_value=analysis)
    for revision in [1, 2]:
        value["revision"] = revision
        assert (await client.post('/api/v1/meetings/sync', headers=headers, json=value)).status_code == 200
        assert (await client.post('/api/v1/meetings/real-progress-session/analysis', headers=headers)).status_code == 200
        data = (await client.get('/api/v1/progress', headers=headers)).json()["data"]
        assert len(data["goals"]) == len(data["commitments"]) == 1
        assert data["commitments"][0]["goalId"] == data["goals"][0]["id"]
        assert data["commitments"][0]["goalLinkBasis"] == "explicit_source_quote"
        assert data["goals"][0]["status"] == "pending"
def test_sentence_ending_punctuation_is_still_grounded():
    from app.modules.progress.services.goal_evidence import certain_clause
    assert certain_clause('本期交付移动端应用。验收待核对。', '本期交付移动端应用。')
    assert not certain_clause('可以考虑本期交付移动端应用。', '本期交付移动端应用。')
    assert not certain_clause('本期交付移动端应用？', '本期交付移动端应用？')

