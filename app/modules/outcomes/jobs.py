"""Durable work-product jobs with kind-specific readiness, handlers and timeouts."""
import asyncio
import hashlib
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, text

from app.core.database.mixins import now
from app.modules.identity.models.identity import User
from app.modules.meetings.models.archive import MeetingArchive
from app.modules.meetings.models.meeting import Meeting
from app.modules.meetings.services.minutes_details import legacy_details
from app.modules.outcomes.engine import analyze
from app.modules.outcomes.meter import MeteredProvider
from app.modules.outcomes.models import OutcomeRun

KIND = "role_outcome"
ASSISTANT_KIND = "assistant_query"
PREPARATION_KIND = "meeting_preparation"
KIND_POLICY = {
    KIND: {"readiness": "meeting_archive", "handler": "role_outcome", "result": "role_report", "timeout": 150, "retrySeconds": 30},
    "meeting_question": {"readiness": "meeting_archive", "handler": "meeting_question", "result": "question_analysis", "timeout": 90, "retrySeconds": 5},
    ASSISTANT_KIND: {"readiness": "active_account", "handler": "assistant_query", "result": "evidence_answer", "timeout": 100, "retrySeconds": 5},
    PREPARATION_KIND: {"readiness": "calendar_authority", "handler": "meeting_preparation", "result": "pre_meeting_brief", "timeout": 90, "retrySeconds": 10},
}


def intent(meeting_id, revision, request_key="automatic"):
    return hashlib.sha256(f"{KIND}:{meeting_id}:{revision}:{request_key}".encode()).hexdigest()


def ready(meeting, archive):
    return bool(
        meeting
        and not meeting.deleted
        and archive
        and meeting.content.get("ended")
        and meeting.revision == archive.revision
        and (archive.details or getattr(archive, "markdown", "").strip())
        and meeting.content.get("utterances")
    )


async def enqueue(session, meeting, archive, *, request_key="automatic", request=""):
    if not ready(meeting, archive):
        return None
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": "outcome:" + meeting.owner_id})
    key = intent(meeting.id, archive.revision, request_key)
    if request_key == "automatic":
        previous = await session.scalar(select(OutcomeRun).where(
            OutcomeRun.owner_id == meeting.owner_id,
            OutcomeRun.meeting_id == meeting.id,
            OutcomeRun.content["automatic"].as_boolean().is_(True),
        ).order_by(OutcomeRun.version).limit(1))
        if previous:
            return previous
    existing = await session.scalar(select(OutcomeRun).where(
        OutcomeRun.owner_id == meeting.owner_id, OutcomeRun.intent_key == key
    ))
    if existing:
        return existing
    count = await session.scalar(select(func.count()).select_from(OutcomeRun).where(
        OutcomeRun.owner_id == meeting.owner_id,
        OutcomeRun.content["kind"].astext == KIND,
        OutcomeRun.content["createdAt"].astext >= (now() - timedelta(days=1)).isoformat(),
    ))
    if count >= 12:
        return None
    previous_version = await session.scalar(select(func.max(OutcomeRun.version)).where(
        OutcomeRun.owner_id == meeting.owner_id, OutcomeRun.meeting_id == meeting.id
    ))
    row = OutcomeRun(
        id=str(uuid.uuid4()),
        owner_id=meeting.owner_id,
        meeting_id=meeting.id,
        intent_key=key,
        version=(previous_version or 0) + 1,
        status="queued",
        content={
            "automatic": request_key == "automatic",
            "kind": KIND,
            "clientId": meeting.client_id,
            "revision": archive.revision,
            "attempts": 0,
            "createdAt": now().isoformat(),
            "request": request[:1000],
            "delivery": {"status": "not_sent", "reason": "wechat_binding_unavailable"},
        },
    )
    session.add(row)
    await session.flush()
    return row


