"""真实数据库：清除墓碑、账户隔离、参考资料与候选详情。"""
from tests.test_meetings import account
from tests.test_knowledge_assistance import action, document, processor, TEXT
from app.modules.knowledge.services.document_agent import DocumentDraft, validate_actions
from types import SimpleNamespace

async def test_clear_is_durable_account_scoped_and_idempotent(client):
    a = await account(client)
    b = await account(client, "inbox-b@example.com")
    path = "/api/v1/knowledge/notice-inbox"
    ids = ["progress-pending:one", "assistance:meeting-warning:one"]
    for _ in range(2):
        response = await client.post(path + "/clear", headers=a, json={"ids": ids})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["clearedIds"] == sorted(ids)
    assert (await client.get(path, headers=a)).json()["data"]["clearedIds"] == sorted(ids)
    assert (await client.get(path, headers=b)).json()["data"]["clearedIds"] == []
    assert (await client.post(path + "/clear", headers=a, json={"ids": ["anything"]})).status_code == 422

async def test_document_actions_live_in_detail_not_global_and_cannot_cross_accounts(client, app):
    a = await account(client)
    b = await account(client, "docs-b@example.com")
    await client.post("/api/v1/knowledge/sync", headers=a, json={"revision": 0, "snapshot": document()})
    await processor(app)
    assert (await client.get("/api/v1/knowledge/assistance", headers=a)).json()["data"] == []
    path = "/api/v1/knowledge/documents/contract/actions?version_id=v1"
    details = await client.get(path, headers=a)
    assert details.status_code == 200, details.text
    assert details.json()["data"]["actions"][0]["quote"] == TEXT
    assert (await client.get(path, headers=b)).status_code == 404


def test_reference_examples_uncertain_actions_are_not_real_tasks():
    node = SimpleNamespace(id="a" * 64, text=TEXT, document_id="d", version_id="v")
    for purpose in ["reference", "example", "unknown"]:
        draft = DocumentDraft.model_validate({"purpose": purpose, "actions": [action()]})
        assert validate_actions(draft, [node]) == []
    for role in ["reference", "example", "uncertain"]:
        draft = DocumentDraft.model_validate({"purpose": "mixed", "actions": [action(evidence_role=role)]})
        assert validate_actions(draft, [node]) == []
    draft = DocumentDraft.model_validate({"purpose": "actionable", "actions": [action(), action()]})
    assert len(validate_actions(draft, [node])) == 1


async def test_risk_dedup_uses_actual_source_and_clear_survives_model_rerun(client, app):
    from sqlalchemy import select
    from app.modules.identity.models.identity import User
    from app.modules.meetings.models.meeting import Meeting
    a = await account(client)
    raw = "ERP 接口超时可能阻塞上线。"
    def risk(id, body):
        return {"id": id, "kind": "risk", "title": "上线风险", "body": body, "evidence_ids": ["u1"]}
    async with app.state.database.sessions() as session:
        owner = await session.scalar(select(User.id))
        session.add(Meeting(id="inbox-meeting", owner_id=owner, client_id="qa-source", revision=1, analysis_revision=1,
            content={"utterances": [{"id": "u1", "raw": raw, "speaker": "A"}]},
            analysis={"insights": [risk("r1", "需要核对影响"), risk("r2", "需确认上线影响")]}))
        await session.commit()
    notices = (await client.get("/api/v1/knowledge/assistance", headers=a)).json()["data"]
    assert len(notices) == 1
    previous = notices[0]
    await client.post("/api/v1/knowledge/notice-inbox/clear", headers=a, json={"ids": ["assistance:" + previous["id"]]})
    async with app.state.database.sessions() as session:
        row = await session.get(Meeting, "inbox-meeting")
        row.analysis = {"insights": [risk("regenerated-id", "模型改成另一种措辞")]}
        await session.commit()
    assert (await client.get("/api/v1/knowledge/assistance", headers=a)).json()["data"] == []
    async with app.state.database.sessions() as session:
        row = await session.get(Meeting, "inbox-meeting")
        row.content = {"utterances": [{"id": "u1", "raw": "ERP 接口已连续失败，正式发布被阻塞。", "speaker": "A"}]}
        row.revision = row.analysis_revision = 2
        await session.commit()
    fresh = (await client.get("/api/v1/knowledge/assistance", headers=a)).json()["data"]
    assert len(fresh) == 1 and fresh[0]["id"] != previous["id"]
    async with app.state.database.sessions() as session:
        row = await session.get(Meeting, "inbox-meeting")
        row.deleted = True
        await session.commit()
    assert (await client.get("/api/v1/knowledge/assistance", headers=a)).json()["data"] == []
