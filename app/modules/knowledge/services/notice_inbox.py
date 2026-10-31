"""账户范围的清除墓碑：不授权、不改变任务，仅阻止旧通知再次投递。"""
from sqlalchemy.dialects.postgresql import insert
from app.modules.knowledge.models.assistance import AssistanceReceipt
from app.modules.meetings.services.decision_questionnaire import digest

async def cleared_ids(session, owner):
    row = await session.get(AssistanceReceipt, digest([owner, "notice-inbox"]))
    return set(row.result.get("ids", [])) if row else set()

async def clear_ids(session, owner, ids):
    from app.modules.progress.services.progress_service import ProgressService
    await ProgressService(session)._lock_owner(owner)
    key = digest([owner, "notice-inbox"])
    await session.execute(insert(AssistanceReceipt).values(
        id=key, owner_id=owner, decision="cleared", result={"ids": []}
    ).on_conflict_do_nothing(index_elements=[AssistanceReceipt.id]))
    row = await session.get(AssistanceReceipt, key, with_for_update=True, populate_existing=True)
    await expire_progress(session, owner, ids)
    merged = set(row.result.get("ids", [])) | set(ids)
    row.result = {"ids": sorted(merged)}
    await session.commit()
    return sorted(merged)


async def expire_progress(session, owner, ids):
    from sqlalchemy import select
    from datetime import datetime, timezone
    from app.modules.progress.models.progress_item import ProgressItem
    from app.modules.progress.services.progress_service import output
    item_ids = [i[len("progress-pending:"):] for i in ids
                if i.startswith("progress-pending:") and not i.startswith("progress-pending:confirm-")]
    if item_ids:
        items = await session.scalars(select(ProgressItem).where(
            ProgressItem.owner_id == owner, ProgressItem.id.in_(item_ids)).with_for_update())
        for item in items:
            current = output(item)
            if item.kind not in {"goal", "commitment"} or current.get("deadline") or current.get("status") != "pending":
                continue
            if current.get("expiredByClear"):
                continue
            item.overrides = {**item.overrides, "expiredByClear": True}
            item.version += 1
            item.history = [*item.history, {"at": datetime.now(timezone.utc).isoformat(),
                "description": "Undated pending item expired when its notification was cleared",
                "actorUserId": owner, "version": item.version, "changes": {"expiredByClear": True}}]
