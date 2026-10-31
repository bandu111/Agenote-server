"""承诺 Agent 的证据、匿名身份与状态提升边界。"""

from copy import deepcopy
from unittest.mock import AsyncMock
import pytest
from app.modules.meetings.services.commitment_agent import (
    CommitmentAgent,
    CommitmentDraft,
    validate_commitments,
)
from app.modules.meetings.services.meeting_agents import MeetingAgents
from test_meetings import account, meeting
from app.modules.meetings.services.transcript_polish_service import checked_polish


def source(key, text, speaker="a", separated=True):
    return {
        "id": key,
        "raw": text,
        "speaker": f"发言人 {speaker.upper()}",
        "speaker_id": speaker,
        "speaker_separated": separated,
        "at": int(key[1:]) * 1000,
    }


def test_polish_removes_only_separated_fillers_and_keeps_standalone_response():
    assert checked_polish(
        "嗯，嗯，我们我们周三提交。", "嗯，嗯，我们我们周三提交。"
    ) == ("我们周三提交。", True)
    assert checked_polish("嗯。", "嗯。") == ("嗯。", True)


def candidate(events, **extra):
    return {
        "origin_id": "u1",
        "speaker_id": "a",
        "action": "提交评估报告",
        "events": events,
        **extra,
    }


def event(kind, key, deadline=None):
    return {
        "kind": kind,
        "evidence_ids": [key],
        "summary": "原话对应的变化",
        "deadline_text": deadline,
    }


def checked(item, sources):
    return validate_commitments(
        CommitmentDraft.model_validate({"commitments": [item]}), sources
    )


def test_anonymous_commitment_reschedule_and_withdrawal_keep_evidence():
    sources = [
        source("u1", "我方负责提交评估报告，周三交付。"),
        source("u2", "把交期改为周五，报告仍由我负责。"),
        source("u3", "撤回刚才提交报告的承诺。"),
    ]
    original = deepcopy(sources)
    item = candidate(
        [
            event("committed", "u1", "周三"),
            event("rescheduled", "u2", "周五"),
            event("withdrawn", "u3"),
        ],
        side="ours",
        side_evidence_ids=["u1"],
    )
    result = checked(item, sources)[0]
    assert result["status"] == "committed"
    assert result["deadline_text"] == "周三"
    assert len(result["pending_changes"]) == 2
    assert all(c["authorization_status"] == "pending" for c in result["pending_changes"])
    assert result["side"] == "ours"
    assert result["verification_status"] == "not_verified"
    assert result["identity_status"] == "unverified"
    assert result["events"][0]["evidence"][0]["raw"] == sources[0]["raw"]
    assert len(result["events"]) == 1
    assert sources == original
    assert checked(item, sources)[0]["id"] == result["id"]


@pytest.mark.parametrize(
    "raw",
    [
        "这个先评估一下。",
        "如果可行我们会提交。",
        "我们不会提交。",
        "我们争取周三交付。",
        "我们会提交吗？",
    ],
)
def test_proposal_not_promoted_to_commitment(raw):
    result = checked(candidate([event("committed", "u1")]), [source("u1", raw)])[0]
    assert result["status"] == "proposed"
    assert "是否承诺待确认" in result["missing"]


def test_unseparated_microphone_does_not_confirm_responsible_person():
    result = checked(
        candidate([event("committed", "u1")]),
        [source("u1", "我承诺提交报告。", separated=False)],
    )[0]
    assert result["status"] == "proposed"
    assert "发言来源尚未分离，责任人待确认" in result["missing"]


def test_others_cannot_reschedule_and_completion_is_only_a_claim():
    sources = [
        source("u1", "我们会提交报告。"),
        source("u2", "改为周五提交。", "b"),
        source("u3", "报告已经完成。"),
        source("u4", "不要取消报告。"),
    ]
    item = candidate(
        [
            event("committed", "u1"),
            event("rescheduled", "u2", "周五"),
            event("claimed_completed", "u3"),
            event("withdrawn", "u4"),
        ]
    )
    result = checked(item, sources)[0]
    assert result["status"] == "claimed_completed"
    assert result["verification_status"] == "not_verified"
    assert len(result["events"]) == 2
    assert result["deadline_text"] is None


def test_fabricated_sources_deadlines_identity_and_side_are_rejected():
    sources = [source("u1", "我们会提交报告。")]
    assert checked(candidate([event("committed", "missing")]), sources) == []
    assert (
        checked(
            candidate([event("committed", "u1")], speaker_id="account-owner"), sources
        )
        == []
    )
    result = checked(
        candidate(
            [event("committed", "u1", "2026年11月1日")],
            side="ours",
            side_evidence_ids=["u1"],
        ),
        sources,
    )[0]
    assert result["deadline_text"] is None
    assert result["side"] == "unknown"


async def test_commitment_agent_runs_alongside_existing_agents_and_keeps_old_context():
    sources = [
        source(f"u{i}", "我们会提交评估报告。" if i == 1 else "继续讨论。")
        for i in range(1, 41)
    ]

    async def reply(role, prompt, payload):
        if role == "commitment":
            assert len(payload["utterances"]) == 40
            return {"commitments": [candidate([event("committed", "u1")])]}
        if role == "verify":
            assert any(u["id"] == "u1" for u in payload["utterances"])
            return {"reviews": [{"id": c["id"], "digest": c["digest"], "accepted": True} for c in payload["business_candidates"]]}
        return {}

    provider = AsyncMock()
    provider.run.side_effect = reply
    result = await MeetingAgents(provider).analyze(sources)
    assert result["commitments"][0]["origin_id"] == "u1"
    assert "commitment" in result["agents_completed"]
    assert provider.run.await_count == 3
    assert not any(call.args[0] == "questionnaire" for call in provider.run.await_args_list)
    assert result['questionnaire_pending'] and result['questionnaire']['questions']


async def test_commitment_bad_schema_has_one_bounded_retry():
    provider = AsyncMock()
    provider.run.side_effect = [{"unexpected": True}, {"commitments": []}]
    assert await CommitmentAgent(provider).analyze([]) == []
    assert provider.run.await_count == 2


async def test_commitment_snapshot_persisted_scoped_and_stale_after_new_revision(
    client, app
):
    headers = await account(client)
    await client.post("/api/v1/meetings/sync", headers=headers, json=meeting())
    report = {"commitments": [{"id": "c1", "status": "proposed", "events": []}]}
    app.state.meeting_analysis.agents.analyze = AsyncMock(return_value=report)
    await client.post("/api/v1/meetings/phone-meeting-1/analysis", headers=headers)
    path = "/api/v1/meetings/phone-meeting-1/commitments"
    data = (await client.get(path, headers=headers)).json()["data"]
    assert data["commitments"] == []  # 无原话和事件的 JSON 不能成为业务事实。
    assert data["revision"] == 1 and data["stale"] is False
    await client.post("/api/v1/meetings/sync", headers=headers, json=meeting(2))
    assert (await client.get(path, headers=headers)).json()["data"]["stale"] is True
    assert (await client.get(path)).status_code == 401
