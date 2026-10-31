"""原文件解析队列：数据库行锁租用，进程退出可重新领取，写入前回查版本。"""
import asyncio
import logging
from sqlalchemy import select, delete
from app.modules.knowledge.models.library import KnowledgeIndexState, KnowledgeNode
from app.modules.knowledge.repositories.knowledge_repository import KnowledgeRepository
from app.integrations.llama_index.knowledge_index import fingerprint, parse_nodes


class KnowledgeIndexJobs:
    def __init__(self, database): self.database, self.task = database, None

    def start(self): self.task = asyncio.create_task(self.loop())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def loop(self):
        while True:
            try: worked = await self.process_one()
            except asyncio.CancelledError: raise
            except Exception as error:
                logging.getLogger('yanshu.knowledge').warning('index_queue_failed', extra={'error_type': type(error).__name__})
                worked = False
            if not worked: await asyncio.sleep(2)

    async def process_one(self):
        async with self.database.sessions() as session:
            state = await session.scalar(select(KnowledgeIndexState).where(KnowledgeIndexState.status == 'queued').with_for_update(skip_locked=True).limit(1))
            if not state: return False
            repository = KnowledgeRepository(session)
            library = await repository.library(state.owner_id)
            def current(row):
                doc = next((d for d in row.snapshot['documents'] if d['id'] == state.document_id), None) if row else None
                version = next((v for v in doc['versions'] if v['id'] == state.version_id), None) if doc else None
                return (doc, version) if version and fingerprint(doc, version) == state.source_hash else None
            pair = current(library)
            if not pair:
                state.status = 'stale'; await session.commit(); return True
            try:
                nodes = await asyncio.to_thread(parse_nodes, state.owner_id, *pair)
                status = 'indexed' if nodes else 'empty'
            except Exception:
                nodes, status = [], 'parse_unavailable'
            library = await repository.library(state.owner_id)
            if not current(library):
                state.status = 'stale'; await session.commit(); return True
            await session.execute(delete(KnowledgeNode).where(KnowledgeNode.owner_id == state.owner_id, KnowledgeNode.document_id == state.document_id, KnowledgeNode.version_id == state.version_id))
            for node in nodes:
                session.add(KnowledgeNode(id=node.node_id, owner_id=state.owner_id, document_id=state.document_id, version_id=state.version_id,
                    source_hash=state.source_hash, text=node.get_content(), node=node.to_dict()))
            state.status = status
            await session.commit()
            return True
