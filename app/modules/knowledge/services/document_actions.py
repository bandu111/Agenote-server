"""文档行动候选的持久队列、版本核对和经用户确认的进展记录。"""

import asyncio
import copy
import uuid
import logging
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from sqlalchemy import select, delete, update
from app.modules.knowledge.models.assistance import DocumentAnalysis, AssistanceReceipt
from app.modules.knowledge.repositories.knowledge_repository import KnowledgeRepository
from app.modules.knowledge.services.knowledge_service import effective_documents
from app.integrations.llama_index.knowledge_index import fingerprint
from app.modules.knowledge.services.document_agent import DocumentActionAgent
from app.modules.meetings.services.decision_questionnaire import (
    DecisionQuestionnaire,
    digest,
)
from app.modules.meetings.models.meeting import Meeting
from app.modules.progress.models.progress_item import ProgressItem
from app.modules.progress.services.progress_service import output as progress_output
from app.modules.progress.repositories.progress_repository import ProgressRepository

logger = logging.getLogger(__name__)


async def enqueue_documents(session, owner, snapshot):
    """只入队知识库的当前有效版本，和资料同步处于同一事务。"""
    valid = effective_documents(snapshot)
    rows = list(
        await session.scalars(
            select(DocumentAnalysis).where(DocumentAnalysis.owner_id == owner)
        )
    )
    by_id = {r.id: r for r in rows}
    current = set()
    for (doc_id, version_id), (doc, version) in valid.items():
        # 会议本身已有承诺提取，自动纪要不能再反向产生重复承诺。
        if doc["source"].startswith(("meeting:", "assistant:")):
            continue
        source_hash = fingerprint(doc, version)
        key = digest([owner, doc_id, version_id, source_hash])
        current.add(key)
        if key not in by_id:
            session.add(
                DocumentAnalysis(
                    id=key,
                    owner_id=owner,
                    document_id=doc_id,
                    version_id=version_id,
                    source_hash=source_hash,
                    status="queued",
                    result={},
                    attempts=0,
                )
            )
        elif by_id[key].status == "succeeded" and by_id[key].result.get("policyVersion") != 2:
            by_id[key].status = "queued"
            by_id[key].result = {}
            by_id[key].attempts = 0
        elif by_id[key].status == "stale":
            by_id[key].status = (
                "succeeded" if by_id[key].result.get("skill") else "queued"
            )
    document_ids = {d["id"] for d in snapshot["documents"]}
    for row in rows:
        if row.document_id not in document_ids:
            await session.delete(row)
        elif row.id not in current:
            row.status = "stale"


async def authorized_job(session, owner, job):
    library = await KnowledgeRepository(session).library(owner)
    if not library or not job or job.owner_id != owner:
        return None
    pair = effective_documents(library.snapshot).get((job.document_id, job.version_id))
    return pair if pair and fingerprint(*pair) == job.source_hash else None


