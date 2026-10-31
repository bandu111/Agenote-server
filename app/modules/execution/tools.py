"""认证工具注册表；副作用和回执原子提交，重试不重复创建任务或资料。"""
import base64
import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from sqlalchemy import select, text
from fastapi import HTTPException
from app.modules.execution.models import InternalTask, ToolReceipt
from app.modules.execution.schemas import ARGUMENTS
from app.modules.meetings.models.meeting import Meeting
from app.modules.knowledge.repositories.knowledge_repository import KnowledgeRepository
from app.modules.knowledge.services.knowledge_service import KnowledgeService, empty_library, effective_documents
from app.modules.knowledge.models.library import KnowledgeLibrary
from app.modules.knowledge.schemas.knowledge import LibrarySnapshot
from app.modules.progress.services.progress_service import ProgressService, deadline_from_text


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def resolve_args(value, observations):
    """只允许引用先前工具回执的公开标量字段，不解释模板代码。"""
    if isinstance(value, str):
        match = re.fullmatch(r'\$\{([a-z][a-z0-9_]{0,19})\.([a-zA-Z][a-zA-Z0-9_]{0,39})\}', value)
        if match:
            source, key = match.groups()
            result = observations.get(source, {}).get(key)
            if not isinstance(result, (str, int, bool)): raise ValueError('tool_reference_missing')
            return result
        if '${' in value: raise ValueError('unsupported_tool_template')
        return value
    if isinstance(value, dict): return {k: resolve_args(v, observations) for k, v in value.items()}
    if value is None or isinstance(value, (bool, int)): return value
    raise ValueError('unsupported_tool_argument')


async def current_sources(session, owner, plan):
    meeting = await session.scalar(select(Meeting).where(Meeting.id == plan.meeting_id, Meeting.owner_id == owner).with_for_update(read=True))
    if not meeting or meeting.owner_id != owner: raise HTTPException(404, 'meeting_not_found')
    sources = {u['id']: u for u in meeting.content['utterances']}
    prepared = plan.content.get('prepared_source_hashes')
    if prepared:
        from app.modules.meetings.services.business_guard import fingerprint
        hashes = {u['id']: fingerprint([u['raw'], u.get('correction'), u.get('withdrawn')]) for u in sources.values()}
        fresh = meeting.analysis_revision == meeting.revision or (meeting.analysis or {}).get('source_hashes') == hashes
        from app.modules.actions.source_snapshot import snapshot_current
        if not fresh and not snapshot_current(prepared, list(sources.values())):
            raise HTTPException(409, 'tool_source_changed')
    disputed = {w['target_id'] for w in (meeting.analysis or {}).get('withdrawal_candidates', [])}
    for key, expected in plan.content['sources'].items():
        source = sources.get(key)
        if not source or source.get('withdrawn') or key in disputed or source.get('turn_status', 'complete') != 'complete' or digest([source['raw'], source.get('correction')]) != expected:
            raise HTTPException(409, 'tool_source_changed')
    return meeting, sources


async def document_version(session, owner, args, retrieval=False):
    library = await KnowledgeRepository(session).library(owner)
    doc = next((d for d in library.snapshot['documents'] if d['id'] == args.documentId), None) if library else None
    version = next((v for v in doc['versions'] if v['id'] == args.versionId), None) if doc else None
    if not version or version['hash'] != args.hash or version['state'] != 'current' or doc['selectedVersionId'] != args.versionId:
        raise HTTPException(409, 'tool_document_changed')
    if retrieval and (doc['id'], version['id']) not in effective_documents(library.snapshot):
        raise HTTPException(403, 'tool_document_not_authorized')
    return doc, version


