"""周期复盘和业务事件跟进：持久去重、源版本校验、建议归档。"""

import copy, uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func
from fastapi import HTTPException
from app.modules.followup.models import FollowupRun
from app.modules.followup.context import collect, selected, phase, SHANGHAI
from app.modules.meetings.services.decision_questionnaire import digest
from app.modules.knowledge.repositories.knowledge_repository import KnowledgeRepository
from app.modules.knowledge.services.knowledge_service import (
    KnowledgeService,
    empty_library,
)
from app.modules.knowledge.models.library import KnowledgeLibrary
from app.modules.knowledge.schemas.knowledge import LibrarySnapshot


def clock():
    return datetime.now(timezone.utc)


class FollowupService:
    def __init__(self, session):
        self.session = session

    async def enqueue(self, owner, kind, now=None, trigger=None, automatic=False):
        now = now or clock()
        # 每账号统一入队锁，避免多个进程或相同事件创建重复任务。
        await KnowledgeRepository(self.session).library(owner, lock=True)
        context = await collect(self.session, owner, now)
        if kind in {"decision", "proactive"} and not trigger:
            candidates = (
                context["risks"]
                if kind == "decision"
                else [r for r in context["records"] if phase(r, now)]
            )
            existing_rows = {
                r.id: r
                for r in await self.session.scalars(
                    select(FollowupRun).where(
                        FollowupRun.owner_id == owner, FollowupRun.kind == kind
                    )
                )
            }
            for candidate in candidates:
                candidate_key = digest(
                    [
                        owner,
                        kind,
                        [
                            candidate["id"],
                            digest([candidate]),
                            phase(candidate, now) if kind == "proactive" else None,
                        ],
                    ]
                )
                if candidate_key not in existing_rows:
                    trigger = candidate["id"]
                    break
        if kind == "decision":
            trigger = trigger or next((r["id"] for r in context["risks"]), None)
        if kind == "proactive":
            trigger = trigger or next(
                (r["id"] for r in context["records"] if phase(r, now)), None
            )
        data = selected(context, kind, trigger)
        if not data["sources"]:
            return None
        current_phase = phase(data["sources"][0], now) if kind == "proactive" else None
        if kind == "proactive" and not current_phase:
            return None
        source_hash = digest(data if kind == "weekly" else data["sources"])
        key = digest(
            [
                owner,
                kind,
                (
                    context["window"]["plan_start"]
                    if kind == "weekly"
                    else [trigger, source_hash, current_phase]
                ),
            ]
        )
        existing = await self.session.get(FollowupRun, key)
        if existing:
            return existing
        if kind != "weekly":
            day = now.astimezone(SHANGHAI).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            count = await self.session.scalar(
                select(func.count())
                .select_from(FollowupRun)
                .where(
                    FollowupRun.owner_id == owner,
                    FollowupRun.kind != "weekly",
                    FollowupRun.created_at >= day,
                )
            )
            same_kind = await self.session.scalar(
                select(func.count())
                .select_from(FollowupRun)
                .where(
                    FollowupRun.owner_id == owner,
                    FollowupRun.kind == kind,
                    FollowupRun.created_at >= day,
                )
            )
            if count >= 2 or same_kind >= 1:
                return None
        row = FollowupRun(
            id=key,
            owner_id=owner,
            kind=kind,
            status="queued",
            attempts=0,
            result={},
            input={
                "data": data,
                "source_hash": source_hash,
                "trigger": trigger,
                "phase": current_phase,
                "clock": now.isoformat(),
                "automatic": automatic,
            },
            created_at=now,
            updated_at=now,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def current(self, row, now=None, lock=False):
        # 原时间窗口保留用于历史快照；期限阶段仍以当前时间复核。
        context = await collect(
            self.session,
            row.owner_id,
            datetime.fromisoformat(row.input["clock"]),
            lock=lock,
        )
        data = selected(context, row.kind, row.input.get("trigger"))
        if (
            digest(data if row.kind == "weekly" else data["sources"])
            != row.input["source_hash"]
        ):
            return False
        if (
            row.kind == "proactive"
            and phase(data["sources"][0], now or clock()) != row.input["phase"]
        ):
            return False
        return bool(data["sources"])

    async def notices(self, owner):
        rows = await self.session.scalars(
            select(FollowupRun)
            .where(FollowupRun.owner_id == owner)
            .order_by(FollowupRun.created_at.desc())
            .limit(30)
        )
        result = []
        for row in rows:
            if row.status not in {"succeeded", "failed"} or not await self.current(row):
                continue
            if row.kind == "decision" and not row.result.get("needed", False):
                continue
            title = {
                "weekly": "周复盘已整理",
                "decision": "这项决策有备选建议",
                "proactive": "有一项履约需要跟进",
            }[row.kind]
            instruction = "归档到知识库供查看，不执行建议。"
            if row.kind == "proactive":
                p = row.input["data"]["sources"][0]
                instruction = (
                    p["title"]
                    + "："
                    + (
                        "交期已过，尚未核验完成。"
                        if row.input["phase"] == "overdue"
                        else "交期临近，尚未核验完成。"
                    )
                )
            if row.kind == "weekly":
                instruction = "本周复盘已归档，规划建议仍待确认。"
            if row.status == "failed":
                instruction = "生成未完成，可以重试。"
            result.append(
                {
                    "id": "followup:" + row.id,
                    "kind": "followup",
                    "title": title,
                    "instruction": instruction,
                    "state": (
                        "allowed"
                        if row.decision == "acknowledged"
                        else (
                            "declined"
                            if row.decision == "declined"
                            else (
                                "unavailable"
                                if row.attempts >= 3 and row.status == "failed"
                                else "pending"
                            )
                        )
                    ),
                    "allowLabel": (
                        "重试"
                        if row.status == "failed"
                        else "知道了" if row.document_id else "归档"
                    ),
                    "fingerprint": digest(
                        [row.id, row.input["source_hash"], row.attempts]
                    ),
                }
            )
        return result

    async def decide(self, owner, notice_id, fingerprint, allow):
        await KnowledgeRepository(self.session).library(owner, lock=True)
        row = await self.session.scalar(
            select(FollowupRun)
            .where(
                FollowupRun.id == notice_id.removeprefix("followup:"),
                FollowupRun.owner_id == owner,
            )
            .with_for_update()
        )
        if (
            row is None
            or digest([row.id, row.input["source_hash"], row.attempts]) != fingerprint
            or not await self.current(row, lock=True)
        ):
            raise HTTPException(409, "assistance_source_changed")
        if row.decision:
            if (row.decision == "declined") == allow:
                raise HTTPException(409, "assistance_decision_conflict")
            return {"status": row.decision, "documentId": row.document_id}
        if row.status == "failed":
            if allow and row.attempts >= 3:
                raise HTTPException(409, "assistance_not_ready")
            if allow:
                row.status = "queued"
            else:
                row.decision = "declined"
            await self.session.commit()
            return {"status": row.status}
        if row.status != "succeeded":
            raise HTTPException(409, "assistance_not_ready")
        if allow:
            if not row.document_id:
                await self.archive(row)
            row.decision = "acknowledged"
        else:
            row.decision = "declined"
        await self.session.commit()
        return {
            "status": "recorded" if allow else "declined",
            "documentId": row.document_id,
        }

    async def archive(self, row):
        library = await KnowledgeRepository(self.session).library(
            row.owner_id, lock=True
        )
        snapshot = copy.deepcopy(library.snapshot) if library else empty_library()
        doc_id = "assistant-" + row.id
        if any(d["id"] == doc_id for d in snapshot["documents"]):
            row.document_id = doc_id
            return
        folder = "ai-reviews"
        project = "personal-assistance"
        if not any(f["id"] == folder for f in snapshot["folders"]):
            snapshot["folders"].append(
                {"id": folder, "name": "AI复盘与建议", "parentId": None}
            )
        if not any(p["id"] == project for p in snapshot["projects"]):
            snapshot["projects"].append(
                {"id": project, "name": "AI复盘与建议", "allowed": False}
            )
        text = render(row)
        stamp = clock().isoformat()
        version = "generated-v1"
        title = {
            "weekly": "周复盘与本周规划",
            "decision": "决策备选建议",
            "proactive": "履约跟进建议",
        }[row.kind]
        links = {}
        for source in row.input["data"]["sources"]:
            evidence = source.get("evidence", {})
            citations = source.get("knowledge_sources", [])
            if isinstance(evidence, dict) and evidence.get("kind") == "document":
                citations = [*citations, evidence]
            for hit in citations:
                if all(hit.get(k) for k in ["documentId", "versionId", "hash"]):
                    link = {
                        "kind": "document",
                        "targetId": hit["documentId"],
                        "versionId": hit["versionId"],
                        "hash": hit["hash"],
                    }
                    links[digest(link)] = link
        snapshot["documents"].append(
            {
                "id": doc_id,
                "folderId": folder,
                "projectId": project,
                "kind": "markdown",
                "source": "assistant:" + row.id,
                "createdAt": stamp,
                "selectedVersionId": version,
                "allowed": False,
                "archived": True,
                "demo": False,
                "ownerUserId": row.owner_id,
                "tags": ["AI建议", "待核对"],
                "versions": [
                    {
                        "id": version,
                        "number": 1,
                        "title": title,
                        "content": text,
                        "hash": digest_text(text),
                        "createdAt": stamp,
                        "state": "review",
                        "reason": "AI建议，不代表正式批准或已执行",
                        "origin": "assistant:" + row.id,
                        "sources": list(links.values())[:100],
                    }
                ],
            }
        )
        canonical = LibrarySnapshot.model_validate(snapshot).model_dump(
            mode="json", by_alias=True
        )
        if not library:
            library = KnowledgeLibrary(
                owner_id=row.owner_id, revision=0, snapshot=canonical
            )
            self.session.add(library)
        library.snapshot = canonical
        library.revision += 1
        await KnowledgeService(self.session)._index(row.owner_id, canonical)
        row.document_id = doc_id


def digest_text(text):
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


def render(row):
    r = row.result
    data = row.input["data"]
    lines = [
        "# AI复盘与建议",
        "",
        "> 这是一份生成时刻的建议快照，不代表批准、履约完成或已执行；正式状态以进展和核验记录为准。",
        "",
        "生成依据时间：" + row.input["clock"],
        "",
        "## 时间窗口",
        json_window(data["window"]),
    ]
    lines += ["## 运行来源", str(r.get("skill", {})), str(r.get("runtime", {}))]
    if row.kind == "decision":
        lines += ["## 问题", r["problem"], "## 三种备选方案"]
        for option in r["options"]:
            lines += [
                f"### {option['code']} · {option['title']}",
                "优点：" + option["pros"],
                "代价与限制：" + option["cons"],
            ]
        lines += [
            "## 一项建议",
            r["recommendation"] + "：" + r["reason"],
            "## 建议完成条件",
            *["- " + s for s in r["done"]],
            "## 建议步骤",
            *["- " + s for s in r["plan"]],
        ]
    else:
        headings = {
            "wins": "已核验完成",
            "risks": "逾期或风险",
            "waiting": "等待与跟进",
            "stalled": "停滞或待明确",
            "plan": "规划建议",
            "proposals": "待确认的更新建议",
        }
        for section, title in headings.items():
            lines += [
                "## " + title,
                *[
                    f"- {f['text']}（依据：{', '.join(f['source_ids'])}）"
                    for f in r["findings"]
                    if f["section"] == section
                ],
            ]
        lines += ["## 信息缺口", *["- " + g for g in r.get("gaps", [])]]
    lines += ["## 覆盖范围", *["- " + g for g in data["coverage"]], "## 来源快照"]
    for source in data["sources"]:
        lines += [
            "### " + source.get("title", "来源"),
            "来源 ID：" + source["id"],
            "版本：" + str(source.get("version", source.get("revision", ""))),
        ]
        if source.get("deadline"):
            lines += ["交期：" + source["deadline"]]
        if source.get("evidence"):
            lines += ["原文：" + str(source["evidence"])]
        if source.get("knowledge_sources"):
            lines += ["历史资料依据：" + str(source["knowledge_sources"])]
        lines += ["状态：" + str(source.get("status", "AI风险候选")), ""]
    return "\n".join(lines)


def json_window(window):
    return "\n".join(f"- {k}: {v}" for k, v in window.items())
