"""One fail-closed definition of a current, verified future calendar arrangement."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from sqlalchemy import or_, select
from app.core.database.mixins import now
from app.modules.actions.models import ActionRun
from app.modules.meetings.models.meeting import Meeting

CALENDAR_OPERATIONS = {"create", "reschedule"}


def _instant(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def receipt_matches_command(row: ActionRun, receipt: dict | None = None) -> bool:
    content = row.content or {}
    result = receipt if receipt is not None else content.get("result")
    if not isinstance(result, dict) or result.get("verified") is not True:
        return False
    if not result.get("eventId"):
        return False
    if any(result.get(key) != content.get(key) for key in ("title", "start", "end", "fingerprint")):
        return False
    if not content.get("deviceId") or result.get("deviceId") != content.get("deviceId"):
        return False
    start, end = _instant(row.starts_at), _instant(row.ends_at)
    if start is None or end is None or end <= start:
        return False
    if _instant(content.get("start")) != start or _instant(content.get("end")) != end:
        return False
    try:
        ZoneInfo(row.time_zone or "")
    except (ZoneInfoNotFoundError, ValueError):
        return False
    operation = row.operation or content.get("operation") or "create"
    if operation == "create":
        return result.get("deleted") is False and not result.get("previousEventId")
    target = content.get("target") or {}
    if not target.get("eventId") or not result.get("calendarId"):
        return False
    if result.get("previousEventId") != target.get("eventId"):
        return False
    if result.get("eventId") != target.get("eventId"):
        return False
    if target.get("calendarId") and result.get("calendarId") != target.get("calendarId"):
        return False
    if operation == "reschedule":
        return result.get("deleted") is False
    if operation == "cancel":
        return result.get("deleted") is True
    return False


def receipt_matches_approval(row: ActionRun) -> bool:
    return row.operation in CALENDAR_OPERATIONS and receipt_matches_command(row)


async def authoritative_action(
    session,
    owner: str,
    action_id: str,
    *,
    at: datetime | None = None,
    require_future: bool = True,
):
    moment = at or now()
    row = await session.scalar(
        select(ActionRun)
        .join(Meeting, Meeting.id == ActionRun.meeting_id)
        .where(
            ActionRun.id == action_id,
            ActionRun.owner_id == owner,
            Meeting.owner_id == owner,
            Meeting.deleted.is_(False),
            ActionRun.operation.in_(CALENDAR_OPERATIONS),
            ActionRun.status == "completed",
            ActionRun.verified_at.is_not(None),
            or_(ActionRun.content["kind"].astext.is_(None), ActionRun.content["kind"].astext != "tool_plan"),
        )
    )
    if row is None or row.starts_at is None or row.ends_at is None:
        return None
    starts_at = _instant(row.starts_at)
    if starts_at is None or (require_future and starts_at <= moment.astimezone(timezone.utc)):
        return None
    if not receipt_matches_approval(row):
        return None
    successors = list(await session.scalars(
        select(ActionRun).where(
            ActionRun.owner_id == owner,
            ActionRun.supersedes_id == row.id,
            ActionRun.operation.in_(["reschedule", "cancel"]),
            ActionRun.status == "completed",
            ActionRun.verified_at.is_not(None),
        ).limit(20)
    ))
    return None if any(receipt_matches_command(successor) for successor in successors) else row


async def list_authoritative_actions(session, owner: str, *, at: datetime | None = None, limit: int = 100):
    moment = at or now()
    candidates = list(await session.scalars(
        select(ActionRun)
        .join(Meeting, Meeting.id == ActionRun.meeting_id)
        .where(
            ActionRun.owner_id == owner,
            Meeting.owner_id == owner,
            Meeting.deleted.is_(False),
            ActionRun.operation.in_(CALENDAR_OPERATIONS),
            ActionRun.status == "completed",
            ActionRun.verified_at.is_not(None),
            ActionRun.starts_at > moment,
            or_(ActionRun.content["kind"].astext.is_(None), ActionRun.content["kind"].astext != "tool_plan"),
        )
        .order_by(ActionRun.starts_at, ActionRun.id)
        .limit(max(1, min(limit, 200)))
    ))
    current = []
    for candidate in candidates:
        value = await authoritative_action(session, owner, candidate.id, at=moment)
        if value is not None:
            current.append(value)
    return current


def authority_output(row: ActionRun) -> dict:
    return {
        "id": row.id,
        "version": row.version,
        "status": row.status,
        "kind": "calendar",
        "operation": row.operation,
        "supersedesId": row.supersedes_id,
        "start": row.content.get("start"),
        "end": row.content.get("end"),
        "title": row.content.get("title"),
        "timeZone": row.time_zone,
        "agenda": row.agenda,
        "fingerprint": row.content.get("fingerprint"),
        "verifiedAt": row.verified_at.isoformat() if row.verified_at else None,
        "authority": "authenticated_device_receipt_fields_matched",
    }
