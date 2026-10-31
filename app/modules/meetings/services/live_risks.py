"""会中风险增量流：不等待上一轮完整分析，不投影任何正式业务状态。"""

import asyncio
from app.modules.meetings.schemas.meeting import AgentReport
from app.modules.meetings.services.hint_gate import HintGate
from app.modules.meetings.services.business_guard import transcript_hashes
from app.modules.meetings.services.incremental_analysis import retained, evidence_ids
from app.modules.meetings.services.meeting_agents import validate_report


def risk_context(sources, stored_sources=(), previous=None):
    """仅携带有当前原话依据的有效状态；历史检索和业务复核仍由完整链路完成。"""
    previous = previous or {}
    if previous.get('business_guard_version') != 3:
        previous = {}
    facts = retained(previous.get('state', []), stored_sources, previous)[-12:]
    # 被客户端纠正但尚未同步的来源也不能继续支撑旧状态。
    current = transcript_hashes(sources)
    old = previous.get('source_hashes', {})
    facts = [f for f in facts if all(k not in current or current[k] == old.get(k)
                                   for k in evidence_ids(f))]
    ids = {k for fact in facts for k in evidence_ids(fact)}
    selected = {u['id']: u for u in stored_sources if u['id'] in ids}
    selected.update({u['id']: u for u in sources})
    # 较早的有效事实只传简短状态及其原始依据，不能无限增长模型输入。
    ordered = list(selected.values())
    ordered.sort(key=lambda u: u.get('at', 0))
    bounded, count = [], 0
    for u in reversed(ordered):
        size = len(u['raw']) + len(u.get('correction') or '')
        if count + size <= 12000 and len(bounded) < 32:
            bounded.insert(0, u)
            count += size
    available = {u['id'] for u in bounded}
    facts = [f for f in facts if evidence_ids(f) <= available]
    return bounded, {'current_effective_state': facts,
        'rules': '只有当前来源支持的有效状态可作依据；这是只读推测，不是批准或执行授权。'}


class LiveMeetingRisks:
    def __init__(self, agents):
        self.agents = agents
        self.gate = HintGate()
        self.owners = set()
        self.capacity = asyncio.Semaphore(2)

    async def stream(self, owner, meeting, sources, stored_sources=(), previous=None):
        def frame(event, insights=(), status='completed'):
            return {'event': event, 'kind': 'source_checked_inference_only',
                'insights': list(insights), 'repairs': [], 'businessEffect': 'none',
                'modelStatus': status}

        if owner in self.owners:
            yield frame('done', status='busy')
            return
        decision = self.gate.prepare(owner, meeting, sources, roles={'risk'})
        if not decision['needed']:
            self.gate.remember(decision)
            yield frame('done', status='not_needed')
            return
        self.owners.add(owner)
        acquired, task, latest = False, None, []
        changed = asyncio.Event()
        selected, context = risk_context(sources, stored_sources, previous)
        ids = set(decision['delta_ids'])

        async def partial(value):
            nonlocal latest
            checked = validate_report(AgentReport.model_validate({
                'insights': [{k: v for k, v in i.items() if k != 'streaming'}
                             for i in value.get('insights', [])]}), selected)
            # 历史来源仅作约束，不能把历史独立问题再次挂到新发言下。
            latest = [{**i, 'streaming': True} for i in checked['insights']
                      if i['kind'] == 'risk' and ids.intersection(i['evidence_ids'])]
            changed.set()

        try:
            try:
                await asyncio.wait_for(self.capacity.acquire(), .1)
                acquired = True
            except TimeoutError:
                yield frame('done', status='busy')
                return
            # 超时包括排队；同一客户端只保留正在运行和最新批次，不积累逐句任务。
            async with asyncio.timeout(12):
                task = asyncio.create_task(self.agents.run_role('risk', {
                    'utterances': selected, 'historical_context': context,
                    'delta_ids': decision['delta_ids'], 'preview_only': True}, on_partial=partial))
                task.add_done_callback(lambda _: changed.set())
                yield frame('snapshot', status='pending')
                while not task.done():
                    await changed.wait()
                    changed.clear()
                    if latest:
                        yield frame('preview', latest, 'streaming')
                value = task.result()
                checked = validate_report(AgentReport.model_validate(value), selected)
                latest = [{**i, 'streaming': False} for i in checked['insights']
                          if i['kind'] == 'risk' and ids.intersection(i['evidence_ids'])]
                self.gate.remember(decision, generated=True)
                yield frame('done', latest)
        except asyncio.CancelledError:
            raise
        except Exception:
            yield frame('done', [{**i, 'streaming': False, 'interrupted': True} for i in latest], 'unavailable')
        finally:
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            if acquired:
                self.capacity.release()
            self.owners.discard(owner)
