"""限制分析并发、复用同版本结果，禁止旧分析覆盖新会议。"""

import asyncio
from weakref import WeakValueDictionary
from pydantic import ValidationError
from app.modules.meetings.repositories.meeting_repository import MeetingRepository
from app.modules.meetings.services.meeting_service import (
    MeetingConflictError,
    MeetingNotFoundError,
)
from app.integrations.hermes.provider import HermesUnavailableError
from app.modules.progress.services.progress_service import ProgressService
from app.modules.knowledge.services.meeting_context import MeetingKnowledgeContext
from app.modules.actions.service import ActionService
from app.modules.knowledge.repositories.knowledge_repository import KnowledgeRepository
from app.modules.meetings.services.business_guard import guarded_report, transcript_hashes


class MeetingAnalysisBusyError(Exception):
    """分析容量已满，客户端稍后重试。"""


class MeetingAnalysisCoordinator:
    """单实例运行时限制同时分析两场会议，锁在请求结束后自动释放。"""

    def __init__(self, agents, database=None):
        self.agents = agents
        self.database = database
        self.enrichments = {}
        self.enrichment_capacity = asyncio.Semaphore(1)
        self.capacity = asyncio.Semaphore(2)
        self.locks = WeakValueDictionary()
        self.on_actions_published = None

    async def analyze(self, session, owner_id: str, client_id: str, on_core_ready=None):
        key = (owner_id, client_id)
        lock = self.locks.setdefault(key, asyncio.Lock())
        try:
            await asyncio.wait_for(lock.acquire(), timeout=1)
        except TimeoutError as error:
            raise MeetingAnalysisBusyError() from error
        try:
            repository = MeetingRepository(session)
            row = await repository.get(owner_id, client_id)
            if row is None:
                raise MeetingNotFoundError()
            library = await KnowledgeRepository(session).library(owner_id)
            knowledge_revision = library.revision if library else 0
            if (
                row.analysis_revision == row.revision
                and row.analysis is not None
                and row.analysis.get("knowledge_revision", 0) == knowledge_revision
                and row.analysis.get("business_guard_version") == 3
                and not row.analysis.get('realtime')
            ):
                await ProgressService(session).project(row, row.analysis)
                await session.commit()
                return {
                    "revision": row.revision,
                    "cached": True,
                    "analysis": row.analysis,
                }
            revision = row.revision
            # 临时片段只持久化为证据，不因 ASR 提前切句而启动意图和行动判断。
            utterances = [u for u in row.content["utterances"] if u.get('turn_status', 'complete') != 'pending']
            knowledge = MeetingKnowledgeContext(session, owner_id)
            context = await knowledge.collect(utterances, client_id)
            context["meeting_started_at"] = row.content["started_at"]
            context["client_id"] = client_id
            context['knowledge_revision'] = knowledge_revision
            context['tool_plans_enabled'] = 'tool_plans_v1' in row.content.get('capabilities', [])
            if context['tool_plans_enabled']:
                from sqlalchemy import select
                from app.modules.execution.models import InternalTask
                from app.modules.progress.models.progress_item import ProgressItem
                from app.modules.progress.services.progress_service import output
                tasks = await session.scalars(select(InternalTask).where(InternalTask.owner_id == owner_id).order_by(InternalTask.id).limit(100))
                goals = await session.scalars(select(ProgressItem).where(ProgressItem.owner_id == owner_id, ProgressItem.kind == 'goal').order_by(ProgressItem.id).limit(100))
                context['execution_catalog'] = {
                    'tasks': [{'taskId': t.id, 'version': str(t.version), 'title': t.content['title'], 'status': t.content['status']} for t in tasks],
                    'goals': [{'goalId': g.id, 'goalVersion': str(g.version), 'title': output(g)['title'],
                        'milestones': [m['title'] for m in output(g).get('milestones', [])], 'status': output(g).get('status')} for g in goals]}

            # 只恢复由当前会议原话支持的状态，避免重用已撤销授权的旧检索正文。
            previous = row.analysis or {}
            context["previous_analysis"] = {
                k: previous[k]
                for k in [
                    "source_hashes",
                    "goals",
                    "commitments",
                    "state",
                    "candidate_reviews",
                    "questionnaire",
                    "insights",
                    "repairs",
                    'tool_plans',
                ]
                if k in previous and previous.get("business_guard_version") == 3
            }
            if previous.get('realtime'):
                # 会中轻量投影不代表所有后台角色已处理；二者分别记录扫描位置。
                context['previous_analysis']['source_hashes'] = previous.get('full_analysis_source_hashes', {})
            context["knowledge_changed"] = (row.analysis or {}).get(
                "knowledge_revision", 0
            ) != knowledge_revision
            # 等待模型期间释放数据库事务；所有写入在返回时重新检查版本。
            await session.rollback()
            try:
                await asyncio.wait_for(self.capacity.acquire(), timeout=1)
            except TimeoutError as error:
                raise MeetingAnalysisBusyError() from error
            try:
                if utterances:
                    publication_lock = asyncio.Lock()
                    async def publish_core(candidate):
                        async with publication_lock:
                            try:
                                current = await repository.get(owner_id, client_id)
                                if current is None: return
                                # 后续追加发言不阻断旧来源的只读正文流；纠错、撤回或身份更正仍使流失效。
                                before = transcript_hashes(utterances)
                                now = transcript_hashes(current.content['utterances'])
                                if any(now.get(key) != value for key, value in before.items()): return
                                library = await KnowledgeRepository(session).library(owner_id)
                                if (library.revision if library else 0) != knowledge_revision: return
                                await knowledge.revalidate(context)
                                checked = guarded_report(candidate, utterances)
                                await on_core_ready({**{k: checked.get(k, []) for k in ['insights', 'repairs', 'agents_completed']},
                                    'kind': candidate.get('preview_kind', 'verified_inference_only')})
                            finally:
                                await session.rollback()
                    async def publish_actions(candidate):
                        # 只提交待审批提案；不写正式承诺、不执行工具，也不等风险/问卷完成。
                        async with publication_lock:
                            try:
                                current = await repository.get(owner_id, client_id, lock=True)
                                if current is None or current.revision != revision: return
                                library = await KnowledgeRepository(session).library(owner_id)
                                if (library.revision if library else 0) != knowledge_revision: return
                                await knowledge.revalidate(context)
                                from sqlalchemy import select
                                from app.modules.actions.models import ActionRun
                                from app.modules.execution.service import ExecutionService
                                from app.modules.meetings.services.source_changes import project_changes
                                from app.modules.meetings.services.business_guard import fingerprint
                                before = set(await session.scalars(select(ActionRun.id).where(ActionRun.owner_id == owner_id, ActionRun.meeting_id == current.id)))
                                checked = guarded_report(candidate, utterances)
                                if candidate.get('reviewed_increment'):
                                    # 单项已完成独立审查，提前持久化候选；不清除其他临时投影。
                                    await ProgressService(session).project(current, checked, incremental=True)
                                await ExecutionService(session).project(current, checked.get('tool_plans', []))
                                await ActionService(session, None).project(current, checked.get('action_intents', []))
                                await project_changes(session, current, candidate)
                                await session.flush()
                                hashes = {u['id']: fingerprint([u['raw'], u.get('correction'), u.get('withdrawn')]) for u in current.content['utterances']}
                                rows = list(await session.scalars(select(ActionRun).where(ActionRun.owner_id == owner_id, ActionRun.meeting_id == current.id)))
                                added = [r for r in rows if r.id not in before]
                                for action in added:
                                    action.content = {**action.content, 'prepared_source_hashes': hashes}
                                await session.commit()
                                if added and self.on_actions_published:
                                    self.on_actions_published()
                                if added and on_core_ready is not None:
                                    await on_core_ready({'kind': 'action_preparation', 'actions_prepared': True})
                            finally:
                                await session.rollback()
                    options = {'context': context, 'on_actions_ready': publish_actions}
                    if on_core_ready is not None: options['on_core_ready'] = publish_core
                    result = await asyncio.wait_for(
                        self.agents.analyze(utterances, **options), timeout=150
                    )
                else:
                    result = {
                        "insights": [],
                        "repairs": [],
                        "withdrawals": [],
                        "provider": "hermes-deepseek",
                        "agents_completed": [],
                    }
            except (
                ValidationError,
                ValueError,
                KeyError,
                TypeError,
                TimeoutError,
            ) as error:
                raise HermesUnavailableError() from error
            finally:
                self.capacity.release()
            row = await repository.get(owner_id, client_id, lock=True)
            if row is None:
                raise MeetingNotFoundError()
            if row.revision != revision:
                raise MeetingConflictError()
            await knowledge.revalidate(context)
            library = await KnowledgeRepository(session).library(owner_id, lock=True)
            if (library.revision if library else 0) != knowledge_revision:
                raise MeetingConflictError()
            result["knowledge_revision"] = knowledge_revision
            result = guarded_report(result, utterances)
            row.analysis = result
            row.analysis_revision = revision
            await ProgressService(session).project(row, result)
            from app.modules.meetings.services.source_changes import project_changes, read_changes
            proposed_changes = await project_changes(session, row, result)
            await session.flush()
            persisted_changes = await read_changes(session, row)
            mapped = {(c['target_id'], c['replacement_id']): {**c, 'status': 'pending'} for c in proposed_changes}
            mapped.update({(c['target_id'], c['replacement_id']): c for c in persisted_changes})
            result['source_changes'] = list(mapped.values())
            row.analysis = result
            await ActionService(session, None).project(
                row, result.get("action_intents", [])
            )
            from app.modules.execution.service import ExecutionService
            await ExecutionService(session).project(row, result.get('tool_plans', []))
            await session.commit()
            self.schedule_questionnaire(owner_id, client_id, revision, result, context)
            return {"revision": revision, "cached": False, "analysis": result}
        finally:
            lock.release()

    def schedule_questionnaire(self, owner, client_id, revision, result, context):
        """核心提交后再整理问题，独立连接与版本复查，不延长核心请求。"""
        if self.database is None or not result.get('questionnaire_pending'):
            return
        key = (owner, client_id)
        active = self.enrichments.get(key)
        if active and not active.done():
            active.cancel()
        if len(self.enrichments) >= 8 and key not in self.enrichments:
            return
        task = asyncio.create_task(self.enrich_questionnaire(owner, client_id, revision, result, context))
        self.enrichments[key] = task
        def finished(value):
            if self.enrichments.get(key) is value:
                self.enrichments.pop(key, None)
        task.add_done_callback(finished)

    async def enrich_questionnaire(self, owner, client_id, revision, result, context):
        from app.modules.meetings.services.decision_questionnaire import DecisionQuestionnaire, meeting_gaps
        from app.modules.meetings.services.meeting_agents import append_questions
        from app.modules.meetings.services.business_guard import fingerprint
        try:
            async with self.enrichment_capacity:
                questionnaire = await asyncio.wait_for(DecisionQuestionnaire(self.agents.provider).build(
                    meeting_gaps(result), context, result.get('questionnaire')), timeout=20)
                async with self.database.sessions() as session:
                    row = await MeetingRepository(session).get(owner, client_id, lock=True)
                    library = await KnowledgeRepository(session).library(owner)
                    if (row is None or row.revision != revision or row.analysis_revision != revision
                        or (library.revision if library else 0) != context.get('knowledge_revision', 0)
                        or fingerprint((row.analysis or {}).get('questionnaire')) != fingerprint(result.get('questionnaire'))):
                        return
                    await MeetingKnowledgeContext(session, owner).revalidate(context)
                    updated = {**row.analysis, 'questionnaire': questionnaire, 'questionnaire_pending': False}
                    append_questions(updated)
                    row.analysis = guarded_report(updated, row.content['utterances'])
                    await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            import logging
            # 问题润色失败保留确定性缺口；日志不包含原话和模型正文。
            logging.getLogger('yanshu.meetings').info('questionnaire_deferred type=%s', type(error).__name__)

    async def close(self):
        tasks = list(self.enrichments.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
