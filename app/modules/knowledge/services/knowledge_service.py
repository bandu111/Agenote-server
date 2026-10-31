"""账号内资料入库即可检索；引用仍核对有效版本与原文完整性。"""

import asyncio
import base64
import copy
import hashlib
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import delete, select
from llama_index.core.schema import TextNode
from app.modules.knowledge.models.library import (
    KnowledgeLibrary,
    KnowledgeNode,
    KnowledgeIndexState,
    DeletedMeetingKnowledge,
)
from app.modules.knowledge.repositories.knowledge_repository import KnowledgeRepository
from app.modules.knowledge.schemas.knowledge import LibrarySnapshot
from app.integrations.llama_index.knowledge_index import (
    parse_nodes,
    fingerprint,
    retrieve_nodes,
)


def empty_library():
    return {
        "schema": 1,
        "folders": [{"id": "inbox", "name": "收件箱", "parentId": None}],
        "projects": [
            {"id": "personal", "name": "个人资料", "allowed": True},
            {"id": "personal-meetings", "name": "个人会议", "allowed": True},
        ],
        "documents": [],
        "audit": [],
    }


def retrieval_snapshot(snapshot):
    """兼容旧客户端字段；历史关闭标记不再控制资料检索。"""
    result = copy.deepcopy(snapshot)
    for item in [*result["projects"], *result["documents"]]:
        item["allowed"] = True
    return result


def effective_documents(snapshot, project=None):
    projects = {p["id"] for p in snapshot["projects"]}
    now = datetime.now(timezone.utc)
    result = {}
    for d in snapshot["documents"]:
        if (
            d["projectId"] not in projects
            or (project is not None and d["projectId"] != project)
        ):
            continue
        if any(v["state"] == "conflicted" for v in d["versions"]):
            continue
        versions = [v for v in d["versions"] if v["state"] == "current"]
        if len(versions) != 1:
            continue
        v = versions[0]
        if (
            v.get("validUntil")
            and datetime.fromisoformat(v["validUntil"].replace("Z", "+00:00")) <= now
        ):
            continue
        if hashlib.sha256(v["content"].encode()).hexdigest() != v["hash"]:
            continue
        result[(d["id"], v["id"])] = (d, v)
    return result