async def _meeting_payload(session, row, meeting, archive, user):
    evidence = {
        "utterance:" + utterance["id"]: {
            "text": utterance.get("correction") or utterance["raw"],
            "at": utterance["at"],
            "speaker": utterance.get("speaker"),
        }
        for utterance in meeting.content["utterances"]
        if not utterance.get("withdrawn")
    }
    from app.modules.progress.models.progress_item import ProgressItem
    from app.modules.progress.services.progress_service import output as progress_output
    goal_rows = list(await session.scalars(select(ProgressItem).where(
        ProgressItem.owner_id == row.owner_id, ProgressItem.kind == "goal"
    ).limit(20)))
    goals = []
    for item in goal_rows:
        value = progress_output(item)
        if value.get("effectiveStatus", value.get("status")) in ["completed", "expired", "withdrawn"]:
            continue
        goals.append({key: value.get(key) for key in ["id", "title", "condition", "deadline", "status"]})
    from app.modules.identity.services.professional_profile import professional_profile
    profile = professional_profile(user.profile)
    profession = profile.get("outcome_profession")
    if not profession or profession.get("raw") != profile.get("job", ""):
        profession = {"raw": profile.get("job", "")}
    payload = {
        "goals": goals[:10],
        "profession": profession,
        "scenarios": (profile.get("personalization") or {}).get("scenarios", []),
        "title": archive.title,
        "minutes": {
            key: value for key, value in (
                archive.details or legacy_details(archive.markdown, meeting.content["utterances"])
            ).items() if not key.startswith("_")
        },
        "request": row.content.get("request", ""),
        "evidence": evidence,
    }
    parent_id = row.content.get("parentOutcomeId")
    if parent_id:
        parent = await session.scalar(select(OutcomeRun).where(
            OutcomeRun.id == parent_id,
            OutcomeRun.owner_id == row.owner_id,
            OutcomeRun.meeting_id == row.meeting_id,
            OutcomeRun.status == "succeeded",
        ))
        if parent:
            previous = parent.content.get("result") or {}
            payload["previous_outcome"] = {
                key: previous.get(key) for key in ["title", "summary", "next_steps", "data_gaps"]
            }
    question = row.content.get("question")
    if question:
        payload["markedQuestion"] = question
    return payload


