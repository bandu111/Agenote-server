"""构建可追溯的只读业务上下文；不把候选误当已确认事项。"""

import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.modules.progress.models.progress_item import ProgressItem
from app.modules.progress.services.progress_service import output
from app.modules.actions.models import ActionRun
from app.modules.meetings.models.meeting import Meeting
from app.modules.knowledge.services.knowledge_service import (
    KnowledgeService,
    effective_documents,
)
from app.modules.knowledge.repositories.knowledge_repository import KnowledgeRepository
from app.modules.knowledge.schemas.knowledge import CitationInput
from app.modules.meetings.services.decision_questionnaire import digest

SHANGHAI = ZoneInfo("Asia/Shanghai")


def completed_in_window(event, window):
    if event.get("changes", {}).get("status") not in {"verified", "completed"}:
        return False
    try:
        stamp = datetime.fromisoformat(event["at"].replace("Z", "+00:00"))
        return (
            datetime.fromisoformat(window["review_start"])
            <= stamp
            < datetime.fromisoformat(window["review_end"])
        )
    except (KeyError, TypeError, ValueError):
        return False


def week_window(now):
    now = now.astimezone(SHANGHAI)
    start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return {
        "review_start": (start - timedelta(days=7)).isoformat(),
        "review_end": start.isoformat(),
        "plan_start": start.isoformat(),
        "plan_end": (start + timedelta(days=7)).isoformat(),
        "timezone": "Asia/Shanghai",
    }


async def collect(session, owner, now, lock=False):
    library = await KnowledgeRepository(session).library(owner)
    knowledge = KnowledgeService(session)
    rows = await session.scalars(
        select(ProgressItem)
        .where(
            ProgressItem.owner_id == owner,
            ProgressItem.kind.in_(["goal", "commitment"]),
        )
        .order_by(ProgressItem.id)
        .with_for_update()
        if lock
        else select(ProgressItem)
        .where(
            ProgressItem.owner_id == owner,
            ProgressItem.kind.in_(["goal", "commitment"]),
        )
        .order_by(ProgressItem.id)
    )
    records = []
    window = week_window(now)
    for row in rows:
        p = output(row)
        evidence = p.get("evidence", {})
        if evidence.get("kind") == "document":
            try:
                await knowledge.resolve(
                    owner,
                    CitationInput.model_validate(
                        {
                            k: evidence[k]
                            for k in ["documentId", "versionId", "nodeId", "hash"]
                        }
                    ),
                )
            except Exception:
                continue
        if p.get("status") == "withdrawn":
            continue
        confirmed = (
            row.source_key.startswith("manual:")
            or p.get("authorizationStatus") == "owner_confirmed"
        )
        records.append(
            {
                "id": "progress:" + row.id,
                "version": row.version,
                "kind": row.kind,
                "title": p["title"],
                "status": p["status"],
                "confirmed": confirmed,
                "side": p.get("side", "unknown"),
                "deadline": p.get("deadline"),
                "missing": p.get("missing", []),
                "updated_at": row.updated_at.isoformat(),
                "completed_in_review": any(
                    completed_in_window(event, window) for event in row.history
                ),
                "evidence": evidence,
                "acceptance": p.get("acceptance"),
                "sessionId": p.get("sessionId") or evidence.get("sessionId"),
            }
        )
    actions = list(
        await session.scalars(
            select(ActionRun).where(ActionRun.owner_id == owner).order_by(ActionRun.id)
        )
    )
    calendar = [
        {
            "id": "action:" + a.id,
            "version": a.version,
            "title": a.content.get("title", "日程"),
            "status": a.status,
            "start": a.content.get("start"),
            "end": a.content.get("end"),
        }
        for a in actions
        if a.status == "completed"
        and a.content.get("start")
        and datetime.fromisoformat(window["review_start"])
        <= datetime.fromisoformat(a.content["start"].replace("Z", "+00:00"))
        < datetime.fromisoformat(window["plan_end"])
    ]
    meetings = list(
        await session.scalars(
            select(Meeting)
            .where(Meeting.owner_id == owner)
            .order_by(Meeting.updated_at.desc())
            .limit(20)
        )
    )
    risks = []
    for m in meetings:
        report = m.analysis or {}
        if m.analysis_revision != m.revision or (
            report.get("knowledge_sources")
            and report.get("knowledge_revision") != (library.revision if library else 0)
        ):
            continue
        utterances = {
            u["id"]: u
            for u in m.content.get("utterances", [])
            if not u.get("withdrawn")
        }
        for risk in report.get("insights", []):
            ids = risk.get("evidence_ids", [])
            if (
                risk.get("kind") != "risk"
                or not ids
                or any(i not in utterances for i in ids)
            ):
                continue
            risks.append(
                {
                    "id": "risk:" + digest([m.client_id, risk["id"]]),
                    "meetingId": m.client_id,
                    "revision": m.revision,
                    "title": risk["title"],
                    "body": risk["body"],
                    "evidence": [{"id": i, "raw": utterances[i]["raw"]} for i in ids],
                    "inference": True,
                    "knowledge_sources": report.get("knowledge_sources", [])[:6],
                }
            )
    for source in records:
        evidence = source.get("evidence", {})
        if len(evidence.get("transcript", "")) > 1200:
            source["evidence"] = {
                **evidence,
                "transcript": evidence["transcript"][:1200],
                "truncated": True,
            }
    return {
        "records": records,
        "calendar": calendar,
        "risks": risks,
        "window": week_window(now),
        "coverage": [
            "仅覆盖言枢进展和已回读确认的日历写入；未读取手机完整日历、邮件或第三方任务库。",
            "匿名发言和文档候选不等于已确认责任；未知信息继续保留。",
        ],
    }


def selected(context, kind, trigger=None):
    if kind == "decision":
        risk = next((r for r in context["risks"] if r["id"] == trigger), None)
        return {
            "sources": [risk] if risk else [],
            "window": context["window"],
            "coverage": context["coverage"],
        }
    if kind == "proactive":
        p = next((r for r in context["records"] if r["id"] == trigger), None)
        return {
            "sources": [p] if p else [],
            "window": context["window"],
            "coverage": context["coverage"],
        }
    all_sources = context["records"] + context["calendar"]
    return {
        "sources": all_sources[:80],
        "window": context["window"],
        "coverage": context["coverage"]
        + (["来源超过80项，本次仅覆盖前80项。"] if len(all_sources) > 80 else []),
    }


def phase(p, now):
    if (
        not p.get("confirmed")
        or p["status"] in {"verified", "completed", "withdrawn"}
        or not p.get("deadline")
    ):
        return None
    at = datetime.fromisoformat(p["deadline"].replace("Z", "+00:00"))
    if at < now:
        return "overdue"
    if at <= now + timedelta(hours=24):
        return "due"
    return None