class KnowledgeService:
    def __init__(self, session):
        self.session = session
        self.repository = KnowledgeRepository(session)

    async def read(self, owner):
        row = await self.repository.library(owner, lock=True)
        if row:
            from app.modules.meetings.models.meeting import Meeting
            meetings = await self.session.execute(select(Meeting.id, Meeting.client_id).where(
                Meeting.owner_id == owner, Meeting.deleted.is_(False)))
            generated = {doc_id: 'meeting:' + client_id
                for meeting_id, client_id in meetings
                for doc_id in ('meeting-' + client_id, 'audio-' + meeting_id)}
            snapshot = copy.deepcopy(row.snapshot)
            changed = False
            for doc in snapshot['documents']:
                if (doc.get('folderId') == 'inbox' and doc.get('archived') is True
                        and doc.get('kind') in {'audio', 'markdown'}
                        and doc['id'] in generated and doc.get('source') == generated[doc['id']]):
                    doc['folderId'] = 'meeting-records'
                    changed = True
            if changed:
                if not any(f['id'] == 'meeting-records' for f in snapshot['folders']):
                    snapshot['folders'].append({'id':'meeting-records', 'name':'会议纪要', 'parentId':None})
                row.snapshot = snapshot
                row.revision += 1
                await self.session.commit()
        return {
            "revision": row.revision if row else 0,
            "snapshot": retrieval_snapshot(row.snapshot) if row else empty_library(),
            "indexStates": [
                {
                    "documentId": s.document_id,
                    "versionId": s.version_id,
                    "status": s.status,
                }
                for s in await self.repository.states(owner)
            ],
        }

    async def sync(self, owner, payload):
        row = await self.repository.library(owner, lock=True)
        if payload.revision != (row.revision if row else 0):
            raise HTTPException(409, "knowledge_revision_conflict")
        snapshot = retrieval_snapshot(payload.snapshot.model_dump(mode="json", by_alias=True))
        from app.modules.meetings.models.meeting import Meeting
        removed_meetings = await self.session.execute(select(Meeting.id, Meeting.client_id).where(
            Meeting.owner_id == owner, Meeting.deleted.is_(True)))
        removed_ids = {doc_id for meeting_id, client_id in removed_meetings for doc_id in ('meeting-' + client_id, 'audio-' + meeting_id)}
        snapshot['documents'] = [d for d in snapshot['documents'] if d['id'] not in removed_ids]
        old_docs = {d["id"]: d for d in row.snapshot["documents"]} if row else {}
        incoming_ids = {doc['id'] for doc in snapshot['documents']}
        removed_meetings = [doc_id for doc_id, doc in old_docs.items()
                            if doc_id not in incoming_ids and doc.get('source', '').startswith('meeting:')]
        for doc_id in removed_meetings:
            if await self.session.get(DeletedMeetingKnowledge, (owner, doc_id)) is None:
                self.session.add(DeletedMeetingKnowledge(owner_id=owner, document_id=doc_id))
        # Automatic clients must not recreate a deleted meeting via sync.
        explicitly_added = incoming_ids - set(old_docs)
        if explicitly_added:
            deleted = await self.session.scalars(select(DeletedMeetingKnowledge).where(
                DeletedMeetingKnowledge.owner_id == owner,
                DeletedMeetingKnowledge.document_id.in_(explicitly_added)))
            if deleted.first():
                raise HTTPException(409, 'knowledge_immutable_source')
        for doc in snapshot["documents"]:
            if doc.get("ownerUserId") not in {None, owner}:
                raise HTTPException(403, "knowledge_access_denied")
            doc["ownerUserId"] = owner
            previous = old_docs.get(doc["id"])
            if doc.get('audioRefs') != (previous.get('audioRefs', []) if previous else []):
                raise HTTPException(409, 'knowledge_immutable_audio')
            old_versions = (
                {v["id"]: v for v in previous["versions"]} if previous else {}
            )
            if previous and (
                previous.get("fileHash") != doc.get("fileHash")
                or previous.get("fileData") != doc.get("fileData")
            ):
                raise HTTPException(409, "knowledge_immutable_source")
            for v in doc["versions"]:
                if hashlib.sha256(v["content"].encode()).hexdigest() != v["hash"]:
                    raise HTTPException(422, "knowledge_integrity_failed")
                original = old_versions.get(v["id"])
                if original and any(
                    original[k] != v[k]
                    for k in [
                        "number",
                        "title",
                        "content",
                        "hash",
                        "createdAt",
                        "origin",
                    ]
                ):
                    raise HTTPException(409, "knowledge_immutable_source")
            if previous and not set(old_versions).issubset(
                {v["id"] for v in doc["versions"]}
            ):
                raise HTTPException(409, "knowledge_immutable_source")
            if doc.get("fileData"):
                try:
                    binary = base64.b64decode(doc["fileData"], validate=True)
                except ValueError as error:
                    raise HTTPException(422, "knowledge_integrity_failed") from error
                if len(binary) > 20 * 1024 * 1024 or hashlib.sha256(
                    binary
                ).hexdigest() != doc.get("fileHash"):
                    raise HTTPException(422, "knowledge_integrity_failed")
        if row is None:
            row = KnowledgeLibrary(owner_id=owner, revision=0, snapshot=snapshot)
            self.session.add(row)
        row.revision += 1
        row.snapshot = snapshot
        await self._index(owner, snapshot)
        from app.modules.knowledge.services.document_actions import enqueue_documents

        await enqueue_documents(self.session, owner, snapshot)
        await self.session.commit()
        from app.integrations.qmd.client import sync as sync_qmd

        allowed = effective_documents(snapshot)
        await sync_qmd(
            owner,
            row.revision,
            [
                n
                for n in await self.repository.nodes(owner)
                if (n.document_id, n.version_id) in allowed
                and n.source_hash
                == fingerprint(*allowed[(n.document_id, n.version_id)])
            ],
        )
        return await self.read(owner)

    async def _index(self, owner, snapshot):
        ids = {d["id"] for d in snapshot["documents"]}
        await self.repository.purge(owner, ids)
        states = {s.id: s for s in await self.repository.states(owner)}
        for doc in snapshot["documents"]:
            for version in doc["versions"]:
                key = hashlib.sha256(
                    f"{owner}:{doc['id']}:{version['id']}".encode()
                ).hexdigest()
                source_hash = fingerprint(doc, version) or version["hash"]
                if key in states and states[key].source_hash == source_hash and states[key].status != 'parse_unavailable':
                    continue
                await self.session.execute(delete(KnowledgeNode).where(
                    KnowledgeNode.owner_id == owner, KnowledgeNode.document_id == doc['id'], KnowledgeNode.version_id == version['id']))
                try:
                    if doc.get('fileData') and not version['content'].strip():
                        # Office 转换和 OCR 不占据移动端同步请求；队列状态持久化后立即返回。
                        nodes, status = [], 'queued'
                    else:
                        nodes = await asyncio.to_thread(parse_nodes, owner, doc, version)
                        status = "indexed" if nodes else "empty"
                except Exception:
                    # 保存原文件与解析状态，不把无法解析的文件伪装成已可检索。
                    nodes, status = [], "parse_unavailable"
                for node in nodes:
                    self.session.add(
                        KnowledgeNode(
                            id=node.node_id,
                            owner_id=owner,
                            document_id=doc["id"],
                            version_id=version["id"],
                            source_hash=source_hash,
                            text=node.get_content(),
                            node=node.to_dict(),
                        )
                    )
                if key in states:
                    states[key].status = status
                    states[key].source_hash = source_hash
                else:
                    self.session.add(
                        KnowledgeIndexState(
                            id=key,
                            owner_id=owner,
                            document_id=doc["id"],
                            version_id=version["id"],
                            source_hash=source_hash,
                            status=status,
                        )
                    )

    async def search(self, owner, query, project=None, limit=6):
        row = await self.repository.library(owner)
        if row is None:
            return []
        allowed = effective_documents(row.snapshot, project)
        # 先按账号、项目和有效版本过滤，再让检索器看到内容。
        rows = [
            n
            for n in await self.repository.nodes(owner)
            if (n.document_id, n.version_id) in allowed
            and n.source_hash == fingerprint(*allowed[(n.document_id, n.version_id)])
        ]
        from app.integrations.qmd.client import retrieve as qmd_retrieve

        revision = row.revision
        qmd_hits = await qmd_retrieve(owner, revision, rows, query, limit, project)
        if qmd_hits is None:
            scored = await asyncio.to_thread(
                retrieve_nodes, [TextNode.from_dict(n.node) for n in rows], query, limit
            )
            ranked = [(score.node, score.score, "llamaindex-bm25") for score in scored]
        else:
            ranked = [
                (TextNode.from_dict(n.node), score, mode) for n, score, mode in qmd_hits
            ]
        # 等待检索期间可能撤权或替换版本；回读快照后才返回证据。
        await self.session.refresh(row)
        if row.revision != revision:
            return []
        from app.modules.search.relevance import relevance, METHOD
        hits = []
        for node, score, retriever in ranked:
            doc, version = allowed[
                (node.metadata["document_id"], node.metadata["version_id"])
            ]
            hits.append(
                {
                    "nodeId": node.node_id,
                    "documentId": doc["id"],
                    "versionId": version["id"],
                    "hash": version["hash"],
                    "fileHash": doc.get("fileHash"),
                    "title": version["title"],
                    "projectId": doc["projectId"],
                    "text": node.get_content(),
                    "page": node.metadata.get("page"),
                    "source": doc["source"],
                    "score": float(score),
                    "matchPercent": relevance(query, version["title"] + "\n" + node.get_content()),
                    "matchMethod": METHOD,
                    "extraction": node.metadata.get("extraction", "text"),
                    "requiresReview": node.metadata.get("extraction") == "ocr",
                    "retriever": retriever,
                    "sessionId": (
                        doc["source"].split(":", 1)[1]
                        if doc["source"].startswith("meeting:")
                        else None
                    ),
                }
            )
        return hits

    async def resolve(self, owner, citation):
        row = await self.repository.library(owner)
        if row is None:
            raise HTTPException(404, "knowledge_source_unavailable")
        allowed = effective_documents(row.snapshot)
        pair = allowed.get((citation.documentId, citation.versionId))
        if pair is None or pair[1]["hash"] != citation.hash:
            raise HTTPException(409, "knowledge_source_unavailable")
        node = next(
            (
                n
                for n in await self.repository.nodes(owner)
                if n.id == citation.nodeId
                and n.document_id == citation.documentId
                and n.version_id == citation.versionId
                and n.source_hash == fingerprint(*pair)
            ),
            None,
        )
        if node is None:
            raise HTTPException(404, "knowledge_source_unavailable")
        return {
            "text": node.text,
            "documentId": node.document_id,
            "versionId": node.version_id,
            "hash": citation.hash,
        }

    async def archive_meeting(self, meeting, archive):
        owner = meeting.owner_id
        row = await self.repository.library(owner, lock=True)
        snapshot = copy.deepcopy(row.snapshot) if row else empty_library()
        doc_id = "meeting-" + meeting.client_id
        if await self.session.get(DeletedMeetingKnowledge, (owner, doc_id)) is not None:
            return doc_id
        if not any(f["id"] == "meeting-records" for f in snapshot["folders"]):
            snapshot["folders"].append(
                {"id": "meeting-records", "name": "会议纪要", "parentId": None}
            )
        if not any(p["id"] == "personal-meetings" for p in snapshot["projects"]):
            snapshot["projects"].append(
                {"id": "personal-meetings", "name": "个人会议", "allowed": True}
            )
        document = next((d for d in snapshot["documents"] if d["id"] == doc_id), None)
        version_id = f"archive-{meeting.id}-r{archive.revision}"
        if archive.speaker_identity_revision:
            version_id += f"-i{archive.speaker_identity_revision}"
        if document and any(v["id"] == version_id for v in document["versions"]):
            return doc_id
        stamp = datetime.now(timezone.utc).isoformat()
        if document is None:
            document = {
                "id": doc_id,
                "folderId": "meeting-records",
                "projectId": "personal-meetings",
                "kind": "markdown",
                "source": "meeting:" + meeting.client_id,
                "createdAt": stamp,
                "versions": [],
                "selectedVersionId": version_id,
                "fileData": None,
                "fileHash": None,
                "filename": None,
                "url": None,
                "tags": ["会议纪要"],
                "allowed": True,
                "archived": True,
                "demo": False,
                "ownerUserId": owner,
            }
            snapshot["documents"].append(document)
        for v in document["versions"]:
            if v["state"] == "current":
                v["state"] = "stale"
                v["reason"] = "由新纪要版本替代"
        document["versions"].append(
            {
                "id": version_id,
                "number": max([v["number"] for v in document["versions"]], default=0)
                + 1,
                "title": archive.title,
                "content": archive.markdown,
                "hash": hashlib.sha256(archive.markdown.encode()).hexdigest(),
                "createdAt": stamp,
                "state": "current",
                "reason": "会后自动归档",
                "origin": "meeting:" + meeting.client_id,
                "sources": [],
                "validUntil": None,
            }
        )
        document["selectedVersionId"] = version_id
        snapshot = LibrarySnapshot.model_validate(snapshot).model_dump(
            mode="json", by_alias=True
        )
        if row is None:
            row = KnowledgeLibrary(owner_id=owner, revision=0, snapshot=snapshot)
            self.session.add(row)
        row.snapshot = snapshot
        row.revision += 1
        await self._index(owner, snapshot)
        return doc_id
