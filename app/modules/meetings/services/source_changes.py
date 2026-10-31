"""原话变更只生成审批计划；确认后经现有 LangGraph 工具流程原子更新、回读。"""
import json
import re
from datetime import datetime, timezone
from sqlalchemy import select
from fastapi import HTTPException
from app.modules.execution.models import InternalTask
from app.modules.progress.models.progress_item import ProgressItem
from app.modules.actions.models import ActionRun
from app.modules.execution.tools import digest


def named_work(title, speech):
    """同一段原话可能产生多个任务；只允许修改新原话明确提及的那个。"""
    clean = lambda value: re.sub(r'[\s，,。；;：:、“”「」"！!？?]', '', value)
    title, speech = clean(title), clean(speech)
    if len(title) >= 2 and title in speech: return True
    noun = re.sub(r'^(?:(?:我方|我们|负责|提交|完成|创建|准备|处理))+', '', title)
    return len(noun) >= 4 and noun in speech


def change_value(old, new):
    from app.modules.meetings.services.meeting_agents import explicit_change
    if not explicit_change(new['raw']) or new.get('withdrawn') or new.get('correction') or new.get('turn_status', 'complete') != 'complete':
        return None
    if new.get('at', 0) < old.get('at', 0): return None
    # 同词关系是提案过滤，绝不据此认定谁有业务决定权；最终必须本人确认。
    terms = lambda text: {text[i:i+2] for i in range(len(text)-1)} - {'我们', '之前', '刚才', '原来', '现在', '这个', '改为', '改成'}
    if not terms(old['raw']) & terms(new['raw']): return None
    operation = 'replace' if re.search(r'改为|改成|调整为|调整到|延后', new['raw']) else 'cancel'
    return {'target_id': old['id'], 'replacement_id': new['id'], 'operation': operation,
            'old_text': old['raw'], 'new_text': new['raw'], 'evidence_ids': [old['id'], new['id']]}


async def project_changes(session, meeting, report):
    """只给精确关联旧证据的已有任务/承诺生成一个待确认计划。"""
    from app.modules.execution.service import ExecutionService
    sources = {u['id']: u for u in meeting.content['utterances']}
    positions = {u['id']: i for i, u in enumerate(meeting.content['utterances'])}
    changes = []
    handled = set()
    candidates = list(report.get('withdrawal_candidates', []))
    for commitment in report.get('commitments', []):
        for event in commitment.get('pending_changes', []):
            candidates.append({'target_id': commitment['origin_id'], 'evidence_ids': event.get('evidence_ids', [])})
    for candidate in candidates:
        old = sources.get(candidate['target_id'])
        if not old: continue
        for key in candidate.get('evidence_ids', []):
            new = sources.get(key)
            if not new or positions[key] <= positions[old['id']]: continue
            change = change_value(old, new)
            if not change: continue
            pair = (old['id'], new['id'])
            if pair in handled: continue
            handled.add(pair)
            tasks = (await session.scalars(select(InternalTask).where(InternalTask.owner_id == meeting.owner_id, InternalTask.meeting_id == meeting.id))).all()
            progress = (await session.scalars(select(ProgressItem).where(ProgressItem.owner_id == meeting.owner_id, ProgressItem.meeting_id == meeting.id, ProgressItem.kind == 'commitment'))).all()
            matched_tasks = [t for t in tasks if old['id'] in t.content.get('evidenceHashes', {}) and named_work(t.content.get('title', ''), new['raw'])]
            matched_progress = [p for p in progress if not p.content.get('reviewPending') and not p.content.get('reviewRejected') and p.content.get('evidence', {}).get('id') == old['id'] and named_work({**p.content, **p.overrides}.get('title', ''), new['raw'])]
            # 重名条目不猜测；保留候选供核对。
            versions = {'tasks': {t.id: t.version for t in matched_tasks} if len(matched_tasks) <= 1 else {},
                        'commitments': {p.id: p.version for p in matched_progress} if len(matched_progress) <= 1 else {}}
            if versions['tasks'] or versions['commitments']:
                existing = (await session.scalars(select(ActionRun).where(ActionRun.owner_id == meeting.owner_id, ActionRun.meeting_id == meeting.id))).all()
                duplicate = any((action.status in {'completed', 'declined'} or s['args'].get('versions') == json.dumps(versions, sort_keys=True))
                    and s.get('tool') == 'source_change' and s['args'].get('targetHash') == digest([old['raw'], old.get('correction')])
                    and s['args'].get('replacementHash') == digest([new['raw'], new.get('correction')])
                    for action in existing for s in action.content.get('steps', []))
                if duplicate:
                    changes.append(change)
                    continue
                for previous in existing:
                    if previous.status not in {'pending', 'blocked'}: continue
                    if any(s.get('tool') == 'source_change' and s['args'].get('targetId') == old['id']
                           for s in previous.content.get('steps', [])):
                        previous.status = 'superseded'
                label = (matched_tasks[0].content['title'] if versions['tasks'] else matched_progress[0].content['title'])[:60]
                plan = {'title': ('撤回：' if change['operation'] == 'cancel' else '修改：') + label,
                    'evidence_ids': [new['id']], 'steps': [{'id': 'change', 'tool': 'source_change', 'args': {
                        'targetId': old['id'], 'replacementId': new['id'], 'targetHash': digest([old['raw'], old.get('correction')]),
                        'replacementHash': digest([new['raw'], new.get('correction')]),
                        'versions': json.dumps(versions, sort_keys=True), 'operation': change['operation']}}]}
                await ExecutionService(session).project(meeting, [plan], source_change=True)
            changes.append(change)
    return list({(c['target_id'], c['replacement_id']): c for c in changes}.values())


