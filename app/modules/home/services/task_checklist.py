"""会议提取事项的个人清单；勾选只确认待办已处理，不代替履约核验。"""
import hashlib
import json
import re
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import select
from app.modules.progress.models.progress_item import ProgressItem
from app.modules.progress.services.progress_service import ProgressService, output
from app.modules.execution.models import InternalTask
from app.modules.execution.task_service import TaskService, source_current


def fingerprint(item):
    value = [item.get('title'), item.get('sourceEvidenceHashes') or item.get('evidenceHashes') or {}]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


VAGUE_OBJECTS = {'事情', '工作', '任务', '事项', '内容', '问题', '信息', '情况', '进度', '风险', '安排', '后续', '一下'}
ACTION = re.compile(r'^(?:提交|交付|提供|发送|整理|编写|修复|排查|解决|完成|测试|验证|联系|沟通|确认|核对|评估|审查|审核|设计|开发|上线|发布|部署|收集|汇总|统计|采购|准备|对接|签署|签订|回复|开通|升级|实现|协调|跟进|补齐|处理|调查|检查|发|交|写|查|修|submit\b|send\b|review\b|fix\b|deploy\b|prepare\b|check\b|test\b|write\b)', re.I)


def specific_object(value):
    plain = re.sub(r'^(?:(?:这|那)(?:个|些|项|件)?|相关|一些|几个|剩余|[一二三四五六七八九十两\d]+(?:个|项|件)?)+', '', value.strip())
    return len(plain) >= 2 and plain not in VAGUE_OBJECTS


def concrete(title):
    title = title.strip().rstrip('。；;，,')
    if len(title) < 3 or title in VAGUE_OBJECTS:
        return False
    match = ACTION.match(title)
    if match:
        target = title[match.end():].strip(' 的一下')
        return specific_object(target)
    return False


def actionable(item):
    """复用本轮 AI 语义分类，并要求原话中的具体行动与执行意图；不另调模型。"""
    title = item.get('title', '').strip().rstrip('。；;！？?')
    if re.match(r'^(?:再?看看|再?讨论|考虑|重视|关注|注意|了解|说明|认为|觉得|还有|存在|目前|已经)', title):
        return False
    if item.get('kind') == 'internal_task':
        # 内部任务已经过明确创建授权，不把已完成待核验重新当成要做的事。
        return len(title) >= 3 and title not in VAGUE_OBJECTS and item.get('status', 'pending') in {'pending', 'active'}
    intent = item.get('taskIntent')
    confirmed = item.get('authorizationStatus') == 'owner_confirmed' and item.get('status') in {'planned', 'active'}
    if intent in {'context', 'conditional'} or intent in {'proposal', 'completed'} and not confirmed:
        return False
    state = item.get('candidateStatus') or next((e.get('kind') for e in reversed(item.get('sourceEvents', []))), None)
    if state in {'claimed_completed', 'withdrawn'} and not confirmed:
        return False
    if item.get('status') in {'claimed', 'withdrawn'}:
        return False
    source = item.get('taskSourceText') or item.get('evidence', {}).get('transcript', '')
    from app.modules.progress.services.goal_evidence import certain_clause
    if not title or title not in source:
        return False
    if not certain_clause(source, title) and not (confirmed and intent != 'conditional'):
        return False
    clauses = [c for c in re.split(r'[。；;！？?\n]', source) if title in c]
    if not confirmed and any(re.search(
        r'(?:不承诺|不保证|不能保证|不负责|不会|不需要|无法|是否|要不要|能不能|还没决定|尚未确定).{0,10}$',
        c[:c.index(title)]) for c in clauses):
        return False
    target = (item.get('taskDeliverable') or '').strip()
    semantic_task = intent == 'todo' and specific_object(target) and target in title
    # 新数据按 AI 的具体对象分类；旧记录无分类时才用保守的动作+对象结构兼容。
    if not semantic_task and not concrete(title):
        return False
    explicit = confirmed or state in {'committed', 'in_progress'} or bool(item.get('deadline'))
    explicit |= any(re.search(r'明天|后天|下(?:周|星期|个月|月)|周[一二三四五六日天]|星期[一二三四五六日天]|负责|承诺|保证|由我|我[们方]?会|交给', c) for c in clauses)
    if semantic_task:
        explicit |= any(re.search(r'需要|请|必须|应当|安排', c) for c in clauses)
    return bool(explicit)


def expired(item, now=None):
    value = item.get('deadline')
    if not value:
        return False
    try:
        deadline = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now >= deadline


