"""角色独立复核、逐项发布；共享的只是本次运行快照，不共享模型会话。"""

import asyncio
from app.modules.meetings.schemas.agent_roles import VerifyOutput
from app.modules.meetings.schemas.meeting import AgentReport, Insight, Repair, StateFact, Withdrawal
from app.modules.meetings.services.business_guard import fingerprint
from app.modules.meetings.services.incremental_analysis import evidence_ids, review_binding


class ProgressiveReview:
    def __init__(self, agents, sources, context, publish=None, publish_actions=None):
        self.agents, self.sources, self.context = agents, sources, context
        self.publish, self.publish_actions = publish, publish_actions
        self.lock = asyncio.Lock()
        self.parts, self.previews, self.reviews = {}, {}, {}
        self.completed = set()
        self.goals, self.commitments = {}, {}
        self.timings = {}
        self.started = asyncio.get_running_loop().time()
        self.prior = {(v['id'], v['digest']): v for v in
            context.get('previous_analysis', {}).get('candidate_reviews', [])}

    async def emit(self):
        if self.publish:
            await self.publish(self.snapshot())

    def snapshot(self):
        from app.modules.meetings.services.meeting_agents import validate_report
        merged = {k: [] for k in ['insights', 'repairs', 'state', 'withdrawals']}
        schemas = {'insights': Insight, 'repairs': Repair, 'state': StateFact, 'withdrawals': Withdrawal}
        streaming = set()
        def insight_key(i):
            return (i['kind'], i['title'], tuple(sorted(i['evidence_ids'])))
        for name, value in {**self.parts, **self.previews}.items():
            for k in merged:
                for item in value.get(k, []):
                    clean = {key: v for key, v in item.items() if key in schemas[k].model_fields}
                    if k == 'insights' and not clean.get('knowledge_node_ids') and item.get('knowledge_citations'):
                        clean['knowledge_node_ids'] = [h['nodeId'] for h in item['knowledge_citations']][:6]
                    if k == 'insights' and item.get('streaming'):
                        streaming.add(insight_key(item))
                    merged[k].append(clean)
        # 来源与当前角色优先保留，快照大小受现有协议约束；数据库业务项独立保存。
        for k, limit in [('insights', 12), ('repairs', 8), ('state', 30), ('withdrawals', 8)]:
            unique = {}
            for item in merged[k]:
                key = insight_key(item) if k == 'insights' else item.get('target_id', item.get('topic'))
                unique[key] = item
            merged[k] = list(unique.values())[-limit:]
        checked = validate_report(AgentReport.model_validate(merged), self.sources)
        known = {h['nodeId']: h for h in self.context.get('knowledge', [])}
        for insight in checked['insights']:
            insight['knowledge_citations'] = [known[k] for k in insight.get('knowledge_node_ids', []) if k in known]
            # 合并后证据是并集，仍按当前正文的类型/标题和原引用子集保留流式状态。
            insight['streaming'] = any(kind == insight['kind'] and title == insight['title'] and
                set(refs).issubset(insight['evidence_ids']) for kind, title, refs in streaming)
        # 同一标题和来源保持稳定 ID；文本增量不把卡片移动到下一位发言人。
        checked.update(goals=list(self.goals.values()), commitments=list(self.commitments.values()),
            candidate_reviews=list(self.reviews.values()), agents_completed=sorted(self.completed),
            preview_kind='source_checked_inference_only' if self.previews else 'verified_inference_only')
        return checked

    async def partial(self, role, value):
        # 仅有来源的只读推测可提前展示；业务候选和补写原句不走此通道。
        async with self.lock:
            if role in self.parts:
                return
            self.previews[role] = value
            await self.emit()

    async def accept(self, role, value, *, business_only=False):
        """每个完整候选独立审查；慢角色不成为其他角色的屏障。"""
        from app.modules.meetings.services.meeting_agents import role_output
        if role == 'commitment':
            business = [('commitment', c['id'], c) for c in value]
            report = {}
        else:
            report = role_output(role, value)
            business = [('goal', g['origin_id'], g) for g in report.pop('goals', [])]
        # 先审查业务项，以便尽快投影进展；各项仍经确定性的 Service 防线。
        for kind, key, item in business:
            candidate = {'id': kind + ':' + key, 'digest': fingerprint(item), 'kind': kind, 'value': item}
            binding = review_binding(item, self.sources, self.context)
            prior = self.reviews.get((candidate['id'], candidate['digest'])) or self.prior.get((candidate['id'], candidate['digest']))
            reviewed = prior if prior and prior.get('binding') == binding else None
            if reviewed is None:
                ids = evidence_ids(item)
                # 当前变更发言也带入，防止只看旧原话而遗漏否定/撤回。
                from app.modules.meetings.services.incremental_analysis import changed_sources
                ids.update(u['id'] for u in changed_sources(self.sources, self.context))
                response = await self.agents.run_role('verify', {
                    'utterances': [u for u in self.sources if u['id'] in ids],
                    'historical_context': self.context_for_review(), 'candidates': [],
                    'goals': [item] if kind == 'goal' else [],
                    'commitments': [item] if kind == 'commitment' else [],
                    'business_candidates': [candidate]})
                verified = VerifyOutput.model_validate(response)
                reviewed = next((r.model_dump() for r in verified.reviews
                    if r.id == candidate['id'] and r.digest == candidate['digest']), None)
                self.completed.add('verify')
            if reviewed is None:
                continue
            async with self.lock:
                self.reviews[(candidate['id'], candidate['digest'])] = {**reviewed, 'binding': binding}
                (self.goals if kind == 'goal' else self.commitments).pop(key, None)
                if reviewed['accepted']:
                    (self.goals if kind == 'goal' else self.commitments)[key] = item
                    if self.publish_actions:
                        # 这里只发布审查通过的候选，不赋予批准或已履约状态。
                        await self.publish_actions({
                            'goals': [item] if kind == 'goal' else [],
                            'commitments': [item] if kind == 'commitment' else [],
                            'reviewed_increment': True})
                await self.emit()
        if business_only:
            return
        if any(report.get(k) for k in ['insights', 'repairs', 'state', 'withdrawals']):
            response = await self.agents.run_role('verify', {
                'utterances': self.sources, 'historical_context': self.context_for_review(),
                'candidates': [report], 'goals': [], 'commitments': [], 'business_candidates': []})
            verified = VerifyOutput.model_validate(response)
            report = verified.model_dump(exclude={'reviews', 'goals'})
            self.completed.add('verify')
        async with self.lock:
            self.previews.pop(role, None)
            self.parts[role] = report
            self.completed.add(role)
            self.timings[role] = round(asyncio.get_running_loop().time() - self.started, 3)
            if report.get('withdrawals') and self.publish_actions:
                from app.modules.meetings.services.meeting_agents import validate_report
                await self.publish_actions(validate_report(AgentReport.model_validate(report), self.sources))
            await self.emit()

    def context_for_review(self):
        from app.modules.meetings.services.incremental_analysis import compact_context
        return compact_context(self.context, self.sources)