class DocumentJobs:
    """单个受限后台消费者；队列落库，崩溃任务超出租期后可恢复。"""

    def __init__(self, database, provider):
        self.database, self.provider = database, provider
        self.task = None

    def start(self):
        self.task = asyncio.create_task(self.loop())

    async def loop(self):
        try:
            await self.backfill()
        except Exception as error:
            logger.warning(
                "document_backfill_unavailable",
                extra={"error_type": type(error).__name__},
            )
        while True:
            try:
                worked = await self.process_one()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "document_queue_unavailable",
                    extra={"error_type": type(error).__name__},
                )
                worked = False
            if not worked:
                await asyncio.sleep(3)

    async def backfill(self):
        """为升级前已有的有效资料补建队列；逐账号加锁，不能重复入队。"""
        from app.modules.knowledge.models.library import KnowledgeLibrary

        async with self.database.sessions() as session:
            owners = list(await session.scalars(select(KnowledgeLibrary.owner_id)))
        for owner in owners:
            async with self.database.sessions() as session:
                library = await KnowledgeRepository(session).library(owner, lock=True)
                if library:
                    # 文档引擎升级后重建派生索引，不更改用户原文或版本。
                    from app.modules.knowledge.services.knowledge_service import KnowledgeService
                    await KnowledgeService(session)._index(owner, library.snapshot)
                    await enqueue_documents(session, owner, library.snapshot)
                await session.commit()

    async def process_one(self):
        async with self.database.sessions() as session:
            # 工作最多运行 10 分钟，15 分钟租期用于异常退出恢复。
            await session.execute(
                update(DocumentAnalysis)
                .where(
                    DocumentAnalysis.status == "running",
                    DocumentAnalysis.updated_at
                    < datetime.now(timezone.utc) - timedelta(minutes=15),
                )
                .values(status="queued")
            )
            row = await session.scalar(
                select(DocumentAnalysis)
                .where(DocumentAnalysis.status == "queued")
                .order_by(DocumentAnalysis.updated_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not row:
                await session.commit()
                return False
            job_id, owner = row.id, row.owner_id
            pair = await authorized_job(session, owner, row)
            if not pair:
                row.status = "stale"
                await session.commit()
                return True
            nodes = [
                n
                for n in await KnowledgeRepository(session).nodes(owner)
                if n.document_id == row.document_id
                and n.version_id == row.version_id
                and n.source_hash == row.source_hash
            ]
            if not nodes:
                from app.modules.knowledge.models.library import KnowledgeIndexState
                waiting = await session.scalar(select(KnowledgeIndexState).where(KnowledgeIndexState.owner_id == owner,
                    KnowledgeIndexState.document_id == row.document_id, KnowledgeIndexState.version_id == row.version_id,
                    KnowledgeIndexState.status == 'queued'))
                if waiting:
                    await session.commit()
                    return False
                row.status = "failed"
                row.result = {"error": "document_text_unavailable"}
                await session.commit()
                return True
            row.status = "running"
            row.attempts += 1
            row.updated_at = datetime.now(timezone.utc)
            await session.commit()
        try:
            result = await asyncio.wait_for(
                DocumentActionAgent(self.provider).analyze(nodes, title=pair[1]["title"]), timeout=600
            )
            # 未采用的资料候选不生成全局补全；用户采用后由进展统一处理真实缺项。
            result["questionnaire"] = {"questions": [], "applied": False}
            status = "succeeded"
        except asyncio.CancelledError:
            async with self.database.sessions() as session:
                await session.execute(
                    update(DocumentAnalysis)
                    .where(
                        DocumentAnalysis.id == job_id,
                        DocumentAnalysis.status == "running",
                    )
                    .values(status="queued")
                )
                await session.commit()
            raise
        except Exception:
            result, status = {"error": "document_analysis_unavailable"}, "failed"
        async with self.database.sessions() as session:
            # 先核对资料有效性，再落结果；运行期间撤权或换版不会产生业务结果。
            library = await KnowledgeRepository(session).library(owner, lock=True)
            row = await session.scalar(
                select(DocumentAnalysis)
                .where(DocumentAnalysis.id == job_id)
                .with_for_update()
            )
            if row is None:
                return True
            pair = await authorized_job(session, owner, row)
            if not pair:
                row.status = "stale"
            else:
                doc, version = pair
                result["documentTitle"] = version["title"]
                result["createdAt"] = version["createdAt"]
                result["projectId"] = doc["projectId"]
                result["versionHash"] = version["hash"]
                row.status, row.result = status, result
            await session.commit()
        return True

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class KnowledgeAssistance:
    def __init__(self, session):
        self.session = session

    async def notices(self, owner):
        from app.modules.knowledge.services.notice_inbox import cleared_ids
        cleared = await cleared_ids(self.session, owner)
        receipts = {
            r.id: r
            for r in await self.session.scalars(
                select(AssistanceReceipt).where(AssistanceReceipt.owner_id == owner)
            )
        }
        notices = []
        # 会议问题和文档问题共用通知入口；过时分析不再作为当前待补事项。
        meetings = list(
            await self.session.scalars(
                select(Meeting)
                .where(Meeting.owner_id == owner, Meeting.deleted.is_(False))
                .order_by(Meeting.updated_at.desc())
                .limit(40)
            )
        )
        library = await KnowledgeRepository(self.session).library(owner)
        for m in meetings:
            report = m.analysis or {}
            if m.revision != m.analysis_revision:
                continue
            if report.get("knowledge_sources") and report.get("knowledge_revision") != (
                library.revision if library else 0
            ):
                continue
            # 风险沿用来源版本和已读回执，只有真实警告进入主动提醒通路。
            sources = {u['id']: u for u in m.content.get('utterances', [])}
            groups = {}
            for insight in report.get('insights', []):
                ids = sorted(set(insight.get('evidence_ids', [])))
                if (insight.get('kind') != 'risk' or not ids or
                        any(i not in sources or sources[i].get('withdrawn') for i in ids)):
                    continue
                groups.setdefault(tuple(ids), []).append(insight)
            for ids, risks in groups.items():
                notice_id = 'meeting-warning:' + m.client_id + ':' + digest([[i, sources[i].get('raw'), sources[i].get('correction')] for i in ids])
                aliases = ['assistance:meeting-warning:' + m.client_id + ':' + i['id'] for i in risks]
                if 'assistance:' + notice_id in cleared or any(a in cleared for a in aliases):
                    continue
                notices.append({
                    'id': notice_id, 'kind': 'warning', 'meetingId': m.client_id,
                    'title': risks[0]['title'],
                    'instruction': '；'.join(dict.fromkeys(i['body'] for i in risks[:2])),
                    'allowLabel': '知道了', 'state': 'pending',
                    # 来源没有改变时，模型重跑或换措辞不能再次推送同一风险。
                    'fingerprint': digest([m.client_id,
                        [[i, sources[i].get('raw'), sources[i].get('correction')] for i in ids]]),
                })
            for q in report.get("questionnaire", {}).get("questions", []):
                kind = "goal" if q["field"] == "acceptance" else "commitment"
                progress = await ProgressRepository(self.session).source(
                    owner, f'{kind}:{m.id}:{q["origin_id"]}'
                )
                if progress:
                    current = progress_output(progress)
                    known = current.get(q["field"])
                    if (q["field"] == "side" and known in {"ours", "theirs"}) or (
                        q["field"] in {"deadline", "acceptance"}
                        and known
                        and known != "待明确"
                    ):
                        continue
                n = {
                    "id": "meeting-question:" + m.client_id + ":" + q["id"],
                    "kind": "question",
                    "meetingId": m.client_id,
                    "title": "待补充：" + q["subject"],
                    "instruction": q["question"],
                    "allowLabel": "知道了",
                    "state": "pending",
                }
                n["fingerprint"] = digest([m.client_id, q])
                notices.append(n)
        from app.modules.followup.service import FollowupService

        notices.extend(await FollowupService(self.session).notices(owner))
        notices = [n for n in notices if n.get("kind") == "warning" and "assistance:" + n["id"] not in cleared]
        for n in notices:
            receipt = receipts.get(digest([owner, n["id"], n["fingerprint"]]))
            if receipt:
                n["state"] = {
                    "recorded": "allowed",
                    "acknowledged": "allowed",
                    "declined": "declined",
                }[receipt.decision]
        return notices

    async def document_actions(self, owner, document_id, version_id):
        library = await KnowledgeRepository(self.session).library(owner)
        pair = effective_documents(library.snapshot).get((document_id, version_id)) if library else None
        if not pair:
            raise HTTPException(404, "document_action_source_unavailable")
        rows = list(await self.session.scalars(select(DocumentAnalysis).where(
            DocumentAnalysis.owner_id == owner,
            DocumentAnalysis.document_id == document_id,
            DocumentAnalysis.version_id == version_id).order_by(DocumentAnalysis.updated_at.desc())))
        job = next((j for j in rows if j.source_hash == fingerprint(*pair)), None)
        if not job:
            return {"documentId": document_id, "versionId": version_id, "status": "unavailable", "actions": []}
        result = {"documentId": document_id, "versionId": version_id, "status": job.status,
                  "purpose": job.result.get("purpose", "unknown"), "actions": []}
        if job.status != "succeeded":
            return result
        if job.result.get("policyVersion") != 2:
            result["status"] = "queued"
            return result
        if job.result.get("purpose") not in {"actionable", "mixed"}:
            return result
        for action in job.result.get("actions", []):
            if action.get("evidenceRole") != "actual":
                continue
            notice_id = "document-action:" + job.id + ":" + action["id"]
            source = digest([job.id, action])
            receipt = await self.session.get(AssistanceReceipt, digest([owner, notice_id, source]))
            result["actions"].append({
                "id": notice_id, "fingerprint": source, "kind": "document_action",
                "jobId": job.id, "candidateId": action["id"],
                "title": action["title"], "quote": action["quote"],
                "modality": action["modality"], "missing": action.get("missing", []),
                "state": "allowed" if receipt and receipt.decision == "recorded" else "declined" if receipt else "pending",
                "citation": {"documentId": document_id, "versionId": version_id,
                             "nodeId": action["nodeId"], "hash": pair[1]["hash"]},
            })
        return result

    async def decide(self, owner, notice_id, expected, allow):
        if notice_id.startswith("followup:"):
            from app.modules.followup.service import FollowupService

            return await FollowupService(self.session).decide(
                owner, notice_id, expected, allow
            )
        # 固定锁顺序：资料库 → 通知回执，避免换版与批准交错。
        await KnowledgeRepository(self.session).library(owner, lock=True)
        if notice_id.startswith("document-action:"):
            parts = notice_id.split(":")
            job = await self.session.get(DocumentAnalysis, parts[1] if len(parts) == 3 else "")
            if not job or job.owner_id != owner:
                raise HTTPException(404, "document_action_source_unavailable")
            candidates = (await self.document_actions(owner, job.document_id, job.version_id))["actions"]
        else:
            candidates = await self.notices(owner)
        n = next((n for n in candidates if n["id"] == notice_id), None)
        if n is None or n["fingerprint"] != expected:
            raise HTTPException(409, "assistance_source_changed")
        if n["state"] == "preparing":
            raise HTTPException(409, "assistance_not_ready")
        if n["kind"] == "retry":
            row = await self.session.get(
                DocumentAnalysis, n["jobId"], with_for_update=True
            )
            if not allow:
                row.status = "declined"
            elif row.status == "failed" and row.attempts < 3:
                row.status = "queued"
            else:
                raise HTTPException(409, "assistance_not_ready")
            await self.session.commit()
            return {"status": row.status}
        key = digest([owner, notice_id, expected])
        previous = await self.session.get(AssistanceReceipt, key)
        if previous:
            if (previous.decision == "declined") == allow:
                raise HTTPException(409, "assistance_decision_conflict")
            return previous.result
        result = {"status": "declined"}
        decision = "declined"
        if allow and n["kind"] in {"question", "warning"}:
            # 知悉不等于补全，更不能把问题当作已经确认的事实。
            result, decision = {"status": "acknowledged"}, "acknowledged"
        elif allow:
            job = await self.session.get(DocumentAnalysis, n["jobId"])
            a = next(a for a in job.result["actions"] if a["id"] == n["candidateId"])
            progress_id = str(uuid.uuid4())
            content = {
                "title": a["title"],
                "person": "待确认",
                "side": a["side"],
                "status": "pending",
                "candidateStatus": a["modality"],
                "authorizationStatus": "pending",
                "createdAt": job.result["createdAt"],
                "deadline": a["deadline"],
                "deadlineText": a["deadlineText"],
                "summary": "来自资料原文的候选；记录不代表已批准或验收",
                "goalId": "",
                "progress": 0,
                "missing": a["missing"],
                "projectId": job.result["projectId"],
                "attachment": None,
                "evidence": {
                    "kind": "document",
                    "id": a["nodeId"],
                    "sessionId": "",
                    "transcript": a["quote"],
                    "start": 0,
                    "end": 0,
                    "recordedAt": job.result["createdAt"],
                    "timeZone": "Asia/Shanghai",
                    "speakerConfirmed": False,
                    "documentId": job.document_id,
                    "versionId": job.version_id,
                    "hash": job.result["versionHash"],
                    "nodeId": a["nodeId"],
                },
            }
            self.session.add(
                ProgressItem(
                    id=progress_id,
                    owner_id=owner,
                    meeting_id=None,
                    source_key="document:" + digest([job.id, a["id"]]),
                    kind="commitment",
                    source_revision=1,
                    version=1,
                    content=content,
                    overrides={},
                    history=[],
                )
            )
            result, decision = {
                "status": "recorded",
                "progressId": progress_id,
            }, "recorded"
        self.session.add(
            AssistanceReceipt(id=key, owner_id=owner, decision=decision, result=result)
        )
        await self.session.commit()
        return result
