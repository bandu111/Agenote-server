from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from app.modules.progress.services.progress_service import output


def row(kind, status, deadline, overrides=None):
    return SimpleNamespace(kind=kind, content={"status": status, "deadline": deadline},
        overrides=overrides or {}, id="test", version=1, history=[], source_revision=1,
        source_key="commitment:test" if kind == "commitment" else "manual:test")


def test_expiry_does_not_confirm_or_complete():
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    item = output(row("commitment", "pending", past))
    assert item["status"] == "pending"
    assert item["effectiveStatus"] == "overdue"
    assert output(row("goal", "pending", past))["effectiveStatus"] == "expired"
    assert output(row("goal", "completed", past))["effectiveStatus"] == "completed"
    for status in ["verified", "withdrawn", "claimed"]:
        assert output(row("commitment", status, past, {"status": status}))["effectiveStatus"] == status


def test_missing_or_future_deadline_is_not_expired():
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    for deadline in [None, future, "invalid"]:
        assert output(row("commitment", "pending", deadline))["effectiveStatus"] == "pending"


async def test_clear_expires_only_owners_pending_undated_items():
    from unittest.mock import AsyncMock
    from app.modules.knowledge.services.notice_inbox import clear_ids
    receipt = SimpleNamespace(result={"ids": []})
    def entry(ident, deadline=None, status="pending"):
        return SimpleNamespace(id=ident, kind="goal", content={"status":status,"deadline":deadline},
            overrides={}, version=1, history=[], source_revision=1, source_key="manual:"+ident)
    expired = entry("undated")
    dated = entry("dated", "2099-01-01T00:00:00+00:00")
    done = entry("done",status="completed")
    session = SimpleNamespace(execute=AsyncMock(),get=AsyncMock(return_value=receipt),
        scalars=AsyncMock(return_value=[expired,dated,done]),commit=AsyncMock())
    await clear_ids(session,"owner",["progress-pending:undated","progress-pending:dated","progress-pending:done"])
    assert expired.overrides == {"expiredByClear": True}
    assert expired.version == 2
    assert dated.overrides == done.overrides == {}
    assert output(expired)["effectiveStatus"] == "expired"
    await clear_ids(session,"owner",["progress-pending:undated"])
    assert expired.version == 2
