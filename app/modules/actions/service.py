"""Calendar approval commands with typed, owner-scoped authority and atomic preparation enqueue."""
import hashlib
import json
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import HTTPException
from sqlalchemy import select, text
from app.core.database.mixins import now
from app.modules.actions.models import ActionRun
from app.modules.meetings.models.meeting import Meeting
from app.modules.meetings.services.business_guard import fingerprint as source_fingerprint


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _instant(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            raise HTTPException(422, "invalid_calendar_time")
    if parsed.tzinfo is None:
        raise HTTPException(422, "invalid_calendar_time")
    return parsed.astimezone(timezone.utc)


def _zone(value):
    name = str(value or "UTC")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(422, "invalid_time_zone")
    return name


def _mutation_hash(operation, value, device_id):
    return digest({"operation": operation, "value": value, "deviceId": device_id})


class ActionService:
    def __init__(self, session, settings, database=None):
        self.session, self.settings = session, settings
        self.database = database

    async def project(self, meeting, intents):
        sources = {u["id"]: u for u in meeting.content["utterances"]}
        existing = list(await self.session.scalars(select(ActionRun).where(
            ActionRun.owner_id == meeting.owner_id,
            ActionRun.meeting_id == meeting.id,
        )))
        for intent in intents:
            ids = intent["evidence_ids"]
            if intent["status"] != "confirmed" or not all(
                i in sources and not sources[i].get("withdrawn")
                and sources[i].get("turn_status", "complete") == "complete" for i in ids
            ):
                continue
            start, end = _instant(intent["start"]), _instant(intent["end"])
            if start <= now() or end <= start:
                continue
            time_zone = _zone(intent.get("timeZone") or "Asia/Shanghai")
            agenda = str(intent["agenda"]).strip()[:4000] if intent.get("agenda") else None
            start_text = intent["start"].isoformat() if isinstance(intent["start"], datetime) else str(intent["start"])
            end_text = intent["end"].isoformat() if isinstance(intent["end"], datetime) else str(intent["end"])
            content = {
                "operation": "create",
                "title": intent["title"],
                "start": start_text,
                "end": end_text,
                "timeZone": time_zone,
                "agenda": agenda,
                "evidence_ids": ids,
                "sources": {i: digest(sources[i]["raw"]) for i in ids},
            }
            content["fingerprint"] = digest(content)
            key = digest([
                "calendar", "create", "".join(content["title"].split()),
                start.isoformat(), end.isoformat(), time_zone, agenda,
            ])
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"intent:{meeting.owner_id}:{key}"},
            )
            duplicate = await self.session.scalar(select(ActionRun).where(
                ActionRun.owner_id == meeting.owner_id, ActionRun.intent_key == key
            ))
            if duplicate is not None:
                continue
            for old in existing:
                if old.content.get("kind") == "tool_plan":
                    continue
                if old.status not in {"completed", "declined", "superseded", "cancelled"} and set(
                    old.content.get("evidence_ids", [])
                ) & set(ids):
                    old.status = "superseded"
                    old.version += 1
                    old.updated_at = now()
            stamp = now()
            row = ActionRun(
                id=str(uuid.uuid4()), owner_id=meeting.owner_id, meeting_id=meeting.id,
                intent_key=key, version=1, status="pending", content=content,
                operation="create", starts_at=start, ends_at=end, time_zone=time_zone,
                agenda=agenda, created_at=stamp, updated_at=stamp,
            )
            self.session.add(row)
            existing.append(row)
            await self.session.flush()

    async def list(self, owner, include_tool_plans=False):
        rows = list(await self.session.scalars(select(ActionRun).where(ActionRun.owner_id == owner)))
        return [self.output(row) for row in rows if include_tool_plans or row.content.get("kind") != "tool_plan"]

    def output(self, row):
        value = {
            "id": row.id, "version": row.version, "status": row.status,
            "kind": "tool_plan" if row.content.get("kind") == "tool_plan" else "calendar",
            **row.content,
        }
        if row.content.get("kind") != "tool_plan":
            value.update(
                operation=row.operation or row.content.get("operation") or "create",
                supersedesId=row.supersedes_id,
                timeZone=row.time_zone or row.content.get("timeZone") or "UTC",
                agenda=row.agenda,
                verifiedAt=row.verified_at.isoformat() if row.verified_at else None,
                createdAt=row.created_at.isoformat() if row.created_at else None,
                updatedAt=row.updated_at.isoformat() if row.updated_at else None,
            )
        return value

    async def _lock(self, owner, action_id):
        await self.session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {
            "key": f"action:{owner}:{action_id}"
        })
        row = await self.session.scalar(select(ActionRun).where(
            ActionRun.id == action_id, ActionRun.owner_id == owner
        ).with_for_update())
        if row is None:
            raise HTTPException(404, "action_not_found")
        return row

    async def _source_current(self, row):
        meeting = await self.session.scalar(select(Meeting).where(
            Meeting.id == row.meeting_id, Meeting.owner_id == row.owner_id, Meeting.deleted.is_(False)
        ))
        if meeting is None:
            raise HTTPException(409, "action_source_changed")
        sources = {u["id"]: u for u in meeting.content.get("utterances", [])}
        withdrawn = {v["target_id"] for v in (meeting.analysis or {}).get("withdrawals", [])}
        withdrawn.update(v["target_id"] for v in (meeting.analysis or {}).get("withdrawal_candidates", []))
        current_hashes = {
            u["id"]: source_fingerprint([u["raw"], u.get("correction"), u.get("withdrawn")])
            for u in meeting.content.get("utterances", [])
        }
        same_facts = (meeting.analysis or {}).get("source_hashes") == current_hashes
        from app.modules.actions.source_snapshot import snapshot_current
        prepared_current = snapshot_current(row.content.get("prepared_source_hashes"), list(sources.values()))
        if (meeting.analysis_revision != meeting.revision and not same_facts and not prepared_current) or any(
            source_id not in sources or sources[source_id].get("withdrawn") or source_id in withdrawn
            or digest(sources[source_id]["raw"]) != raw_hash
            for source_id, raw_hash in row.content.get("sources", {}).items()
        ):
            raise HTTPException(409, "action_source_changed")
        return meeting

    def _replayed(self, row, mutation_id, operation, value, device_id):
        if not mutation_id:
            return False
        expected = _mutation_hash(operation, value, device_id)
        for item in row.content.get("processedMutations", []):
            if item.get("id") == mutation_id:
                if item.get("hash") != expected:
                    raise HTTPException(409, "action_mutation_conflict")
                return True
        return False

    def _record(self, row, mutation_id, operation, value, device_id):
        if not mutation_id:
            return
        entries = list(row.content.get("processedMutations", []))
        entries.append({"id": mutation_id, "hash": _mutation_hash(operation, value, device_id)})
        row.content = {**row.content, "processedMutations": entries[-20:]}

    async def create_mutation(self, owner, action_id, operation, current_version, request_id, **changes):
        if operation not in {"reschedule", "cancel"}:
            raise HTTPException(422, "invalid_calendar_operation")
        target = await self._lock(owner, action_id)
        key = digest(["calendar_mutation_request", owner, request_id])
        duplicate = await self.session.scalar(select(ActionRun).where(
            ActionRun.owner_id == owner, ActionRun.intent_key == key
        ))
        if duplicate is not None:
            saved_target = duplicate.content.get("target") or {}
            if (duplicate.operation != operation or duplicate.supersedes_id != target.id
                    or saved_target.get("actionVersion") != current_version):
                raise HTTPException(409, "action_mutation_conflict")
            if operation == "reschedule":
                expected_title = str(changes.get("title") or target.content.get("title") or "").strip()
                expected_agenda = changes.get("agenda", target.agenda)
                expected_agenda = str(expected_agenda).strip()[:4000] if expected_agenda else None
                if any([
                    duplicate.content.get("start") != changes.get("start"),
                    duplicate.content.get("end") != changes.get("end"),
                    duplicate.content.get("timeZone") != changes.get("timeZone"),
                    duplicate.content.get("title") != expected_title,
                    duplicate.content.get("agenda") != expected_agenda,
                ]):
                    raise HTTPException(409, "action_mutation_conflict")
            return self.output(duplicate)
        from app.modules.actions.calendar_authority import authoritative_action
        current = await authoritative_action(self.session, owner, target.id)
        if current is None:
            raise HTTPException(409, "action_not_authoritative")
        if target.version != current_version:
            raise HTTPException(409, "action_version_conflict")
        active_successor = await self.session.scalar(select(ActionRun.id).where(
            ActionRun.owner_id == owner,
            ActionRun.supersedes_id == target.id,
            ActionRun.status.in_(["pending", "awaiting_device", "retryable"]),
        ).limit(1))
        if active_successor is not None:
            raise HTTPException(409, "action_mutation_pending")
        target_result = target.content.get("result") or {}
        if operation == "reschedule":
            start, end = _instant(changes.get("start")), _instant(changes.get("end"))
            if start <= now() or end <= start:
                raise HTTPException(422, "invalid_calendar_time")
            zone = _zone(changes.get("timeZone"))
            title = str(changes.get("title") or target.content.get("title") or "").strip()
            if not title:
                raise HTTPException(422, "invalid_calendar_title")
            agenda = changes.get("agenda", target.agenda)
            agenda = str(agenda).strip()[:4000] if agenda else None
            start_text, end_text = changes.get("start"), changes.get("end")
        else:
            start, end, zone = target.starts_at, target.ends_at, target.time_zone
            title, agenda = target.content.get("title"), target.agenda
            start_text, end_text = target.content.get("start"), target.content.get("end")
        command = {
            "operation": operation,
            "title": title,
            "start": start_text,
            "end": end_text,
            "timeZone": zone,
            "agenda": agenda,
            "evidence_ids": list(target.content.get("evidence_ids", [])),
            "sources": dict(target.content.get("sources", {})),
            "target": {
                "actionId": target.id, "actionVersion": target.version,
                "fingerprint": target.content.get("fingerprint"),
                "eventId": target_result.get("eventId"),
                "calendarId": target_result.get("calendarId"),
                "deviceId": target.content.get("deviceId"),
            },
            "requestId": request_id,
        }
        fingerprint_value = {key_name: command[key_name] for key_name in [
            "operation", "title", "start", "end", "timeZone", "agenda", "target"
        ]}
        command["fingerprint"] = digest(fingerprint_value)
        stamp = now()
        row = ActionRun(
            id=str(uuid.uuid4()), owner_id=owner, meeting_id=target.meeting_id,
            intent_key=key, version=1, status="pending", content=command,
            operation=operation, supersedes_id=target.id, starts_at=start,
            ends_at=end, time_zone=zone, agenda=agenda, created_at=stamp, updated_at=stamp,
        )
        self.session.add(row)
        await self.session.flush()
        await self.session.commit()
        return self.output(row)

    def _receipt_matches(self, row, receipt, target):
        from app.modules.actions.calendar_authority import receipt_matches_command
        return receipt_matches_command(row, receipt)

    async def advance(
        self, owner, action_id, operation, fingerprint, value=None, device_id=None,
        version=None, mutation_id=None,
    ):
        row = await self._lock(owner, action_id)
        if row.content.get("kind") == "tool_plan":
            from app.modules.execution.service import ExecutionService
            return await ExecutionService(self.session, self.database, self.settings).advance(
                row, operation, fingerprint, value
            )
        if row.content.get("fingerprint") != fingerprint:
            raise HTTPException(409, "action_changed")
        if self._replayed(row, mutation_id, operation, value, device_id):
            return self.output(row)
        if version is not None and row.version != version:
            raise HTTPException(409, "action_version_conflict")
        bound_device = row.content.get("deviceId")
        if bound_device and bound_device != device_id:
            raise HTTPException(409, "action_device_mismatch")
        if operation == "decision":
            if row.status in {"completed", "awaiting_device"} and value is True and bound_device == device_id:
                return self.output(row)
            if row.status == "declined" and value is False:
                return self.output(row)
            if row.status != "pending":
                raise HTTPException(409, "action_invalid_transition")
            if value is True and row.operation in {"reschedule", "cancel"}:
                target = await self._lock(owner, row.supersedes_id)
                from app.modules.actions.calendar_authority import authoritative_action
                current = await authoritative_action(self.session, owner, target.id)
                target_snapshot = row.content.get("target") or {}
                expected_version = target_snapshot.get("actionVersion")
                if current is None or target.version != expected_version:
                    raise HTTPException(409, "action_version_conflict")
                if target_snapshot.get("deviceId") and target_snapshot.get("deviceId") != device_id:
                    raise HTTPException(409, "action_device_mismatch")
            await self._source_current(row)
            if row.starts_at is None or row.starts_at <= now():
                raise HTTPException(409, "action_expired")
            stamp = now()
            if value is True:
                row.content = {**row.content, "deviceId": device_id}
                row.status = "awaiting_device"
            else:
                row.status = "declined"
            row.version += 1
            row.updated_at = stamp
            self._record(row, mutation_id, operation, value, device_id)
            await self.session.commit()
            return self.output(row)
        if operation != "receipt":
            raise HTTPException(409, "action_invalid_transition")
        if not bound_device or row.status not in {"awaiting_device", "retryable"}:
            if row.status == "completed":
                incoming = dict(value or {})
                incoming.pop("version", None)
                incoming.pop("mutationId", None)
                saved = row.content.get("result") or {}
                if all(saved.get(key) == item for key, item in incoming.items()):
                    return self.output(row)
            raise HTTPException(409, "action_invalid_transition")
        await self._source_current(row)
        if row.starts_at is None or row.starts_at <= now():
            raise HTTPException(409, "action_expired")
        target = None
        if row.operation in {"reschedule", "cancel"}:
            target = await self._lock(owner, row.supersedes_id)
            from app.modules.actions.calendar_authority import authoritative_action
            current = await authoritative_action(self.session, owner, target.id)
            expected_version = (row.content.get("target") or {}).get("actionVersion")
            if current is None or target.version != expected_version:
                raise HTTPException(409, "action_version_conflict")
        receipt = dict(value or {})
        receipt.pop("version", None)
        receipt.pop("mutationId", None)
        if not self._receipt_matches(row, receipt, target):
            row.content = {**row.content, "result": receipt,
                           "verification": "authenticated_device_receipt_rejected"}
            self._record(row, mutation_id, operation, value, device_id)
            row.updated_at = now()
            await self.session.commit()
            return self.output(row)
        stamp = now()
        receipt["verificationMeaning"] = "authenticated_device_report_fields_matched_not_server_calendar_read"
        row.content = {**row.content, "result": receipt,
                       "verification": "authenticated_device_receipt_fields_matched"}
        row.status = "completed"
        row.verified_at = stamp
        row.version += 1
        row.updated_at = stamp
        self._record(row, mutation_id, operation, value, device_id)
        if target is not None:
            target.status = "superseded" if row.operation == "reschedule" else "cancelled"
            target.version += 1
            target.updated_at = stamp
            from app.modules.outcomes.meeting_preparation import invalidate_action_preparations
            await invalidate_action_preparations(
                self.session, target.id, cancelled=row.operation == "cancel", at=stamp
            )
        # autoflush=False: make the new authority visible to the shared validator
        # before deriving the preparation in this same database transaction.
        await self.session.flush()
        if row.operation in {"create", "reschedule"} and getattr(
            self.settings, "meeting_preparation_enabled", False
        ):
            from app.modules.outcomes.meeting_preparation import enqueue_preparation
            await enqueue_preparation(
                self.session, row, self.settings, scheduler_received_at=stamp
            )
        await self.session.commit()
        return self.output(row)
