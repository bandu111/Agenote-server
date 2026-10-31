"""私有检索适配：账号隔离、版本隔离，故障时由调用方降级。"""

import hashlib
import httpx
from app.core.config import get_settings


async def retrieve(owner, revision, nodes, query, limit, project=None):
    settings = get_settings()
    if not settings.qmd_url or not nodes:
        return None
    if len(nodes) > 2000 or any(len(n.text) > 30000 for n in nodes):
        return None
    payload = {
        "scope": hashlib.sha256(f"{owner}:{project or ''}".encode()).hexdigest(),
        "tenant": hashlib.sha256(owner.encode()).hexdigest(),
        "revision": revision,
        "nodes": [{"id": n.id, "text": n.text} for n in nodes],
        "query": query[:2000],
        "limit": limit,
    }
    try:
        async with httpx.AsyncClient(
            timeout=settings.qmd_timeout_seconds, trust_env=False
        ) as client:
            response = await client.post(
                settings.qmd_url.rstrip("/") + "/search", json=payload
            )
            response.raise_for_status()
            value = response.json()
        if value.get("mode") not in {"qmd-hybrid", "qmd-bm25"}:
            return None
        allowed = {n.id: n for n in nodes}
        seen, hits = set(), []
        for hit in value["hits"]:
            key = hit["nodeId"]
            score = float(hit["score"])
            if key in allowed and key not in seen and 0 < score < float("inf"):
                hits.append((allowed[key], score, value["mode"]))
                seen.add(key)
        return hits[:limit]
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        return None


async def sync(owner, revision, nodes):
    """资料变化时发送当前有效节点；不暴露外部任意路径。"""
    settings = get_settings()
    if not settings.qmd_url:
        return
    try:
        async with httpx.AsyncClient(
            timeout=min(3, settings.qmd_timeout_seconds), trust_env=False
        ) as client:
            response = await client.post(
                settings.qmd_url.rstrip("/") + "/sync",
                json={
                    "tenant": hashlib.sha256(owner.encode()).hexdigest(),
                    "revision": revision,
                    "nodes": [{"id": n.id, "text": n.text} for n in nodes],
                },
            )
            response.raise_for_status()
    except httpx.HTTPError:
        # 检索入口仍会重新校验版本；故障不回滚已经生效的资料变更。
        pass
