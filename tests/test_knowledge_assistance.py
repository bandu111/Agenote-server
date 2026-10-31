"""真实 PostgreSQL 验证候选、确认、版本、资料撤回与账号边界。"""

import copy
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select
from app.modules.knowledge.models.assistance import DocumentAnalysis
from app.modules.progress.models.progress_item import ProgressItem
from app.modules.knowledge.services.document_agent import (
    DocumentDraft,
    validate_actions,
)
from app.modules.meetings.services.decision_questionnaire import DecisionQuestionnaire
from tests.test_meetings import account
from tests.test_knowledge import snapshot

TEXT = "我方承诺于2026年11月15日提交双端测试报告。"


def document(text=TEXT):
    data = snapshot()
    version = data["documents"][0]["versions"][0]
    version["content"] = text
    version["hash"] = hashlib.sha256(text.encode()).hexdigest()
    return data


def action(node_id="a" * 64, quote=TEXT, **changes):
    return {
        "evidence_role": "actual",
        "node_id": node_id,
        "quote": quote,
        "action_quote": "提交双端测试报告",
        "modality": "obligation",
        "side": "ours",
        "deadline_text": "2026年11月15日",
        **changes,
    }


@pytest.mark.parametrize(
    "quote,expected",
    [
        (TEXT, "obligation"),
        ("我方可以考虑提交双端测试报告。", "proposal"),
        ("如果条件满足，我方承诺提交双端测试报告。", "conditional"),
        ("请不要取消提交双端测试报告。", None),
    ],
)
def test_document_modality_and_verbatim_provenance(quote, expected):
    node = SimpleNamespace(id="a" * 64, text=quote, document_id="d", version_id="v")
    draft = DocumentDraft.model_validate({"purpose": "actionable", "actions": [action(quote=quote)]})
    result = validate_actions(draft, [node])
    if expected is None:
        assert result == []
    else:
        assert result[0]["modality"] == expected
        assert result[0]["quote"] == quote
        if quote != TEXT:
            assert result[0]["deadline"] is None


def test_document_does_not_invent_account_side_or_relative_deadline():
    raw = "乙方承诺下周三提交双端测试报告。"
    node = SimpleNamespace(id="a" * 64, text=raw, document_id="d", version_id="v")
    draft = DocumentDraft.model_validate(
        {"purpose": "actionable", "actions": [action(quote=raw, deadline_text="下周三")]}
    )
    result = validate_actions(draft, [node])[0]
    assert result["side"] == "unknown" and result["deadline"] is None
    assert set(result["missing"]) == {"履约方待明确", "交付时间待明确"}
    invented = DocumentDraft.model_validate({"purpose": "actionable", "actions": [action(quote=TEXT)]})
    assert validate_actions(invented, [node]) == []


def test_cropped_quote_cannot_drop_condition_or_negation():
    quote = "我方承诺提交双端测试报告"
    draft = DocumentDraft.model_validate({"purpose": "actionable", "actions": [action(quote=quote)]})
    node = SimpleNamespace(
        id="a" * 64,
        text="如果采购通过，" + quote + "。",
        document_id="d",
        version_id="v",
    )
    assert validate_actions(draft, [node])[0]["modality"] == "conditional"
    node.text = "不要把这个写成" + quote + "。"
    assert validate_actions(draft, [node]) == []


async def processor(app, during=None):
    async def run(role, prompt, payload):
        if role == "document":
            if during:
                await during()
            n = payload["nodes"][0]
            return {"purpose": "actionable", "actions": [action(n["node_id"], quote=n["text"])]}
        return {"questions": []}

    app.state.document_jobs.provider = SimpleNamespace(run=AsyncMock(side_effect=run))
    assert await app.state.document_jobs.process_one()