def checklist_item(item, *, now=None, include_expired=False):
    if item.get('kind') not in {'commitment', 'internal_task'}:
        return None
    if not actionable(item):
        return None
    if item.get('reviewRejected') or item.get('status') in {'withdrawn', 'cancelled'}:
        return None
    if item['kind'] == 'commitment' and not item.get('evidenceCurrent', True):
        return None
    if not item.get('sourceCurrent', True):
        return None
    if not include_expired and expired(item, now):
        return None
    if item.get('kind') == 'commitment' and not item.get('evidence', {}).get('sessionId'):
        return None
    completed = (item.get('checklistCompleted') is True and
                 item.get('checklistFingerprint') == fingerprint(item))
    completed = completed or item.get('status') in {'verified', 'completed'}
    return {
        'id': ('task:' if item['kind'] == 'internal_task' else 'commitment:') + item['id'],
        'title': item['title'], 'version': item['version'],
        'deadline': item.get('deadline'), 'createdAt': item.get('createdAt'),
        'completed': completed, 'side': item.get('side', 'unknown'),
        'reviewPending': item.get('reviewPending', False),
        'taskIntent': item.get('taskIntent'),
        'notes': item.get('evidence', {}).get('transcript') or item.get('notes') or item.get('summary', ''),
        'sessionId': item.get('evidence', {}).get('sessionId') or item.get('sessionId'),
        'source': '会议整理 · ' + {'ours': '我方事项', 'theirs': '对方事项'}.get(item.get('side'), '责任待确认'),
        'sourceRoute': '/progress/commitments/' + item['id'] if item['kind'] == 'commitment' else None,
    }


def checklist_items(progress, *, now=None):
    # 同一来源的待办摘要和后续承诺只展示一份，保留先前待办的稳定 ID 与勾选状态。
    chosen = {}
    for item in sorted(progress, key=lambda p: p.get('sourceKind') != 'meeting_todo'):
        task = checklist_item(item, now=now)
        if task:
            origin = item.get('originId') or item.get('evidence', {}).get('id')
            key = (task['sessionId'], task['title'], origin or tuple(sorted(item.get('evidenceIds', []))))
            chosen.setdefault(key, task)
    return list(chosen.values())


def meeting_todos(candidates, sources):
    """待办摘要不等于本人承诺：保留他人委派，但不提供执行授权。"""
    from app.modules.meetings.services.business_guard import supported_action, normalized
    source_map = {u['id']: u for u in sources if not u.get('withdrawn') and u.get('turn_status', 'complete') == 'complete'}
    result = {}
    for candidate in candidates:
        value = candidate.model_dump(mode='json') if hasattr(candidate, 'model_dump') else candidate
        origin = source_map.get(value.get('origin_id'))
        if not origin:
            continue
        quote = (value.get('action_quote') or value.get('action') or '').strip()
        if not supported_action(quote, [origin]):
            continue
        words = origin.get('correction') or origin['raw']
        events = value.get('events', [])
        kinds = [e['kind'] for e in events]
        intent = value.get('task_intent')
        target = value.get('deliverable_quote')
        if target and target not in words:
            target = None
        proposed = {'kind': 'commitment', 'title': quote, 'taskIntent': intent,
            'taskDeliverable': target, 'candidateStatus': kinds[-1] if kinds else 'proposed',
            'evidence': {'transcript': words}, 'taskSourceText': words}
        if not actionable(proposed):
            continue
        deadline = next((e.get('deadline_text') for e in events
            if e.get('kind') in {'proposed', 'committed', 'in_progress'} and origin['id'] in e.get('evidence_ids', [])
            and e.get('deadline_text') and e['deadline_text'] in words), None)
        key = hashlib.sha256(json.dumps([origin['id'], normalized(quote)], ensure_ascii=False).encode()).hexdigest()[:32]
        result[key] = {'id': key, 'origin_id': origin['id'], 'action': quote,
            'task_intent': intent, 'deliverable_quote': target, 'deadline_text': deadline,
            'evidence_ids': [origin['id']]}
    return list(result.values())


