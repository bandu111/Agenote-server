"""QMD 故障降级和越权返回不能突破 LlamaIndex 资料授权边界。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from app.integrations.qmd import client as qmd
from app.core.config import Settings
from tests.test_meetings import account
from tests.test_knowledge import snapshot


async def test_qmd_response_can_only_select_pre_authorized_nodes(monkeypatch):
    sent = []

    async def handler(request):
        import json

        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "mode": "qmd-hybrid",
                "hits": [
                    {"nodeId": "other-account", "score": 1},
                    {"nodeId": "a" * 64, "score": 0.9},
                    {"nodeId": "a" * 64, "score": 0.8},
                    {"nodeId": "b" * 64, "score": float("inf")},
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        qmd,
        "get_settings",
        lambda: SimpleNamespace(qmd_url="http://127.0.0.1:8182", qmd_timeout_seconds=3),
    )
    monkeypatch.setattr(
        qmd.httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw)
    )
    a = SimpleNamespace(id="a" * 64, text="资料原文")

    # JSON 不接受 Infinity，模拟服务端仅返回可序列化正常分数。
    async def valid_handler(request):
        import json

        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "mode": "qmd-hybrid",
                "hits": [
                    {"nodeId": "other", "score": 1},
                    {"nodeId": a.id, "score": 0.9},
                    {"nodeId": a.id, "score": 0.8},
                ],
            },
        )

    transport = httpx.MockTransport(valid_handler)
    result = await qmd.retrieve("owner", 3, [a], "资料", 6, "p1")
    assert result == [(a, 0.9, "qmd-hybrid")]
    assert sent[0]["revision"] == 3 and sent[0]["scope"] != sent[0]["tenant"]


async def test_qmd_network_failure_returns_explicit_fallback(monkeypatch):
    async def fail(request):
        raise httpx.ConnectError("test unavailable", request=request)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        qmd,
        "get_settings",
        lambda: SimpleNamespace(qmd_url="http://127.0.0.1:8182", qmd_timeout_seconds=1),
    )
    monkeypatch.setattr(
        qmd.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(fail), **kw),
    )
    assert (
        await qmd.retrieve("o", 1, [SimpleNamespace(id="a" * 64, text="x")], "x", 2)
        is None
    )


async def test_service_rechecks_library_after_retrieval(client, monkeypatch):
    headers = await account(client)
    response = await client.post(
        "/api/v1/knowledge/sync",
        headers=headers,
        json={"revision": 0, "snapshot": snapshot()},
    )
    library = response.json()["data"]

    async def revoke(owner, revision, nodes, query, limit, project):
        library["snapshot"]["documents"][0]["versions"][0]["state"] = "retracted"
        result = await client.post(
            "/api/v1/knowledge/sync",
            headers=headers,
            json={"revision": revision, "snapshot": library["snapshot"]},
        )
        assert result.status_code == 200
        return [(nodes[0], 0.9, "qmd-hybrid")]

    monkeypatch.setattr(qmd, "retrieve", revoke)
    result = await client.post(
        "/api/v1/knowledge/search", headers=headers, json={"query": "合同"}
    )
    assert result.status_code == 200 and result.json()["data"] == []


def test_qmd_cannot_be_public_or_include_credentials():
    for value in [
        "https://example.com",
        "http://127.0.0.1.evil.test",
        "http://user:pass@localhost:8182",
    ]:
        with pytest.raises(ValueError):
            Settings(qmd_url=value)