class OutcomeJobs:
    def __init__(self, database, provider, settings=None):
        self.database, self.provider, self.settings, self.task = database, provider, settings, None
        self.running = set()

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.loop())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for task in self.running:
            task.cancel()
        if self.running:
            await asyncio.gather(*self.running, return_exceptions=True)
        self.running.clear()

    async def scan_questions(self):
        from app.modules.outcomes.questions import KIND as question_kind, queue
        async with self.database.sessions() as session:
            rows = list(await session.scalars(select(OutcomeRun).where(
                OutcomeRun.content["kind"].astext == question_kind,
                OutcomeRun.status == "question",
            ).limit(100)))
            for row in rows:
                meeting = await session.get(Meeting, row.meeting_id)
                if meeting and not meeting.deleted and meeting.owner_id == row.owner_id:
                    await queue(session, meeting, row)
            await session.commit()

    async def scan(self):
        # The same durable worker reconciles calendar preparations; no second poller.
        async with self.database.sessions() as session:
            from app.modules.outcomes.meeting_preparation import reconcile_preparations
            if self.settings is not None:
                await reconcile_preparations(session, self.settings)
            await session.commit()
        # Automatic role-outcome scanning remains meeting-only. Assistant queries are explicit.
        async with self.database.sessions() as session:
            users = list(await session.scalars(select(User).where(
                User.is_active.is_(True),
                User.profile["outcome_preferences"]["autoAnalyze"].as_boolean().is_(True),
            ).order_by(User.id).limit(100)))
            for user in users:
                since = (user.profile or {}).get("outcome_preferences", {}).get("enabledAt")
                if not since:
                    continue
                rows = (await session.execute(select(Meeting, MeetingArchive).join(
                    MeetingArchive, MeetingArchive.meeting_id == Meeting.id
                ).where(
                    Meeting.owner_id == user.id,
                    MeetingArchive.owner_id == user.id,
                    Meeting.deleted.is_(False),
                    MeetingArchive.updated_at >= datetime.fromisoformat(since),
                ).order_by(MeetingArchive.updated_at.desc()).limit(12))).all()
                for meeting, archive in rows:
                    started = meeting.content.get("started_at")
                    if not started:
                        continue
                    try:
                        beginning = datetime.fromisoformat(started)
                        if beginning.tzinfo is None:
                            beginning = beginning.replace(tzinfo=timezone.utc)
                        ended = beginning + timedelta(milliseconds=meeting.content.get("elapsed_ms", 0))
                        if ended < datetime.fromisoformat(since):
                            continue
                    except (ValueError, TypeError, OverflowError):
                        continue
                    await enqueue(session, meeting, archive)
            await session.commit()

    async def loop(self):
        last_scan = 0
        while True:
            try:
                if time.monotonic() - last_scan > 20:
                    await self.scan()
                    await self.scan_questions()
                    last_scan = time.monotonic()
                finished = {task for task in self.running if task.done()}
                for task in finished:
                    self.running.remove(task)
                    error = task.exception()
                    if error:
                        logging.getLogger(__name__).warning("outcome_worker_error", extra={"error_type": type(error).__name__})
                # Bounded parallel jobs keep one slow or malformed model response from
                # blocking unrelated accounts. Durable leases still prevent duplicate work.
                if len(self.running) < 3:
                    task = asyncio.create_task(self.process_one())
                    self.running.add(task)
                await asyncio.sleep(0.5 if self.running else 3)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logging.getLogger(__name__).warning("outcome_worker_error", extra={"error_type": type(error).__name__})
                await asyncio.sleep(3)

    async def process_one(self):
        async with self.database.engine.connect() as lock_connection:
            return await self._process_one(lock_connection)

    async def _unlock(self, connection):
        await connection.execute(text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": "agenote:outcome-worker"})
        await connection.commit()

    async def _process_one(self, lock_connection):
        locked = False
        async with self.database.sessions() as session:
            locked = bool(await lock_connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": "agenote:outcome-worker"}
            ))
            if not locked:
                return False
            try:
                stamp = now().isoformat()
                next_at = OutcomeRun.content["nextAt"].astext
                lease_until = OutcomeRun.content["leaseUntil"].astext
                rows = list(await session.scalars(select(OutcomeRun).where(
                    OutcomeRun.content["kind"].astext.in_(list(KIND_POLICY)),
                    OutcomeRun.status.in_(["queued", "retryable", "running"]),
                    or_(next_at.is_(None), next_at == "", next_at <= stamp),
                    or_(OutcomeRun.status != "running", lease_until.is_(None), lease_until == "", lease_until <= stamp),
                ).order_by(OutcomeRun.content["createdAt"].astext).limit(30)))
                row = rows[0] if rows else None
                if row is None:
                    return False
                if row.content.get("attempts", 0) >= 3:
                    row.status = "failed"
                    await session.commit()
                    return True
                kind = row.content.get("kind")
                policy = KIND_POLICY[kind]
                if (kind == PREPARATION_KIND and self.settings is not None
                        and not getattr(self.settings, "meeting_preparation_enabled", False)):
                    row.status = "cancelled"
                    row.content = {**row.content, "terminalReason": "meeting_preparation_disabled",
                                   "finishedAt": now().isoformat(),
                                   "delivery": {"status": "suppressed", "reason": "meeting_preparation_disabled"}}
                    await session.commit()
                    return True
                user = await session.get(User, row.owner_id)
                meeting = archive = payload = None
                revision = None
                if not user or not user.is_active:
                    row.status = "stale"
                    await session.commit()
                    return True
                if policy["readiness"] == "calendar_authority":
                    from app.modules.outcomes.meeting_preparation import preparation_context
                    payload, invalid_reason = await preparation_context(session, row)
                    if payload is None:
                        row.status = "stale"
                        row.content = {**row.content, "terminalReason": invalid_reason,
                                       "finishedAt": now().isoformat(),
                                       "delivery": {"status": "stale", "reason": invalid_reason}}
                        await session.commit()
                        return True
                    revision = row.content.get("actionVersion")
                elif policy["readiness"] == "meeting_archive":
                    meeting = await session.get(Meeting, row.meeting_id)
                    archive = await session.scalar(select(MeetingArchive).where(
                        MeetingArchive.meeting_id == row.meeting_id,
                        MeetingArchive.owner_id == row.owner_id,
                    ))
                    if (not ready(meeting, archive) or meeting.owner_id != row.owner_id
                            or archive.revision != row.content.get("revision")):
                        row.status = "stale"
                        await session.commit()
                        return True
                    if row.content.get("automatic", False) and not (user.profile or {}).get(
                        "outcome_preferences", {}
                    ).get("autoAnalyze", False):
                        row.status = "cancelled"
                        await session.commit()
                        return True
                    payload = await _meeting_payload(session, row, meeting, archive, user)
                    revision = archive.revision

                row.status = "running"
                claim_id = str(uuid.uuid4())
                row.content = {
                    **row.content,
                    "claimId": claim_id,
                    "processingStartedAt": now().isoformat(),
                    "attempts": row.content.get("attempts", 0) + 1,
                    "leaseUntil": (now() + timedelta(minutes=5)).isoformat(),
                }
                key, owner = row.id, row.owner_id
                mutation = row.content.get("mutationId")
                content_snapshot = dict(row.content)
                profile_snapshot = dict(user.profile or {})
                await session.commit()
                # The global claim lock is not held across a model call. The durable lease
                # prevents duplicate work while another worker can serve another account.
                await self._unlock(lock_connection)
                locked = False

                started = time.monotonic()
                metered = MeteredProvider(self.provider, key)
                timing = None
                classification = None
                try:
                    if policy["handler"] == "meeting_question":
                        from app.modules.outcomes.question_engine import analyze_question
                        result = await asyncio.wait_for(analyze_question(metered, session, owner, payload), policy["timeout"])
                    elif policy["handler"] == "assistant_query":
                        from app.modules.outcomes.assistant_query import answer_query
                        result, timing, classification = await asyncio.wait_for(
                            answer_query(metered, session, owner, content_snapshot, profile_snapshot),
                            policy["timeout"],
                        )
                    elif policy["handler"] == "meeting_preparation":
                        from app.modules.outcomes.meeting_preparation import prepare_meeting
                        result, timing = await asyncio.wait_for(
                            prepare_meeting(metered, session, owner, payload, profile_snapshot),
                            policy["timeout"],
                        )
                    elif policy["handler"] == "role_outcome":
                        result = await asyncio.wait_for(analyze(metered, session, owner, payload, repair=True), policy["timeout"])
                    else:
                        raise ValueError("unknown_outcome_handler")
                    status = "skipped" if result.get("skipped") else "succeeded"
                    failure = None
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    await session.rollback()
                    result = None
                    status = "retryable"
                    known = {
                        "invalid_evidence_reference", "no_verified_citations", "invalid_tool_plan",
                        "invalid_question_evidence", "invalid_coverage", "missing_question_evidence",
                        "invalid_assistant_query_quote", "missing_assistant_query_evidence",
                        "invalid_assistant_query_conflict", "invalid_assistant_query_coverage",
                        "model_generated_link", "ai_derived_only_claim",
                        "invalid_preparation_quote", "invalid_preparation_reference",
                        "ai_derived_only_preparation_claim", "unknown_outcome_handler",
                    }
                    failure = str(error) if str(error) in known else type(error).__name__

                row = await session.get(OutcomeRun, key, populate_existing=True)
                if row is None:
                    return True
                if row.status != "running" or row.content.get("claimId") != claim_id:
                    return True
                if mutation and row.content.get("mutationId") != mutation:
                    return True
                if policy["readiness"] == "calendar_authority":
                    from app.modules.outcomes.meeting_preparation import preparation_context
                    current_action, invalid_reason = await preparation_context(session, row)
                    if current_action is None or current_action.version != revision:
                        status, result = "stale", None
                        row.content = {**row.content, "terminalReason": invalid_reason or "action_version_changed"}
                elif policy["readiness"] == "meeting_archive":
                    meeting = await session.get(Meeting, row.meeting_id, populate_existing=True)
                    archive = await session.scalar(select(MeetingArchive).where(
                        MeetingArchive.meeting_id == row.meeting_id,
                        MeetingArchive.owner_id == owner,
                    ).execution_options(populate_existing=True))
                    if not ready(meeting, archive) or archive.revision != revision:
                        status, result = "stale", None
                else:
                    current_user = await session.get(User, owner, populate_existing=True)
                    if not current_user or not current_user.is_active:
                        status, result = "stale", None
                if failure and row.content.get("attempts", 0) >= 3:
                    status = "failed"
                extra = {}
                if timing is not None:
                    extra["timing"] = {**(row.content.get("timing") or {}), **timing,
                                       "finishedAt": now().isoformat()}
                if classification is not None:
                    inbound = dict(row.content.get("inbound") or {})
                    inbound["classifiedAt"] = now().isoformat()
                    extra.update(classification=classification, timing=timing, inbound=inbound)
                row.status = status
                row.content = {
                    **row.content,
                    **extra,
                    "result": result,
                    "error": failure,
                    "nextAt": (now() + timedelta(seconds=policy["retrySeconds"] * row.content["attempts"])).isoformat(),
                    "lastFailure": failure or row.content.get("lastFailure"),
                    "finishedAt": now().isoformat(),
                    "elapsedMs": round((time.monotonic() - started) * 1000),
                    "cost": {"calls": metered.calls, "currencyCost": None, "status": "sdk_usage_unavailable"},
                }
                await session.commit()
                return True
            finally:
                if locked:
                    await self._unlock(lock_connection)