async def project_meeting_todos(session, meeting, todos, *, preview, reviewed_sources):
    """只登记个人待办，不创建日历/执行工具；人工完成及历史不会被重跑覆盖。"""
    from app.modules.execution.tools import digest
    from app.modules.progress.services.progress_service import deadline_from_text
    sources = {u['id']: u for u in meeting.content['utterances']}
    accepted = set()
    for todo in todos:
        origin = sources.get(todo['origin_id'])
        if not origin:
            continue
        key = 'todo-' + hashlib.sha256(f'{meeting.owner_id}:{meeting.id}:{todo["id"]}'.encode()).hexdigest()[:48]
        accepted.add(key)
        row = await session.scalar(select(InternalTask).where(InternalTask.id == key, InternalTask.owner_id == meeting.owner_id))
        hashes = {i: digest([sources[i]['raw'], sources[i].get('correction')]) for i in todo['evidence_ids']}
        if (row and not row.content.get('reviewPending') and not row.content.get('reviewRejected')
            and row.content.get('evidenceHashes') == hashes):
            continue
        words = origin.get('correction') or origin['raw']
        content = {'sourceKind': 'meeting_todo', 'originId': todo['origin_id'], 'todoKey': todo['id'],
            'title': todo['action'], 'status': 'pending', 'sessionId': meeting.client_id,
            'side': 'ours' if '我方' in words else 'theirs' if '对方' in words else 'unknown',
            'taskIntent': todo.get('task_intent'), 'taskDeliverable': todo.get('deliverable_quote'),
            'taskSourceText': words, 'notes': words, 'createdAt': meeting.content['started_at'],
            'deadline': deadline_from_text(todo.get('deadline_text'), meeting.content['started_at']),
            'evidenceIds': todo['evidence_ids'], 'evidenceHashes': hashes,
            'attachments': [], 'customerAcceptance': 'pending', 'reviewPending': preview, 'reviewRejected': False,
            'authorizationStatus': 'organize_only'}
        if row:
            row.content = {**row.content, **content}
            row.version += 1
        else:
            session.add(InternalTask(id=key, owner_id=meeting.owner_id, meeting_id=meeting.id, version=1, content=content))
    if not preview:
        pending = await session.scalars(select(InternalTask).where(InternalTask.owner_id == meeting.owner_id, InternalTask.meeting_id == meeting.id))
        for row in pending:
            if (row.content.get('sourceKind') == 'meeting_todo' and row.content.get('reviewPending')
                and row.content.get('originId') in reviewed_sources and row.id not in accepted):
                row.content = {**row.content, 'reviewPending': False, 'reviewRejected': True}
                row.version += 1


async def complete_checklist(session, owner, checklist_id, version):
    if checklist_id.startswith('minutes:'):
        from app.modules.meetings.services.minutes_actions import complete_minutes_action
        return await complete_minutes_action(session, owner, checklist_id, version)
    kind, _, item_id = checklist_id.partition(':')
    if kind not in {'commitment', 'task'} or not item_id:
        raise HTTPException(404, 'checklist_task_not_found')
    # 与实时写入保持会议锁 → 账户锁顺序，避免确认期间原话被替换。
    from app.modules.meetings.models.meeting import Meeting
    model = ProgressItem if kind == 'commitment' else InternalTask
    meeting_id = await session.scalar(select(model.meeting_id).where(model.id == item_id, model.owner_id == owner))
    if meeting_id:
        await session.scalar(select(Meeting).where(Meeting.id == meeting_id, Meeting.owner_id == owner).with_for_update(read=True))
    # 与实时投影同一个账户锁，随后检查版本及当前原话。
    service = ProgressService(session)
    await service._lock_owner(owner)
    if kind == 'commitment':
        row = await session.scalar(select(ProgressItem).where(
            ProgressItem.id == item_id, ProgressItem.owner_id == owner,
            ProgressItem.kind == 'commitment').with_for_update())
        if not row:
            raise HTTPException(404, 'checklist_task_not_found')
        value = {**output(row), 'kind': 'commitment',
                 'evidenceCurrent': await service.source_current(row)}
    else:
        row = await session.scalar(select(InternalTask).where(
            InternalTask.id == item_id, InternalTask.owner_id == owner).with_for_update())
        if not row:
            raise HTTPException(404, 'checklist_task_not_found')
        value = {**TaskService.output(row), 'kind': 'internal_task',
                 'sourceCurrent': await source_current(session, owner, row)}
    item = checklist_item(value, include_expired=True)
    if item is None:
        raise HTTPException(409, 'checklist_source_changed')
    if item['completed']:
        return item  # 超时后重试不重复写历史。
    if expired(value):
        raise HTTPException(409, 'checklist_task_expired')
    if row.version != version:
        raise HTTPException(409, 'checklist_version_changed')
    if value.get('reviewPending'):
        raise HTTPException(409, 'checklist_review_pending')
    change = {'checklistCompleted': True, 'checklistFingerprint': fingerprint(value),
              'checklistCompletedAt': datetime.now(timezone.utc).isoformat()}
    if kind == 'commitment':
        row.overrides = {**row.overrides, **change}
        row.version += 1
        row.history = [*row.history, {'actorUserId': owner, 'at': change['checklistCompletedAt'],
            'version': row.version, 'description': '本人勾选会议任务已处理；不代替履约核验', 'changes': change}]
    else:
        TaskService(session).record(row, change, '本人勾选会议任务已处理；不代替履约核验', 'account_owner')
    await session.commit()
    return {**item, 'completed': True, 'version': row.version}
