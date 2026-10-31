"""真实数据库验证账号隔离、版本、索引和会议自动归档，不依赖模型猜测。"""

from copy import deepcopy
import hashlib
from unittest.mock import AsyncMock
from tests.test_meetings import account, meeting


def snapshot():
    text = "星澜客户管理合同：一期仅交付 Android 和 iOS，交付周期30天。Windows 属于待评估新增范围，并未批准。"
    return {
        "schema": 1,
        "folders": [{"id": "inbox", "name": "收件箱"}],
        "projects": [{"id": "p1", "name": "星澜", "allowed": True}],
        "documents": [
            {
                "id": "contract",
                "folderId": "inbox",
                "projectId": "p1",
                "kind": "markdown",
                "source": "测试合同",
                "createdAt": "2026-10-19T00:00:00Z",
                "selectedVersionId": "v1",
                "allowed": True,
                "versions": [
                    {
                        "id": "v1",
                        "number": 1,
                        "title": "星澜一期范围合同",
                        "content": text,
                        "hash": hashlib.sha256(text.encode()).hexdigest(),
                        "createdAt": "2026-10-19T00:00:00Z",
                        "state": "current",
                    }
                ],
            }
        ],
    }


async def test_knowledge_acl_revision_citations_immutable_source(client):
    first = await account(client)
    second = await account(client, "second@example.com")
    data = snapshot()
    result = await client.post(
        "/api/v1/knowledge/sync", headers=first, json={"revision": 0, "snapshot": data}
    )
    assert result.status_code == 200, result.text
    assert result.json()["data"]["indexStates"][0]["status"] == "indexed"
    search = await client.post(
        "/api/v1/knowledge/search",
        headers=first,
        json={"query": "Windows Android 合同"},
    )
    hits = search.json()["data"]
    assert hits and "并未批准" in hits[0]["text"]
    citation = {k: hits[0][k] for k in ["documentId", "versionId", "nodeId", "hash"]}
    assert (
        await client.post("/api/v1/knowledge/resolve", headers=first, json=citation)
    ).status_code == 200
    assert (
        await client.post(
            "/api/v1/knowledge/search", headers=second, json={"query": "合同"}
        )
    ).json()["data"] == []
    assert (
        await client.post("/api/v1/knowledge/resolve", headers=second, json=citation)
    ).status_code == 404
    canonical = result.json()["data"]["snapshot"]
    canonical["projects"][0]["allowed"] = False
    canonical["documents"][0]["allowed"] = False
    revoked = await client.post(
        "/api/v1/knowledge/sync",
        headers=first,
        json={"revision": 1, "snapshot": canonical},
    )
    assert revoked.status_code == 200, revoked.text
    assert (
        await client.post("/api/v1/knowledge/resolve", headers=first, json=citation)
    ).status_code == 200
    assert revoked.json()["data"]["snapshot"]["projects"][0]["allowed"] is True
    assert revoked.json()["data"]["snapshot"]["documents"][0]["allowed"] is True
    assert (
        await client.post(
            "/api/v1/knowledge/search", headers=first, json={"query": "合同"}
        )
    ).json()["data"]
    assert (
        await client.post(
            "/api/v1/knowledge/sync",
            headers=first,
            json={"revision": 0, "snapshot": canonical},
        )
    ).status_code == 409
    canonical["documents"][0]["versions"][0]["content"] = "已批准Windows"
    canonical["documents"][0]["versions"][0]["hash"] = hashlib.sha256(
        "已批准Windows".encode()
    ).hexdigest()
    assert (
        await client.post(
            "/api/v1/knowledge/sync",
            headers=first,
            json={"revision": 2, "snapshot": canonical},
        )
    ).status_code == 409


async def test_historical_false_flags_do_not_block_search_or_citations(client, app):
    from sqlalchemy import select
    from app.modules.knowledge.models.library import KnowledgeLibrary

    headers = await account(client)
    owner = (await client.get('/api/v1/auth/me', headers=headers)).json()['data']['id']
    result = await client.post('/api/v1/knowledge/sync', headers=headers,
                               json={'revision': 0, 'snapshot': snapshot()})
    assert result.status_code == 200
    async with app.state.database.sessions() as db:
        row = await db.scalar(select(KnowledgeLibrary).where(KnowledgeLibrary.owner_id == owner))
        legacy = deepcopy(row.snapshot)
        legacy['projects'][0]['allowed'] = False
        legacy['documents'][0]['allowed'] = False
        row.snapshot = legacy
        await db.commit()
    library = (await client.get('/api/v1/knowledge', headers=headers)).json()['data']
    assert library['snapshot']['projects'][0]['allowed'] is True
    assert library['snapshot']['documents'][0]['allowed'] is True
    hits = (await client.post('/api/v1/knowledge/search', headers=headers,
                              json={'query': 'Windows Android'})).json()['data']
    assert hits
    citation = {key: hits[0][key] for key in ['documentId', 'versionId', 'nodeId', 'hash']}
    assert (await client.post('/api/v1/knowledge/resolve', headers=headers, json=citation)).status_code == 200


async def test_archive_to_knowledge_then_following_meeting_context(client, app):
    headers = await account(client)
    source = meeting()
    source["utterances"][0]["raw"] = "一期Android与iOS，Windows仅评估，未批准。"
    source["ended"] = True
    await client.post("/api/v1/meetings/sync", headers=headers, json=source)
    app.state.meeting_analysis.agents.provider.run = AsyncMock(
        return_value={"decisions": [], "actions": [], "open_questions": []}
    )
    archive = await client.post(
        "/api/v1/meetings/phone-meeting-1/archive", headers=headers
    )
    assert archive.status_code == 200, archive.text
    library = (await client.get("/api/v1/knowledge", headers=headers)).json()["data"]
    assert len(library["snapshot"]["documents"]) == 1
    assert library["snapshot"]["documents"][0]["source"] == "meeting:phone-meeting-1"
    assert (
        await client.post(
            "/api/v1/knowledge/search", headers=headers, json={"query": "Windows"}
        )
    ).json()["data"]
    library["snapshot"]["projects"][1]["allowed"] = False
    grant = await client.post(
        "/api/v1/knowledge/sync",
        headers=headers,
        json={"revision": library["revision"], "snapshot": library["snapshot"]},
    )
    assert grant.status_code == 200, grant.text
    hits = (
        await client.post(
            "/api/v1/knowledge/search", headers=headers, json={"query": "Windows"}
        )
    ).json()["data"]
    assert hits and hits[0]["sessionId"] == "phone-meeting-1"
    source["id"] = "second-meeting"
    source["ended"] = False
    source["utterances"][0]["raw"] = "Windows 可以一起做吗？"
    await client.post("/api/v1/meetings/sync", headers=headers, json=source)
    analyzer = AsyncMock(
        return_value={"insights": [], "repairs": [], "withdrawals": []}
    )
    app.state.meeting_analysis.agents.analyze = analyzer
    result = await client.post(
        "/api/v1/meetings/second-meeting/analysis", headers=headers
    )
    assert result.status_code == 200, result.text
    assert (
        analyzer.call_args.kwargs["context"]["knowledge"][0]["sessionId"]
        == "phone-meeting-1"
    )