async def test_document_record_requires_exact_consent_and_is_idempotent(client, app):
    headers = await account(client)
    other = await account(client, "other@example.com")
    response = await client.post(
        "/api/v1/knowledge/sync",
        headers=headers,
        json={"revision": 0, "snapshot": document()},
    )
    assert response.status_code == 200, response.text
    before = (await client.get("/api/v1/progress", headers=headers)).json()["data"]
    assert not before["commitments"] and not before["pending"]
    await processor(app)
    assert (await client.get("/api/v1/knowledge/assistance", headers=headers)).json()["data"] == []
    response_actions = await client.get("/api/v1/knowledge/documents/contract/actions?version_id=v1", headers=headers)
    assert response_actions.status_code == 200, response_actions.text
    notices = response_actions.json()["data"]["actions"]
    n = next(n for n in notices if n["kind"] == "document_action")
    assert n["state"] == "pending"
    assert (await client.get("/api/v1/knowledge/assistance", headers=other)).json()[
        "data"
    ] == []
    request = {k: n[k] for k in ["id", "fingerprint"]} | {"allow": True}
    assert (
        await client.post(
            "/api/v1/knowledge/assistance/decision", headers=other, json=request
        )
    ).status_code == 404
    first = await client.post(
        "/api/v1/knowledge/assistance/decision", headers=headers, json=request
    )
    assert first.status_code == 200, first.text
    second = await client.post(
        "/api/v1/knowledge/assistance/decision", headers=headers, json=request
    )
    assert second.json()["data"] == first.json()["data"]
    conflict = await client.post(
        "/api/v1/knowledge/assistance/decision",
        headers=headers,
        json=request | {"allow": False},
    )
    assert conflict.status_code == 409
    data = (await client.get("/api/v1/progress", headers=headers)).json()["data"]
    item = data["commitments"][0]
    assert len(data["commitments"]) == 1 and item["status"] == "pending"
    assert (
        item["evidence"]["kind"] == "document"
        and item["evidence"]["transcript"] == TEXT
    )
    assert item["authorizationStatus"] == "pending"
    library = response.json()["data"]
    library["snapshot"]["documents"][0]["versions"][0]["state"] = "retracted"
    assert (
        await client.post(
            "/api/v1/knowledge/sync",
            headers=headers,
            json={"revision": library["revision"], "snapshot": library["snapshot"]},
        )
    ).status_code == 200
    assert (await client.get("/api/v1/knowledge/assistance", headers=headers)).json()[
        "data"
    ] == []
    assert (
        await client.post(
            "/api/v1/knowledge/assistance/decision", headers=headers, json=request
        )
    ).status_code == 404
    # 资料撤回阻止后续引用，不暗中删除用户已记录的业务候选。
    assert (
        len(
            (await client.get("/api/v1/progress", headers=headers)).json()["data"][
                "commitments"
            ]
        )
        == 1
    )


async def test_retracted_during_model_execution_cannot_publish_candidate(client, app):
    headers = await account(client)
    response = await client.post(
        "/api/v1/knowledge/sync",
        headers=headers,
        json={"revision": 0, "snapshot": document()},
    )
    library = response.json()["data"]

    async def revoke():
        library["snapshot"]["documents"][0]["versions"][0]["state"] = "retracted"
        result = await client.post(
            "/api/v1/knowledge/sync",
            headers=headers,
            json={"revision": library["revision"], "snapshot": library["snapshot"]},
        )
        assert result.status_code == 200

    await processor(app, revoke)
    assert (await client.get("/api/v1/knowledge/assistance", headers=headers)).json()[
        "data"
    ] == []
    async with app.state.database.sessions() as session:
        row = await session.scalar(select(DocumentAnalysis))
        assert row.status == "stale" and not row.result
        assert list(await session.scalars(select(ProgressItem))) == []


async def test_missing_fields_question_is_not_a_business_answer(client, app):
    headers = await account(client)
    text = "相关负责人承诺提交双端测试报告。"
    await client.post(
        "/api/v1/knowledge/sync",
        headers=headers,
        json={"revision": 0, "snapshot": document(text)},
    )
    await processor(app)
    notices = (
        await client.get("/api/v1/knowledge/assistance", headers=headers)
    ).json()["data"]
    assert notices == []
    details = (await client.get("/api/v1/knowledge/documents/contract/actions?version_id=v1", headers=headers)).json()["data"]
    assert len(details["actions"]) == 1
    data = (await client.get("/api/v1/progress", headers=headers)).json()["data"]
    assert not data["commitments"] and not data["pending"]


async def test_questionnaire_uses_known_fields_and_reuses_unchanged_gaps():
    provider = SimpleNamespace(run=AsyncMock(return_value={"questions": []}))
    agent = DecisionQuestionnaire(provider)
    gap = {
        "id": "g",
        "origin_id": "u1",
        "field": "deadline",
        "subject": "提交报告",
        "fallback": "何时提交？",
        "evidence_ids": ["u1"],
    }
    first = await agent.build([gap])
    assert len(first["questions"]) == 1 and not first["applied"]
    second = await agent.build([gap], previous=first)
    assert second["cached"] and provider.run.await_count == 1
    third = await agent.build(
        [gap],
        {
            "client_id": "m1",
            "progress": [
                {
                    "content": {
                        "evidence": {"id": "u1", "sessionId": "m1"},
                        "deadline": "2026-11-15",
                    }
                }
            ],
        },
    )
    assert third["questions"] == [] and provider.run.await_count == 1
    other = await agent.build(
        [gap],
        {
            "client_id": "m2",
            "progress": [
                {
                    "content": {
                        "evidence": {"id": "u1", "sessionId": "m1"},
                        "deadline": "2026-11-15",
                    }
                }
            ],
        },
    )
    assert other["questions"] and provider.run.await_count == 2


async def test_questionnaire_cannot_fabricate_source_id():
    provider = SimpleNamespace(
        run=AsyncMock(
            return_value={
                "questions": [{"candidate_id": "invented", "question": "谁负责？"}]
            }
        )
    )
    gap = {
        "id": "g",
        "origin_id": "u1",
        "field": "side",
        "subject": "报告",
        "fallback": "由哪方履约？",
        "evidence_ids": ["u1"],
    }
    result = await DecisionQuestionnaire(provider).build([gap])
    assert result["questions"][0]["id"] == "g" and result["questions"][0][
        "evidence_ids"
    ] == ["u1"]
