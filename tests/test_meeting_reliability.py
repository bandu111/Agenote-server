"""用户报告的业务边界：撤回、统一审查、按需调用及持久上下文。"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.modules.meetings.services.meeting_agents import (
    MeetingAgents,
    explicit_change,
    select_context,
    validate_report,
)
from app.modules.meetings.schemas.meeting import AgentReport
from app.modules.meetings.services.business_guard import guarded_report, fingerprint
from tests.test_commitment_agent import source, candidate, event
from tests.test_progress import projected, account, report, meeting


@pytest.mark.parametrize(
    "text,expected",
    [
        ("我们已经确认，取消原计划", True),
        ("可以考虑改为下周三，但还没确定", False),
        ("之前的合同取消不了", False),
        ("请不要改成 Windows", False),
        ("我们改成 Web，现在确认", True),
        ("如果取消原合同会怎么样？", False),
        ("不要再撤回刚才的决定", False),
    ],
)
def test_user_examples_are_only_candidate_filters(text, expected):
    assert explicit_change(text) is expected
    raw = [source("u1", "原目标是交付Android"), source("u2", text)]
    result = validate_report(
        AgentReport.model_validate(
            {
                "withdrawals": [
                    {"target_id": "u1", "evidence_ids": ["u2"], "reason": "模型判断"}
                ]
            }
        ),
        raw,
    )
    assert result["withdrawals"] == []
    assert bool(result["withdrawal_candidates"]) is expected


async def test_normal_chat_only_runs_light_state_and_reuses_graph():
    provider = SimpleNamespace(
        run=AsyncMock(return_value={}),
        settings=SimpleNamespace(deepseek_model="test-only-model"),
    )
    agent = MeetingAgents(provider)
    graph = agent.graph
    result = await agent.analyze([source("u1", "大家好，先喝水休息一下。")])
    assert provider.run.await_count == 1
    assert provider.run.call_args.args[0] == "state"
    assert result["agents_completed"] == ["state"]
    assert result["runtime"]["model"] == "test-only-model"
    await agent.analyze([source("u1", "声音清楚。")])
    assert agent.graph is graph


@pytest.mark.parametrize(
    "accept,wrong_digest,expected",
    [(False, False, 0), (True, True, 0), (True, False, 1)],
)
async def test_commitment_requires_exact_final_review(accept, wrong_digest, expected):
    async def reply(role, _prompt, payload):
        if role == "commitment":
            return {"commitments": [candidate([event("committed", "u1")])]}
        if role == "verify":
            assert payload["commitments"]
            return {
                "reviews": [
                    {
                        "id": v["id"],
                        "digest": "0" * 64 if wrong_digest else v["digest"],
                        "accepted": accept,
                    }
                    for v in payload["business_candidates"]
                ]
            }
        return {}

    agent = MeetingAgents(SimpleNamespace(run=AsyncMock(side_effect=reply)))
    result = await agent.analyze([source("u1", "我方负责提交评估报告。")])
    assert len(result["commitments"]) == expected


def test_persistent_early_decision_added_back_after_40_turns():
    raw = [source("u1", "合同范围仅限Android与iOS。")]
    raw += [source(f"u{i}", "接下来讨论其他内容。") for i in range(2, 55)]
    context = {
        "previous_analysis": {
            "state": [
                {
                    "topic": "范围",
                    "value": "仅限Android与iOS",
                    "status": "proposed",
                    "evidence_ids": ["u1"],
                }
            ]
        }
    }
    assert any(u["id"] == "u1" for u in select_context(raw, context))


@pytest.mark.parametrize(
    "actor,text,expected",
    [
        ("a", "接回验收，验收周三确认。", True),
        ("b", "接回验收，验收周三确认。", False),
        ("a", "接回验收，验收周五确认。", False),
        ("a", "预算周三确认。", False),
    ],
)
def test_repair_requires_same_source_topic_and_exact_later_quote(actor, text, expected):
    raw = [source("u1", "验收……"), source("u2", text, speaker=actor)]
    original = deepcopy(raw)
    report = AgentReport.model_validate(
        {
            "repairs": [
                {
                    "target_id": "u1",
                    "text": "周三确认",
                    "start": 0,
                    "end": 0,
                    "evidence_ids": ["u1", "u2"],
                }
            ]
        }
    )
    assert bool(validate_report(report, raw)["repairs"]) is expected
    assert raw == original


async def test_untrusted_model_cannot_cancel_manual_versioned_commitment(client, app):
    headers = await account(client)
    data = await projected(client, app, headers)
    item = data["commitments"][0]
    path = f"/api/v1/progress/commitments/{item['id']}/status"
    confirmed = await client.post(
        path,
        headers=headers,
        json={
            "version": item["version"],
            "status": "planned",
            "note": "用户核对当前承诺",
        },
    )
    assert confirmed.status_code == 200
    newer = meeting(2)
    newer["utterances"].append(
        source("u2", "可以考虑取消提交报告，但还没确定。", separated=False)
    )
    await client.post("/api/v1/meetings/sync", headers=headers, json=newer)
    forged = report()
    forged["commitments"][0]["status"] = "withdrawn"
    forged["commitments"][0]["events"].append(
        {"kind": "withdrawn", "summary": "模型误判取消", "evidence_ids": ["u2"]}
    )
    forged["withdrawals"] = [
        {"target_id": "c", "evidence_ids": ["u2"], "reason": "误判"}
    ]
    app.state.meeting_analysis.agents.analyze = AsyncMock(return_value=forged)
    analyzed = await client.post(
        "/api/v1/meetings/real-progress-session/analysis", headers=headers
    )
    assert analyzed.status_code == 200, analyzed.text
    assert analyzed.json()["data"]["analysis"]["withdrawals"] == []
    current = (await client.get("/api/v1/progress", headers=headers)).json()["data"][
        "commitments"
    ][0]
    assert current["status"] == "planned"
    # 旧版本的人工撤回同样不能覆盖刚才的确认。
    assert (
        await client.post(
            path,
            headers=headers,
            json={"version": 1, "status": "withdrawn", "note": "旧版本"},
        )
    ).status_code == 409


def test_unseparated_memory_is_a_cited_candidate_never_an_inline_repair():
    raw = [source("u1", "验收我们先按"), source("u2", "接着刚才验收，周三确认。")]
    for item in raw:
        item["speaker_id"] = "unseparated"
        item["speaker_separated"] = False
    report = AgentReport.model_validate({"repairs": [{"target_id":"u1", "text":"周三确认", "start":0,"end":0,"evidence_ids":["u1","u2"]}]})
    result = validate_report(report, raw)
    assert result["repairs"] == []
    candidate = result["insights"][0]
    assert candidate["kind"] == "stitch"
    assert candidate["evidence_ids"] == ["u1", "u2"]
    assert "未补写原句" in candidate["body"]
    raw[1]["raw"] = "预算下周五确认"
    assert validate_report(report, raw)["insights"] == []