class ControlledTools:
    def __init__(self, database): self.database = database

    async def call(self, owner, plan, step, observations):
        args = ARGUMENTS[step['tool']].model_validate(resolve_args(step['args'], observations))
        key = digest([owner, plan.id, step['id']])
        signature = digest([step['tool'], args.model_dump()])
        async with self.database.sessions() as session:
            await session.execute(text('SELECT pg_advisory_xact_lock(hashtext(:key))'), {'key': 'tool:' + key})
            await current_sources(session, owner, plan)
            for observation in observations.values():
                if observation.get('readSource'):
                    from app.modules.execution.schemas import ReadArgs
                    await document_version(session, owner, ReadArgs.model_validate(observation['readSource']), retrieval=True)
            old = await session.get(ToolReceipt, key)
            if old:
                if old.input_hash != signature: raise HTTPException(409, 'tool_parameters_changed')
                return old.content
            result = await self.execute(session, owner, plan, step['tool'], args, key)
            session.add(ToolReceipt(id=key, owner_id=owner, plan_id=plan.id, input_hash=signature, content=result))
            await session.commit()
            return result

    async def execute(self, session, owner, plan, name, args, key):
        meeting, sources = await current_sources(session, owner, plan)
        if name == 'source_change':
            from app.modules.meetings.services.source_changes import execute_change
            return await execute_change(session, owner, meeting, sources, args, plan)
        if name == 'knowledge_search':
            hits = await KnowledgeService(session).search(owner, args.query, limit=5)
            selected = hits[0] if hits else None
            reference = {k: selected[k] for k in ['documentId', 'versionId', 'hash']} if selected else None
            return {'hits': hits, 'verified': True, **(reference or {}),
                **({'text': selected['text'], 'readSource': reference,
                    'selectionScope': 'top_search_hit_not_business_confirmation'} if selected else {})}
        if name == 'knowledge_read':
            _, version = await document_version(session, owner, args, retrieval=True)
            body = version['content']
            if not body:
                nodes = await KnowledgeRepository(session).nodes(owner)
                selected = sorted([n for n in nodes if n.document_id == args.documentId and n.version_id == args.versionId], key=lambda n: n.node.get('metadata', {}).get('chunk', 0))
                body = '\n'.join(n.text for n in selected)
            if not body: raise HTTPException(422, 'tool_document_not_parsed')
            return {'text': body[:60000], 'partial': len(body) > 60000, 'readSource': args.model_dump(), 'verified': True}
        if name == 'progress_read':
            return {'snapshot': await ProgressService(session).snapshot(owner), 'verified': True}
        if name == 'task_read':
            # 标题严格匹配且唯一才能返回可操作标量，避免把相似任务误更新。
            rows = list(await session.scalars(select(InternalTask).where(InternalTask.owner_id == owner,
                InternalTask.content['title'].astext == args.query)))
            if len(rows) != 1:
                return {'verified': True, 'matches': len(rows), 'selectionRequired': True}
            row = rows[0]
            return {'verified': True, 'taskId': row.id, 'version': str(row.version), 'status': row.content['status'], 'title': row.content['title']}
        if name in {'task_update', 'task_link'}:
            from app.modules.execution.task_service import TaskService
            service = TaskService(session)
            if name == 'task_update':
                row = await service.change(owner, args.taskId, int(args.version), args.status, args.note, actor='approved_tool_plan')
            else:
                row = await service.bind(owner, args.taskId, int(args.version), args.goalId, int(args.goalVersion),
                    args.milestoneTitle, args.note, actor='approved_tool_plan')
            return {'verified': True, 'taskId': row.id, 'version': str(row.version), 'status': row.content['status']}
        if name == 'task_create':
            words = '\n'.join(sources[i].get('correction') or sources[i]['raw'] for i in plan.content['sources'])
            deadline = None
            if args.deadline_text:
                if args.deadline_text not in words: raise HTTPException(422, 'task_deadline_not_grounded')
                deadline = deadline_from_text(args.deadline_text, meeting.content['started_at'])
                if deadline is None: raise HTTPException(422, 'task_deadline_ambiguous')
            content = {'title': args.title, 'status': 'pending', 'deadline': deadline,
                'evidenceIds': list(plan.content['sources']), 'evidenceHashes': plan.content['sources'], 'attachments': [], 'customerAcceptance': 'pending'}
            session.add(InternalTask(id=key, owner_id=owner, meeting_id=meeting.id, version=1, content=content))
            await session.flush()
            saved = await session.get(InternalTask, key)
            return {'taskId': key, 'title': saved.content['title'], 'version': str(saved.version), 'verified': True}
        if name == 'document_create':
            repository = KnowledgeRepository(session)
            library = await repository.library(owner, lock=True)
            snapshot = copy.deepcopy(library.snapshot) if library else empty_library()
            if not any(f['id'] == 'agent-results' for f in snapshot['folders']):
                snapshot['folders'].append({'id': 'agent-results', 'name': 'AI 成果', 'parentId': None})
            if not any(p['id'] == 'agent-results' for p in snapshot['projects']):
                snapshot['projects'].append({'id': 'agent-results', 'name': '自主行动', 'allowed': False})
            stamp = datetime.now(timezone.utc).isoformat()
            body = args.content if 'AI 草稿' in args.content[:80] else '# AI 草稿（待核对）\n\n' + args.content
            raw = body.encode()
            file_hash = hashlib.sha256(raw).hexdigest()
            doc_id, version_id = 'tool-' + key[:32], 'v-' + key[:32]
            snapshot['documents'].append({'id': doc_id, 'folderId': 'agent-results', 'projectId': 'agent-results',
                'kind': 'markdown', 'source': 'tool:' + plan.id, 'createdAt': stamp, 'selectedVersionId': version_id,
                'fileData': base64.b64encode(raw).decode(), 'fileHash': file_hash, 'filename': args.title + '.md',
                'versions': [{'id': version_id, 'number': 1, 'title': args.title, 'content': body, 'hash': file_hash,
                    'createdAt': stamp, 'state': 'current', 'origin': 'tool:' + plan.id}],
                'allowed': True, 'archived': False, 'demo': False, 'ownerUserId': owner})
            snapshot = LibrarySnapshot.model_validate(snapshot).model_dump(mode='json', by_alias=True)
            if library is None:
                library = KnowledgeLibrary(owner_id=owner, revision=0, snapshot=snapshot); session.add(library)
            library.snapshot, library.revision = snapshot, library.revision + 1
            await KnowledgeService(session)._index(owner, snapshot)
            await session.flush()
            return {'documentId': doc_id, 'versionId': version_id, 'hash': file_hash, 'verified': True}
        if name in {'task_attach', 'task_verify'}:
            from app.modules.execution.task_service import TaskService
            service = TaskService(session)
            await service.lock(owner)
            task = await session.scalar(select(InternalTask).where(InternalTask.id == args.taskId, InternalTask.owner_id == owner).with_for_update())
            if not task: raise HTTPException(404, 'task_not_found')
            if task.content.get('status') == 'verified': raise HTTPException(409, 'task_already_verified')
            doc, version = await document_version(session, owner, args)
            if hashlib.sha256(version['content'].encode()).hexdigest() != args.hash:
                raise HTTPException(409, 'task_evidence_integrity_failed')
            if doc.get('fileData'):
                from app.integrations.llama_index.document_runtime import file_bytes
                try: file_bytes(doc)
                except ValueError as error: raise HTTPException(409, 'task_file_integrity_failed') from error
            attachment = {'documentId': doc['id'], 'versionId': version['id'], 'hash': args.hash, 'fileHash': doc.get('fileHash')}
            attachments = [*task.content.get('attachments', [])]
            if attachment not in attachments: attachments.append(attachment)
            # 只能证明资料存在且版本/哈希可回读，不能证明现实交付或客户验收。
            service.record(task, {'attachments': attachments,
                'status': 'evidence_verified' if name == 'task_verify' else task.content['status'],
                'customerAcceptance': 'pending', 'verificationScope': 'document_integrity_only', 'completionAuthorization': 'pending'},
                '回读文件版本和内容哈希', 'approved_tool_plan')
            await service.project(owner)
            await session.flush()
            return {'taskId': task.id, 'version': str(task.version), 'documentId': doc['id'], 'verified': True, 'customerAcceptance': 'pending', 'verificationScope': 'document_integrity_only'}
        raise HTTPException(422, 'tool_not_allowed')
