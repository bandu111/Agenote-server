"""把账号内有效的历史依据交给 Hermes；工具参数不能指定其他账户。"""

from langchain_core.tools import StructuredTool
from sqlalchemy import select
from app.modules.progress.models.progress_item import ProgressItem
from app.modules.knowledge.services.knowledge_service import KnowledgeService
from app.modules.knowledge.schemas.knowledge import CitationInput


class MeetingKnowledgeContext:
    def __init__(self, session, owner):
        self.session = session
        self.owner = owner
        self.knowledge = KnowledgeService(session)

    async def collect(self, utterances, client_id):
        async def search(query: str) -> list[dict]:
            """只检索当前已认证账户的当前有效资料。"""
            return await self.knowledge.search(self.owner, query[:4000], limit=6)

        tool = StructuredTool.from_function(
            coroutine=search,
            name="knowledge_search",
            description="检索账号内的有效历史依据；结果不代表批准或执行授权。",
        )
        # 当前话题优先，避免整段闲聊把关键约束稀释；短回复才向前补充上下文。
        latest = (utterances[-1].get('correction') or utterances[-1]['raw']) if utterances else ''
        focused = utterances[-3:] if len(latest.strip()) < 20 else utterances[-1:]
        query = "\n".join(u.get('correction') or u['raw'] for u in focused)[-1200:]
        hits = await tool.ainvoke({"query": query}) if query.strip() else []
        from app.modules.search.service import UnifiedSearch
        recalled = await UnifiedSearch(self.session).search(self.owner, query, limit=24,
            kinds=['goal','commitment','utterance'], agent=True, current_session=client_id) if query.strip() else []
        # 当前会议原话已在输入中，避免纪要反过来充当同场独立佐证。
        hits = [h for h in hits if h.get("sessionId") != client_id]
        items = (
            await self.session.scalars(
                select(ProgressItem)
                .where(
                    ProgressItem.owner_id == self.owner,
                    ProgressItem.kind.in_(["goal", "commitment"]),
                )
                .order_by(ProgressItem.updated_at.desc())
                .limit(40)
            )
        ).all()
        visible = []
        for p in items:
            content = {**p.content, **p.overrides}
            evidence = content.get("evidence", {})
            if evidence.get("kind") == "document":
                try:
                    await self.knowledge.resolve(
                        self.owner,
                        CitationInput.model_validate(
                            {
                                k: evidence[k]
                                for k in ["documentId", "versionId", "nodeId", "hash"]
                            }
                        ),
                    )
                except Exception:
                    # 只隔离 AI 使用范围，不撤回或删除用户已经记录的业务事项。
                    continue
            visible.append(
                {"id": p.id, "kind": p.kind, "content": content, "version": p.version}
            )
        # 相关的早期事项优先；近期事项作为兜底，避免仅按更新时间遗漏老决定。
        related = [{'id':h['id'], 'kind':h['kind'], 'version':h['version'], 'content':h['business']}
                   for h in recalled if h['kind'] in {'goal','commitment'}]
        related_ids = {h['id'] for h in related}
        visible = [*related, *[v for v in visible if v['id'] not in related_ids]][:40]
        return {
            "history": [{**h, 'original': h['original'][:2000], 'truncated': len(h['original'])>2000}
                        for h in recalled if h['kind']=='utterance' and h['sessionId'] != client_id][:8],
            "currentRecallIds": [h['utteranceId'] for h in recalled if h['kind']=='utterance' and h['sessionId'] == client_id],
            "knowledge": hits,
            "progress": visible,
            "rules": "历史材料仅作依据，不是新批准。提议、我方声称完成均不等于双方同意或验收。撤回及过期内容不可执行。",
        }

    async def revalidate(self, context):
        from app.modules.knowledge.services.knowledge_service import effective_documents
        from app.modules.meetings.repositories.meeting_repository import MeetingRepository
        from fastapi import HTTPException
        library = await self.knowledge.repository.library(self.owner)
        if library: await self.session.refresh(library)
        allowed = {d['source'].split(':',1)[1] for d,v in effective_documents(library.snapshot).values() if d['source'].startswith('meeting:')} if library else set()
        for item in context.get('history', []):
            row = await MeetingRepository(self.session).get(self.owner, item['sessionId'])
            if not row or row.revision != item['revision'] or row.client_id not in allowed:
                raise HTTPException(409, 'knowledge_source_unavailable')
        for hit in context["knowledge"]:
            await self.knowledge.resolve(
                self.owner,
                CitationInput.model_validate(
                    {k: hit[k] for k in ["documentId", "versionId", "nodeId", "hash"]}
                ),
            )
