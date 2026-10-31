"""Unified, owner-scoped, read-only retrieval across indexed Agenote data."""
from datetime import datetime
from urllib.parse import urlencode, quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import DateTime, Text, and_, or_, select

from app.modules.execution.models import InternalTask
from app.modules.knowledge.schemas.knowledge import CitationInput
from app.modules.knowledge.services.knowledge_service import KnowledgeService, effective_documents
from app.modules.meetings.models.meeting import Meeting
from app.modules.outcomes.models import OutcomeRun
from app.modules.progress.models.progress_item import ProgressItem
from app.modules.progress.services.progress_service import output
from app.modules.search.relevance import METHOD, excerpt, relevance, terms


class UnifiedSearch:
    def __init__(self, session):
        self.session = session
        self.knowledge = KnowledgeService(session)

    async def search(
        self,
        owner,
        query,
        limit=24,
        kinds=None,
        project=None,
        agent=False,
        current_session=None,
        *,
        meeting_ids=None,
        date_from=None,
        date_to=None,
        time_zone="Asia/Shanghai",
        scope_listing=False,
    ):
        query = query.strip()[:4000]
        if not terms(query):
            return []
        kinds = set(kinds or ["document", "goal", "commitment", "task", "utterance", "meeting", "outcome"])
        if meeting_ids is not None:
            requested_ids = list(dict.fromkeys(meeting_ids))
            if not requested_ids:
                return []
            owned = (await self.session.execute(select(Meeting.id, Meeting.client_id).where(
                Meeting.owner_id == owner,
                Meeting.deleted.is_(False),
                or_(Meeting.id.in_(requested_ids), Meeting.client_id.in_(requested_ids)),
            ))).all()
            if not owned:
                return []
            meeting_ids = [row.id for row in owned]
        hits = []

        def append(kind, key, title, text_value, path, score_text=None, forced_score=None, **source):
            score = forced_score if forced_score is not None else relevance(
                query, score_text if score_text is not None else title + "\n" + text_value
            )
            if not score or score < 12:
                return
            hits.append({
                "kind": kind,
                "id": key,
                "title": title,
                "text": excerpt(text_value, query),
                "path": path,
                "matchPercent": score,
                "matchMethod": METHOD,
                "scoreMeaning": "词面相关度，不表示内容正确或已批准",
                **source,
            })

        if "document" in kinds:
            for hit in await self.knowledge.search(owner, query, project, limit=min(12, limit)):
                path = "/knowledge/documents/" + quote(hit["documentId"], safe="") + "?" + urlencode({
                    "version": hit["versionId"], "hash": hit["hash"]
                })
                append("document", hit["nodeId"], hit["title"], hit["text"], path,
                       citation=hit, requiresReview=hit.get("extraction") == "ocr")

        needles = sorted(terms(query), key=lambda value: (-len(value), value))[:32]

        def matching(column):
            return or_(*(column.cast(Text).ilike(
                "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",
                escape="\\",
            ) for value in needles))

        if kinds & {"goal", "commitment"}:
            rows = (await self.session.scalars(select(ProgressItem).where(
                ProgressItem.owner_id == owner,
                ProgressItem.kind.in_(kinds & {"goal", "commitment"}),
                or_(matching(ProgressItem.content), matching(ProgressItem.overrides)),
            ).order_by(ProgressItem.updated_at.desc()).limit(300))).all()
            for row in rows:
                value = output(row)
                if value.get("status") == "withdrawn" or (project and value.get("projectId") != project):
                    continue
                evidence = value.get("evidence", {})
                if evidence.get("kind") == "document":
                    try:
                        await self.knowledge.resolve(owner, CitationInput.model_validate({
                            key: evidence[key] for key in ["documentId", "versionId", "nodeId", "hash"]
                        }))
                    except Exception:
                        continue
                body = "\n".join(str(value.get(key) or "") for key in ["condition", "acceptance", "summary", "deadline"])
                incomplete = not value.get("deadline") or (row.kind == "commitment" and value.get("side") not in {"ours", "theirs"})
                path = "/notifications" if incomplete else "/progress/" + ("goals" if row.kind == "goal" else "commitments") + "/" + row.id
                label = "待补充信息" if incomplete else ("待确认" if value.get("authorizationStatus") == "pending" else "")
                append(row.kind, row.id, value.get("title", ""), body, path,
                       version=row.version, status=value.get("status"), displayStatus=label,
                       evidence=evidence, business=value)

        if "task" in kinds and project is None:
            rows = (await self.session.scalars(select(InternalTask).where(
                InternalTask.owner_id == owner, matching(InternalTask.content)
            ).limit(200))).all()
            for row in rows:
                append("task", row.id, row.content["title"], row.content.get("status", ""), "/home",
                       version=row.version, status=row.content.get("status"))

        if "outcome" in kinds and project is None:
            outcome_kinds = ["role_outcome", "meeting_preparation", "assistant_query"]
            statement = select(OutcomeRun).where(
                OutcomeRun.owner_id == owner,
                OutcomeRun.status == "succeeded",
                OutcomeRun.content["kind"].astext.in_(outcome_kinds),
                matching(OutcomeRun.content),
            )
            if meeting_ids is not None:
                statement = statement.where(OutcomeRun.meeting_id.in_(meeting_ids))
            rows = list(await self.session.scalars(statement.order_by(
                OutcomeRun.content["createdAt"].astext.desc()).limit(100)))
            for row in rows:
                result = row.content.get("result") or {}
                source_kind = row.content.get("kind")
                body = "\n".join(filter(None, [
                    str(result.get("summary") or result.get("answer") or ""),
                    "\n".join(str(item.get("text") or "") for item in result.get("findings", [])[:6]
                              if isinstance(item, dict)),
                ]))[:6000]
                path = ("/assistant/queries/" if source_kind == "assistant_query" else "/assistant/outcomes/") + row.id
                append("outcome", row.id, str(result.get("title") or "AI 分析结果"), body, path,
                       aiDerived=True, sourceRunId=row.id, sourceVersion=row.version,
                       sourceKind=source_kind, sourcePath=path)

        if kinds & {"meeting", "utterance"} and project is None:
            library = await self.knowledge.repository.library(owner)
            allowed_sessions = {
                document["source"].split(":", 1)[1]
                for document, version in effective_documents(library.snapshot).values()
                if document["source"].startswith("meeting:")
            } if library else set()
            started = Meeting.content["started_at"].astext.cast(DateTime(timezone=True))
            clauses = [Meeting.owner_id == owner, Meeting.deleted.is_(False)]
            if not scope_listing:
                clauses.append(matching(Meeting.content))
            if meeting_ids is not None:
                if not meeting_ids:
                    clauses.append(Meeting.id.in_([]))
                else:
                    clauses.append(or_(Meeting.id.in_(meeting_ids), Meeting.client_id.in_(meeting_ids)))
            if date_from is not None:
                clauses.extend([Meeting.content["started_at"].astext.is_not(None), started >= date_from])
            if date_to is not None:
                clauses.extend([Meeting.content["started_at"].astext.is_not(None), started < date_to])
            rows = (await self.session.scalars(select(Meeting).where(*clauses).order_by(
                started.desc().nullslast(), Meeting.updated_at.desc()).limit(200))).all()
            try:
                zone = ZoneInfo(time_zone)
            except ZoneInfoNotFoundError:
                zone = ZoneInfo("Asia/Shanghai")
            for row in rows:
                if agent and row.client_id != current_session and row.client_id not in allowed_sessions:
                    continue
                title = row.content.get("title", "会议")
                started_at = row.content.get("started_at")
                occurred = None
                if started_at:
                    try:
                        occurred = datetime.fromisoformat(started_at.replace("Z", "+00:00")).astimezone(zone).date().isoformat()
                    except (ValueError, TypeError):
                        occurred = None
                common = {
                    "meetingId": row.id,
                    "sessionId": row.client_id,
                    "meetingTitle": title,
                    "startedAt": started_at,
                    "occurredDate": occurred,
                    "revision": row.revision,
                }
                if "meeting" in kinds:
                    append("meeting", row.client_id, title, title,
                           "/meeting?" + urlencode({"id": row.client_id}),
                           forced_score=100 if scope_listing else None, **common)
                if "utterance" in kinds:
                    for utterance in row.content.get("utterances", []):
                        if utterance.get("withdrawn"):
                            continue
                        raw = utterance.get("raw", "")
                        total = max(0, int(utterance.get("at", 0)) // 1000)
                        hours, remainder = divmod(total, 3600)
                        minutes, seconds = divmod(remainder, 60)
                        append(
                            "utterance",
                            row.client_id + ":" + utterance["id"],
                            title + " · " + utterance.get("speaker", "匿名发言人"),
                            raw,
                            "/meeting?" + urlencode({"id": row.client_id, "utterance": utterance["id"]}),
                            score_text=raw,
                            utteranceId=utterance["id"],
                            at=utterance.get("at", 0),
                            timestamp=f"{hours:02d}:{minutes:02d}:{seconds:02d}",
                            original=raw,
                            correction=utterance.get("correction"),
                            turnStatus=utterance.get("turn_status", "complete"),
                            speaker=utterance.get("speaker"),
                            **common,
                        )

        if "document" in kinds:
            checked = []
            for hit in hits:
                if hit["kind"] == "document":
                    try:
                        await self.knowledge.resolve(owner, CitationInput.model_validate({
                            key: hit["citation"][key] for key in ["documentId", "versionId", "nodeId", "hash"]
                        }))
                    except Exception:
                        continue
                checked.append(hit)
            hits = checked

        hits.sort(key=lambda hit: (-hit["matchPercent"], hit["kind"], hit["id"]))
        unique, seen, per_session = [], set(), {}
        for hit in hits:
            if hit["kind"] == "utterance":
                key = ("utterance", hit["sessionId"], hit["original"])
                if per_session.get(hit["sessionId"], 0) >= 4:
                    continue
            elif hit["kind"] == "document":
                key = ("document", hit["citation"]["documentId"])
            else:
                key = (hit["kind"], hit["id"])
            if key in seen:
                continue
            seen.add(key)
            if hit["kind"] == "utterance":
                per_session[hit["sessionId"]] = per_session.get(hit["sessionId"], 0) + 1
            unique.append(hit)
        selected, categories = [], set()
        for hit in unique:
            if hit["kind"] not in categories and len(selected) < limit:
                selected.append(hit)
                categories.add(hit["kind"])
        selected.extend(hit for hit in unique if hit not in selected)
        return sorted(selected[:limit], key=lambda hit: (-hit["matchPercent"], hit["kind"], hit["id"]))
