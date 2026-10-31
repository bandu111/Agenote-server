"""Stage B global retrieval, evidence validation, tenancy and Weixin routing."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select

from app.core.database.mixins import now
from app.modules.identity.models.identity import User
from app.modules.meetings.models.meeting import Meeting
from app.modules.outcomes.assistant_query import (
    classify_local,
    parse_time_scope,
    resolve_scope,
)
from app.modules.outcomes.jobs import OutcomeJobs
from app.modules.outcomes.models import OutcomeRun
from app.modules.outcomes.wechat_jobs import WechatJobs
from app.modules.outcomes.wechat_models import WechatBinding
from app.modules.outcomes.wechat_service import cipher_for
from app.modules.search.service import UnifiedSearch
from tests.test_auth import bearer, register
from tests.test_knowledge import snapshot
from tests.test_meetings import account, meeting


def channel(*, updates=None):
    return SimpleNamespace(
        updates=AsyncMock(return_value=updates or {"msgs": []}),
        send=AsyncMock(return_value={"ret": 0}),
        send_item=AsyncMock(return_value={"ret": 0}),
        close=AsyncMock(),
    )


async def age(session, owner):
    binding = await session.get(WechatBinding, owner)
    binding.updated_at = now() - timedelta(seconds=3)
    await session.commit()


def test_explicit_classifier_rules_precede_context_and_time_zone_is_calendar_correct():
    assert classify_local("2 这个风险怎么验证", has_current=True)["intent"] == "question_number"
    assert classify_local("上个月历史会议说了什么", has_current=True)["intent"] == "history"
    assert classify_local("知识库文档里有哪些约束", has_current=True)["intent"] == "knowledge"
    assert classify_local("对比会议和合同", has_current=True)["intent"] == "compare"
    assert classify_local("继续分析", has_current=True)["intent"] == "current_outcome_followup"
    assert classify_local("继续分析", has_current=False)["intent"] == "clarify"
    scope = parse_time_scope("上个月最近3次会议", "Asia/Shanghai",
                             datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc))
    assert scope == {
        "timeZone": "Asia/Shanghai",
        "dateFrom": "2026-01-31T16:00:00+00:00",
        "dateTo": "2026-02-28T16:00:00+00:00",
        "recentMeetings": 3,
    }


async def test_global_query_without_current_meeting_and_no_evidence_does_not_invent(client, app):
    auth = await register(client)
    response = await client.post("/api/v1/assistant/queries", headers=bearer(auth), json={
        "requestId": "global-no-meeting-0001",
        "query": "历史会议里有没有火星交付计划",
    })
    assert response.status_code == 202, response.text
    run_id = response.json()["data"]["id"]
    provider = SimpleNamespace(run=AsyncMock())
    assert await OutcomeJobs(app.state.database, provider).process_one()
    detail = await client.get("/api/v1/assistant/queries/" + run_id, headers=bearer(auth))
    result = detail.json()["data"]
    assert result["meetingId"] is None and result["status"] == "succeeded"
    assert result["result"]["coverage"] == "none"
    assert result["result"]["sources"] == []
    assert "没有找到" in result["result"]["answer"]
    assert provider.run.await_count == 0
    assert result["timing"]["retrievalMs"] >= 0


async def test_recent_three_meetings_are_selected_before_text_search(client, app):
    headers = await account(client)
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()["data"]["id"]
    async with app.state.database.sessions() as session:
        for index in range(4):
            session.add(Meeting(
                id="recent-" + str(index),
                owner_id=owner,
                client_id="recent-client-" + str(index),
                revision=1,
                content={
                    "title": "Android 评审 " + str(index),
                    "started_at": datetime(2026, 1, index + 1, 2, tzinfo=timezone.utc).isoformat(),
                    "utterances": [{"id": "u" + str(index), "raw": "Android 范围 " + str(index),
                                    "speaker": "发言人 A", "at": index * 1000}],
                },
            ))
        await session.commit()
        scope = await resolve_scope(session, owner, "最近3次会议 Android", {}, {})
        assert scope["meetingIds"] == ["recent-3", "recent-2", "recent-1"]
        hits = await UnifiedSearch(session).search(
            owner, "Android", kinds=["utterance"], meeting_ids=scope["meetingIds"]
        )
        sessions = {hit["sessionId"] for hit in hits}
        assert sessions == {"recent-client-3", "recent-client-2", "recent-client-1"}
        assert "recent-client-0" not in sessions
        assert all(hit["startedAt"] and hit["occurredDate"] and hit["timestamp"] for hit in hits)


async def test_document_and_meeting_comparison_has_server_built_sources(client, app):
    headers = await account(client)
    data = meeting()
    data.update(title="Android 范围复核", ended=True)
    data["utterances"][0].update(raw="会议只确认 Android，一期没有批准 Windows。", turnStatus="complete")
    assert (await client.post("/api/v1/meetings/sync", headers=headers, json=data)).status_code == 200
    assert (await client.post("/api/v1/knowledge/sync", headers=headers,
                              json={"revision": 0, "snapshot": snapshot()})).status_code == 200
    queued = await client.post("/api/v1/assistant/queries", headers=headers, json={
        "requestId": "compare-doc-meeting-01",
        "query": "对比 Android 会议原话和知识库文档",
    })
    assert queued.status_code == 202, queued.text

    async def answer(role, system, payload):
        assert role == "assistant_query_answer"
        document = next((key, value) for key, value in payload["evidence"].items() if value["kind"] == "document")
        utterance = next((key, value) for key, value in payload["evidence"].items() if value["kind"] == "utterance")
        return {
            "title": "范围对照",
            "answer": "会议与合同都只支持 Android，Windows 仍未批准。",
            "coverage": "evidence",
            "claims": [
                {"text": "合同未批准 Windows", "stance": "confirmed",
                 "citations": [{"evidence_id": document[0], "quote": "Windows 属于待评估新增范围，并未批准"}]},
                {"text": "会议未批准 Windows", "stance": "confirmed",
                 "citations": [{"evidence_id": utterance[0], "quote": "一期没有批准 Windows"}]},
            ],
            "conflicts": [],
            "clarification": "",
        }

    provider = SimpleNamespace(run=AsyncMock(side_effect=answer))
    assert await OutcomeJobs(app.state.database, provider).process_one()
    result = (await client.get(
        "/api/v1/assistant/queries/" + queued.json()["data"]["id"], headers=headers
    )).json()["data"]["result"]
    assert {source["kind"] for source in result["sources"]} == {"document", "utterance"}
    document_source = next(source for source in result["sources"] if source["kind"] == "document")
    voice_source = next(source for source in result["sources"] if source["kind"] == "utterance")
    assert all(key in document_source for key in ["versionId", "nodeId", "hash", "page", "source", "path"])
    assert all(voice_source.get(key) is not None for key in ["meetingTitle", "startedAt", "occurredDate", "timestamp", "path", "revision"])
    assert result["stanceClaims"][0]["citations"]


async def test_assistant_query_api_and_structured_search_are_account_scoped(client, app):
    first = await register(client)
    second = await register(client, "assistant-other@example.com")
    one, two = bearer(first), bearer(second)
    owner = (await client.get("/api/v1/auth/me", headers=one)).json()["data"]["id"]
    async with app.state.database.sessions() as session:
        session.add(Meeting(id="private-meeting", owner_id=owner, client_id="private-client", revision=1,
                            content={"title": "Android 私有会议", "started_at": "2026-02-01T00:00:00+00:00",
                                     "utterances": [{"id": "u", "raw": "Android 私有内容", "at": 0}]}))
        await session.commit()
    created = await client.post("/api/v1/assistant/queries", headers=one, json={
        "requestId": "tenant-query-request-01", "query": "历史会议 Android"
    })
    run_id = created.json()["data"]["id"]
    assert (await client.get("/api/v1/assistant/queries/" + run_id, headers=two)).status_code == 404
    assert (await client.get("/api/v1/assistant/queries", headers=two)).json()["data"] == []
    foreign = await client.post("/api/v1/search", headers=two, json={
        "query": "Android", "kinds": ["meeting", "utterance"], "meetingIds": ["private-client"]
    })
    assert foreign.status_code == 200 and foreign.json()["data"] == []


async def test_wechat_explicit_global_query_bypasses_question_pointers(client, app):
    await register(client)
    app.state.settings.wechat_binding_key = SecretStr(Fernet.generate_key().decode())
    incoming = {"get_updates_buf": "next-cursor", "msgs": [{
        "message_id": "global-message-1",
        "from_user_id": "verified-peer",
        "to_user_id": "test-bot",
        "context_token": "context",
        "item_list": [{"type": 1, "text_item": {"text": "上个月历史会议里 Android 怎么决定的"}}],
    }]}
    fake = channel(updates=incoming)
    async with app.state.database.sessions() as session:
        owner = await session.scalar(select(User.id).where(User.email == "yan@example.com"))
        session.add(Meeting(id="pointer-meeting", owner_id=owner, client_id="pointer-client", revision=1,
                            content={"title": "旧会议", "utterances": []}))
        await session.flush()
        question = OutcomeRun(
            id="pointer-question", owner_id=owner, meeting_id="pointer-meeting",
            intent_key="pointer-question-intent", version=1, status="succeeded",
            content={"kind": "meeting_question", "createdAt": (now() - timedelta(days=2)).isoformat(),
                     "question": {"status": "pending", "deleted": False},
                     "result": {"title": "旧疑问"}, "delivery": {"status": "accepted"}},
        )
        binding = WechatBinding(
            owner_id=owner, version=1, status="bound", peer_hash="global-peer",
            encrypted=cipher_for(app.state.settings).encode({
                "base": "https://ilinkai.weixin.qq.com", "bot": "test-bot", "peer": "verified-peer",
                "token": "secret", "context": "context", "cursor": "",
            }),
            state={"boundAt": (now() - timedelta(hours=1)).isoformat(), "generation": "generation-global",
                   "contextReady": True, "questionPointers": [question.id, "another-question"],
                   "lastOutcomeId": question.id,
                   "preferences": {"paused": False, "autoMeeting": True, "importantOnly": True,
                                   "aiDone": True, "sendPdf": True}},
            updated_at=now() - timedelta(seconds=3),
        )
        session.add_all([question, binding])
        await session.commit()
    worker = WechatJobs(app.state.database, app.state.settings, fake)
    assert await worker.process_one()
    async with app.state.database.sessions() as session:
        rows = list(await session.scalars(select(OutcomeRun).where(
            OutcomeRun.content["kind"].astext == "assistant_query"
        )))
        assert len(rows) == 1 and rows[0].meeting_id is None
        assert rows[0].content["classification"]["intent"] == "history"
        assert rows[0].content["inbound"]["source"] == "wechat"
        unchanged = await session.get(OutcomeRun, "pointer-question")
        assert unchanged.status == "succeeded"


async def test_assistant_query_wechat_delivery_has_no_pdf_and_reuses_result_client_id(client, app):
    await register(client)
    app.state.settings.wechat_binding_key = SecretStr(Fernet.generate_key().decode())
    fake = channel()
    async with app.state.database.sessions() as session:
        owner = await session.scalar(select(User.id).where(User.email == "yan@example.com"))
        binding = WechatBinding(
            owner_id=owner, version=1, status="bound", peer_hash="delivery-peer",
            encrypted=cipher_for(app.state.settings).encode({
                "base": "https://ilinkai.weixin.qq.com", "bot": "test-bot", "peer": "verified-peer",
                "token": "secret", "context": "context", "cursor": "",
            }),
            state={"boundAt": (now() - timedelta(hours=1)).isoformat(), "generation": "generation-delivery",
                   "contextReady": True, "preferences": {"paused": False, "autoMeeting": True,
                   "importantOnly": True, "aiDone": True, "sendPdf": True}},
            updated_at=now() - timedelta(seconds=3),
        )
        report = OutcomeRun(
            id="assistant-delivery", owner_id=owner, meeting_id=None,
            intent_key="assistant-delivery-intent", version=1, status="succeeded",
            content={"kind": "assistant_query", "createdAt": now().isoformat(), "replyRequested": True,
                     "result": {"title": "资料回答", "answer": "合同仍未批准 Windows。", "sources": [{
                         "kind": "document", "title": "范围合同", "versionId": "v1", "page": 2,
                         "source": "测试合同", "path": "/knowledge/documents/contract",
                     }]}, "delivery": {"status": "not_sent"}},
        )
        session.add_all([binding, report])
        await session.commit()
    worker = WechatJobs(app.state.database, app.state.settings, fake)
    assert await worker.process_one()
    assert fake.send.await_count == 1
    assert fake.send_item.await_count == 0
    first_client_id = fake.send.await_args.args[2]
    async with app.state.database.sessions() as session:
        report = await session.get(OutcomeRun, "assistant-delivery")
        assert report.content["delivery"]["status"] == "accepted"
        assert report.content["delivery"]["clientId"] == first_client_id
        assert "pdfStatus" not in report.content["delivery"]
        await age(session, owner)
    assert await worker.process_one()
    assert fake.send.await_count == 1 and fake.send_item.await_count == 0


async def test_succeeded_outcomes_are_ai_derived_search_hits(client, app):
    await register(client)
    async with app.state.database.sessions() as session:
        owner = await session.scalar(select(User.id).where(User.email == "yan@example.com"))
        session.add(OutcomeRun(
            id="searchable-assistant", owner_id=owner, meeting_id=None,
            intent_key="searchable-assistant-intent", version=3, status="succeeded",
            content={"kind": "assistant_query", "createdAt": now().isoformat(),
                     "result": {"title": "Android 范围答复", "answer": "Windows 尚未批准"}},
        ))
        await session.commit()
        hits = await UnifiedSearch(session).search(owner, "Windows", kinds=["outcome"])
        assert len(hits) == 1
        assert hits[0]["aiDerived"] is True
        assert hits[0]["sourceRunId"] == "searchable-assistant"
        assert hits[0]["sourceVersion"] == 3
        assert hits[0]["sourcePath"].endswith("searchable-assistant")


async def test_foreign_meeting_scope_suppresses_all_search_kinds(client, app):
    first = await register(client)
    second = await register(client, "scope-other@example.com")
    one, two = bearer(first), bearer(second)
    owner_one = (await client.get("/api/v1/auth/me", headers=one)).json()["data"]["id"]
    assert (await client.post("/api/v1/knowledge/sync", headers=two,
                              json={"revision": 0, "snapshot": snapshot()})).status_code == 200
    async with app.state.database.sessions() as session:
        session.add(Meeting(id="foreign-scope", owner_id=owner_one, client_id="foreign-scope-client", revision=1,
                            content={"title": "Android 外部会议", "started_at": "2026-02-01T00:00:00+00:00",
                                     "utterances": [{"id": "u", "raw": "Android", "at": 0}]}))
        await session.commit()
    result = await client.post("/api/v1/search", headers=two, json={
        "query": "Android", "meetingIds": ["foreign-scope-client"]
    })
    assert result.status_code == 200 and result.json()["data"] == []


async def test_same_question_id_is_isolated_by_meeting(client):
    headers = await account(client)
    first = meeting()
    second = {**meeting(), "id": "phone-meeting-2", "startedAt": "2026-10-20T10:00:00+08:00"}
    assert (await client.post("/api/v1/meetings/sync", headers=headers, json=first)).status_code == 200
    assert (await client.post("/api/v1/meetings/sync", headers=headers, json=second)).status_code == 200
    payload = {"baseVersion": 0, "mutationId": "mutation-1", "content": {
        "atMs": 0, "text": "同名疑问", "status": "pending", "deleted": False,
        "sourceIds": ["u1"], "association": "none"
    }}
    one = await client.post("/api/v1/meetings/phone-meeting-1/questions/shared/save", headers=headers, json=payload)
    two = await client.post("/api/v1/meetings/phone-meeting-2/questions/shared/save", headers=headers, json=payload)
    assert one.status_code == 200 and two.status_code == 200


def test_assistant_classifier_uses_light_qwen_route_when_configured():
    from app.core.config import Settings
    from app.integrations.hermes.model_policy import select_model
    from app.integrations.hermes.role_model import LIGHT_ROLES
    settings = Settings(dashscope_api_key="synthetic-qwen-key", qwen_workers_enabled=True)
    route = select_model(settings, "assistant_query_route")
    assert "assistant_query_route" in LIGHT_ROLES
    assert route.provider == "qwen" and route.max_tokens == 260


async def test_recent_meeting_listing_needs_no_topic_keyword(client, app):
    headers = await account(client)
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()["data"]["id"]
    async with app.state.database.sessions() as session:
        for index in range(4):
            session.add(Meeting(id="listing-" + str(index), owner_id=owner,
                client_id="listing-client-" + str(index), revision=1,
                content={"title": "周会 " + str(index),
                         "started_at": datetime(2026, 2, index + 1, tzinfo=timezone.utc).isoformat(),
                         "utterances": []}))
        await session.commit()
    queued = await client.post("/api/v1/assistant/queries", headers=headers, json={
        "requestId": "recent-listing-request", "query": "最近3次会议分别讲了什么"
    })
    seen = []
    async def answer(role, system, payload):
        seen.extend(value["title"] for value in payload["evidence"].values())
        first = next(iter(payload["evidence"].items()))
        return {"title": "最近会议", "answer": "找到最近三场会议。", "coverage": "evidence",
                "claims": [{"text": "最近会议集合", "stance": "discussion",
                            "citations": [{"evidence_id": first[0], "quote": first[1]["text"]}]}],
                "conflicts": [], "clarification": ""}
    assert await OutcomeJobs(app.state.database, SimpleNamespace(run=AsyncMock(side_effect=answer))).process_one()
    assert set(seen) == {"周会 3", "周会 2", "周会 1"}
    assert "周会 0" not in seen
    result = (await client.get("/api/v1/assistant/queries/" + queued.json()["data"]["id"], headers=headers)).json()["data"]["result"]
    assert result["scope"]["meetingIds"] == ["listing-3", "listing-2", "listing-1"]


async def test_wechat_followup_to_assistant_creates_child_query(client, app):
    await register(client)
    app.state.settings.wechat_binding_key = SecretStr(Fernet.generate_key().decode())
    incoming = {"msgs": [{"message_id": "assistant-followup-message", "from_user_id": "verified-peer",
        "to_user_id": "test-bot", "context_token": "context",
        "item_list": [{"type": 1, "text_item": {"text": "继续分析这个差异"}}]}]}
    fake = channel(updates=incoming)
    async with app.state.database.sessions() as session:
        owner = await session.scalar(select(User.id).where(User.email == "yan@example.com"))
        parent = OutcomeRun(id="assistant-parent", owner_id=owner, meeting_id=None,
            intent_key="assistant-parent-intent", version=1, status="succeeded",
            content={"kind": "assistant_query", "query": "比较范围", "createdAt": (now()-timedelta(minutes=2)).isoformat(),
                     "result": {"title": "范围比较", "answer": "仍有差异", "sources": []},
                     "delivery": {"status": "accepted"}})
        binding = WechatBinding(owner_id=owner, version=1, status="bound", peer_hash="followup-peer",
            encrypted=cipher_for(app.state.settings).encode({"base": "https://ilinkai.weixin.qq.com",
                "bot": "test-bot", "peer": "verified-peer", "token": "secret", "context": "context", "cursor": ""}),
            state={"boundAt": (now()-timedelta(hours=1)).isoformat(), "generation": "followup-generation",
                   "contextReady": True, "lastOutcomeId": parent.id, "questionPointers": [],
                   "preferences": {"paused": False, "autoMeeting": True, "importantOnly": True,
                                   "aiDone": True, "sendPdf": True}},
            updated_at=now()-timedelta(seconds=3))
        session.add_all([parent, binding]); await session.commit()
    assert await WechatJobs(app.state.database, app.state.settings, fake).process_one()
    async with app.state.database.sessions() as session:
        child = await session.scalar(select(OutcomeRun).where(
            OutcomeRun.content["kind"].astext == "assistant_query", OutcomeRun.id != "assistant-parent"))
        assert child and child.meeting_id is None
        assert child.content["parentQueryId"] == "assistant-parent"
        assert child.content["classification"]["intent"] == "current_outcome_followup"


async def test_assistant_query_active_queue_is_bounded(client):
    auth = bearer(await register(client))
    statuses = []
    for index in range(3):
        response = await client.post("/api/v1/assistant/queries", headers=auth, json={
            "requestId": "bounded-query-" + str(index).zfill(4),
            "query": "历史会议 Android " + str(index),
        })
        statuses.append(response.status_code)
    assert statuses == [202, 202, 429]
