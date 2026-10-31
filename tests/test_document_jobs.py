"""持久队列的升级回填、崩溃租期恢复和有限重试。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from sqlalchemy import select, delete
from app.modules.knowledge.models.assistance import DocumentAnalysis
from tests.test_meetings import account
from tests.test_knowledge_assistance import document, processor


async def test_backfill_is_idempotent_and_expired_worker_lease_recovers(client, app):
    headers = await account(client)
    await client.post(
        "/api/v1/knowledge/sync",
        headers=headers,
        json={"revision": 0, "snapshot": document()},
    )
    async with app.state.database.sessions() as session:
        await session.execute(delete(DocumentAnalysis))
        await session.commit()
    await app.state.document_jobs.backfill()
    await app.state.document_jobs.backfill()
    async with app.state.database.sessions() as session:
        rows = list(await session.scalars(select(DocumentAnalysis)))
        assert len(rows) == 1
        rows[0].status = "running"
        rows[0].attempts = 1
        rows[0].updated_at = datetime.now(timezone.utc) - timedelta(minutes=16)
        await session.commit()
    await processor(app)
    async with app.state.database.sessions() as session:
        row = await session.scalar(select(DocumentAnalysis))
        assert row.status == "succeeded" and row.attempts == 2


async def test_failed_analysis_requires_explicit_retry_and_has_bounded_attempts(
    client, app
):
    headers = await account(client)
    await client.post(
        "/api/v1/knowledge/sync",
        headers=headers,
        json={"revision": 0, "snapshot": document()},
    )
    app.state.document_jobs.provider = SimpleNamespace(
        run=AsyncMock(side_effect=TimeoutError("synthetic timeout"))
    )
    for attempt in range(1, 4):
        assert await app.state.document_jobs.process_one()
        notice = (
            await client.get("/api/v1/knowledge/assistance", headers=headers)
        ).json()["data"][0]
        assert notice["state"] == ("pending" if attempt < 3 else "unavailable")
        decision = await client.post(
            "/api/v1/knowledge/assistance/decision",
            headers=headers,
            json={k: notice[k] for k in ["id", "fingerprint"]} | {"allow": True},
        )
        assert decision.status_code == (200 if attempt < 3 else 409)
    assert app.state.document_jobs.provider.run.await_count == 3
    assert not (await client.get("/api/v1/progress", headers=headers)).json()["data"][
        "commitments"
    ]