async def execute_change(session, owner, meeting, sources, args, plan):
    from app.modules.execution.task_service import TaskService
    old, new = sources.get(args.targetId), sources.get(args.replacementId)
    if not old or not new or digest([old['raw'], old.get('correction')]) != args.targetHash or digest([new['raw'], new.get('correction')]) != args.replacementHash:
        raise HTTPException(409, 'source_change_evidence_changed')
    change = change_value(old, new)
    if not change or change['operation'] != args.operation or not plan.content.get('approved'):
        raise HTTPException(409, 'source_change_not_approved')
    versions = json.loads(args.versions)
    service = TaskService(session)
    await service.lock(owner)
    updated = []
    for id, version in versions.get('tasks', {}).items():
        task = await service.get(owner, id, version)
        if task.meeting_id != meeting.id or old['id'] not in task.content.get('evidenceHashes', {}) or not named_work(task.content.get('title', ''), new['raw']):
            raise HTTPException(409, 'source_change_task_mismatch')
        values = {'status': 'cancelled' if args.operation == 'cancel' else 'pending',
                  'completionAuthorization': 'pending', 'customerAcceptance': 'pending',
                  'sourceChangeId': plan.id}
        if args.operation == 'replace':
            values.update(title=new['raw'], evidenceHashes={new['id']: args.replacementHash}, verificationScope=None)
            # 原交期不能偷偷沿用；仅明确提及新日期时回读为新交期。
            from app.modules.progress.services.progress_service import deadline_from_text
            dates = re.findall(r'(?:本|下|这)?周[一二三四五六日天]|\d{4}[-/]\d{1,2}[-/]\d{1,2}', new['raw'])
            values['deadline'] = deadline_from_text(dates[0], meeting.content['started_at']) if len(dates) == 1 else None
        before = {k: task.content.get(k) for k in ('title', 'status', 'deadline', 'evidenceHashes')}
        service.record(task, values, new['raw'], 'account_owner')
        task.content = {**task.content, 'history': [*task.content['history'][:-1],
                       {**task.content['history'][-1], 'before': before}]}
        updated.append({'kind': 'task', 'id': id, 'version': task.version, 'status': task.content['status']})
    for id, version in versions.get('commitments', {}).items():
        row = await session.scalar(select(ProgressItem).where(ProgressItem.id == id, ProgressItem.owner_id == owner).with_for_update())
        if not row or row.version != version or row.meeting_id != meeting.id or row.content.get('evidence', {}).get('id') != old['id'] or not named_work({**row.content, **row.overrides}.get('title', ''), new['raw']):
            raise HTTPException(409, 'source_change_commitment_changed')
        values = {'status': 'withdrawn' if args.operation == 'cancel' else 'pending', 'authorizationStatus': 'owner_confirmed'}
        if args.operation == 'replace':
            from app.modules.progress.services.progress_service import deadline_from_text
            dates = re.findall(r'(?:本|下|这)?周[一二三四五六日天]|\d{4}[-/]\d{1,2}[-/]\d{1,2}', new['raw'])
            values.update(title=new['raw'], deadline=deadline_from_text(dates[0], meeting.content['started_at']) if len(dates) == 1 else None)
        before = {k: {**row.content, **row.overrides}.get(k) for k in ('title', 'status', 'deadline')}
        row.version += 1
        row.overrides = {**row.overrides, **values}
        row.history = [*row.history, {'at': datetime.now(timezone.utc).isoformat(), 'version': row.version,
                       'actor': 'account_owner', 'sourceChangeId': plan.id, 'oldText': old['raw'], 'newText': new['raw'], 'before': before, **values}]
        updated.append({'kind': 'commitment', 'id': id, 'version': row.version, 'status': values['status']})
    if not updated: raise HTTPException(409, 'source_change_no_matching_work')
    await session.flush()
    await service.project(owner)
    # 此处 verified 仅为数据库写入、版本和回读，不代表交付或客户验收完成。
    return {'verified': True, 'sourceChange': {**change, 'status': 'confirmed'}, 'updated': updated}


async def read_changes(session, meeting):
    rows = (await session.scalars(select(ActionRun).where(ActionRun.owner_id == meeting.owner_id, ActionRun.meeting_id == meeting.id))).all()
    sources = {u['id']: u for u in meeting.content['utterances']}
    result = [c for c in (meeting.analysis or {}).get('source_changes', [])
              if c.get('target_id') in sources and c.get('replacement_id') in sources
              and change_value(sources[c['target_id']], sources[c['replacement_id']])]
    for row in rows:
        for step in row.content.get('steps', []):
            if step.get('tool') != 'source_change' or row.status == 'superseded': continue
            args = step['args']
            old, new = sources.get(args['targetId']), sources.get(args['replacementId'])
            if not old or not new: continue
            change = change_value(old, new)
            if not change: continue
            result.append({**change, 'action_id': row.id,
                'status': 'confirmed' if row.status == 'completed' else ('rejected' if row.status == 'declined' else 'pending')})
    return list({(c['target_id'], c['replacement_id']): c for c in result}.values())
