"""按明确目标引用续接会议进展；会议陈述与履约核验分开，不按标题相似度合并。"""
import re
from copy import deepcopy
from datetime import datetime, timezone


def ongoing(value, now=None, *, include_completed=False):
    if value.get('removed'):
        return False
    if value.get('status') in {'withdrawn', 'paused'} or (
            value.get('status') == 'completed' and not include_completed):
        return False
    deadline = value.get('deadline')
    if not deadline:
        return True
    try:
        end = datetime.fromisoformat(deadline)
        if end.tzinfo is None:
            return False
        return end > (now or datetime.now(timezone.utc))
    except (ValueError, TypeError):
        return False


def sessions(row):
    return {row.content.get('sessionId'), *(
        s.get('sessionId') for s in row.content.get('meetingSources', {}).values())} - {None, ''}


def mentions(value, words):
    from app.modules.progress.services.goal_evidence import certain_clause
    # 明确复述交付条件，或明确表示继续原具名目标；通用“项目”“测试”不能作为身份。
    condition = value.get('condition', '')
    title = value.get('title', '')
    return any(
        (len(condition) >= 6 and certain_clause(w, condition)) or
        (len(title) >= 6 and certain_clause(w, title) and
         re.search(r'继续|接着|上次|原定|同一|复盘|推进|目标', w))
        for w in words)


def clauses(source):
    return re.split(r'[，,。；;！？?\n]', source.get('correction') or source['raw'])


def milestone_title(title):
    """分离模型附在步骤名后的进度陈述，不用语义相似度猜节点身份。"""
    return re.sub(r'(?:已经完成|已完成|正在进行|进行中|尚未开始|还没开始|尚未完成|还没完成)$', '', title).strip()


def reported_milestones(milestones, sources, session_id=None):
    """只更新已存在、可逐字定位的节点，不把计划和否定句当作已完成。"""
    result = deepcopy(milestones)
    for milestone in result:
        if milestone.get('statusBasis') == 'meeting_reported' and milestone.get('evidenceSessionId') == session_id:
            milestone['status'] = 'pending'
            for field in ['statusBasis', 'evidenceIds', 'evidenceSessionId', 'quote']:
                milestone.pop(field, None)
        title = milestone['title']
        if len(title) < 4:
            continue
        for source in sources:
            if source.get('withdrawn') or source.get('turn_status', 'complete') != 'complete':
                continue
            for clause in clauses(source):
                comparable = re.sub(r'已经|尚未|正在|还没|仍在|已', '', clause)
                if title not in clause and title not in comparable:
                    continue
                if re.search(r'如果|假如|假设|计划|准备|希望|预计|争取|可能|建议|是否|吗|若', clause):
                    continue
                # “不能确认已完成”与否定的执行命令都不产生肯定进度。
                if re.search(r'不能确认|无法确认|尚待确认|不要|不再|取消|撤回|并非|不是|不算|不代表|标为|显示为|改成|记作|标记为', clause):
                    continue
                if any(other['title'] != title and other['title'] in clause for other in result) and not re.search(r'均|都|全部', clause):
                    continue
                state = None
                if re.search(r'尚未|还没|没有|未完成|待完成|待开始|尚未开始', clause):
                    state = 'pending'
                elif re.search(r'已(?:经)?(?:开发|测试|部署|联调)?完成|已(?:经)?(?:通过|结束|修复|解决|交付)|完成了|已经做完', clause) or (
                        title.endswith('确认') and re.search(r'已(?:经)?确认', clause)):
                    state = 'completed'
                elif re.search(r'正在|进行中|推进中|开发中|处理中|测试中|开始了|已开始', clause):
                    state = 'active'
                if state:
                    milestone.update(status=state, statusBasis='meeting_reported',
                        evidenceIds=[source['id']], evidenceSessionId=session_id, quote=clause.strip())
    return result


def explicit_steps(words):
    """模型未列节点时，仅从明确里程碑清单回收原文，不生成行业模板。"""
    result = []
    for word in words:
        for text in re.findall(r'里程碑(?:包括|是|为|有|：|:)?([^。！？?\n]+)', word):
            for part in re.split(r'[、，,；;]|以及', text):
                title = re.sub(r'^[一二三四五六七八九十\d]+[\.．、：:]?', '', part.strip())
                if 4 <= len(title) <= 40 and title not in result:
                    result.append(title)
    return result[:20]


def contribution(meeting, origin_ids, ids):
    from app.modules.meetings.services.business_guard import transcript_hashes
    hashes = transcript_hashes(meeting.content['utterances'])
    return {'sessionId': meeting.client_id, 'revision': meeting.revision,
        'originIds': list(dict.fromkeys(origin_ids)),
        'hashes': {i: hashes[i] for i in ids if i in hashes}}


async def attach(service, row, meeting, *, incoming=None, origin=None):
    """保留规范目标身份和旧节点；新的会议不能悄悄改旧交期或验收要求。"""
    old = row.content
    merged = {**old}
    if incoming:
        if row.source_key == f'goal:{meeting.id}:{origin}':
            for field in ['title', 'condition', 'sourceEvidenceHashes', 'evidenceIds', 'reviewPending', 'reviewRejected']:
                if field in incoming:
                    merged[field] = incoming[field]
        for field in ['deadline', 'deadlineText', 'acceptance']:
            if not merged.get(field) or merged[field] == '待明确':
                merged[field] = incoming.get(field)
        if incoming.get('phase') and incoming['phase'] != '待明确':
            merged['phase'] = incoming['phase']
        nodes = {}
        for m in [*old.get('milestones', []), *incoming.get('milestones', [])]:
            title = milestone_title(m['title'])
            if title and title not in nodes:
                nodes[title] = {**m, 'title': title}
        merged['milestones'] = list(nodes.values())
        if row.meeting_id == meeting.id:
            # 同场重分析可更新来源状态，但不清空上一轮没有重新返回的有效节点。
            merged.update(reviewPending=incoming.get('reviewPending', False), reviewRejected=False)
    entries = deepcopy(old.get('meetingSources', {}))
    previous = entries.get(meeting.id, {})
    sources = meeting.content['utterances']
    hints = getattr(service, '_meeting_goal_hints', [])
    if len({g.get('condition') for g in hints}) > 1:
        sources = [u for u in sources if mentions(merged, [u.get('correction') or u['raw']])]
    merged['milestones'] = reported_milestones(merged.get('milestones', []), sources, meeting.client_id)
    ids = set((incoming or {}).get('evidenceIds', [])) | set(previous.get('hashes', {}))
    if not ids:
        anchor = next((u for u in sources if not u.get('withdrawn')
            and u.get('turn_status', 'complete') == 'complete'
            and mentions(merged, [u.get('correction') or u['raw']])), None)
        if anchor:
            ids.add(anchor['id'])
    ids.update(i for m in merged['milestones'] if m.get('statusBasis') == 'meeting_reported'
        and m.get('evidenceSessionId') == meeting.client_id
        for i in m.get('evidenceIds', []) if any(u['id'] == i for u in sources))
    origins = [*previous.get('originIds', []), *([origin] if origin else [])]
    entries[meeting.id] = contribution(meeting, origins, ids)
    merged['meetingSources'] = entries
    merged['meetingOrder'] = list(dict.fromkeys([
        *old.get('meetingOrder', [row.meeting_id] if row.meeting_id else []), meeting.id]))
    merged['lastSessionId'] = meeting.client_id
    # 已核验投影在后续 refresh 中重新计算，不能把旧待完成快照盖在新口头进展上。
    if merged.get('milestones') != old.get('milestones'):
        merged.pop('milestoneProjection', None)
    if merged != old:
        row.content = merged
        def business_value(value):
            value = deepcopy(value)
            for source in value.get('meetingSources', {}).values():
                source.pop('revision', None)
            return value
        # 仅同步游标变化不使补全版本失效，也不反复追加进展历史。
        if business_value(merged) != business_value(old):
            row.version += 1
            row.history = [*row.history, {'at': datetime.now(timezone.utc).isoformat(),
                'description': '按明确目标引用续接会议进展；会议陈述仍待核验',
                'version': row.version, 'sessionId': meeting.client_id}]
    return row


async def upsert_goal(service, meeting, origin, content, revision, preview):
    from sqlalchemy import select
    from app.modules.progress.models.progress_item import ProgressItem
    from app.modules.progress.services.progress_service import output
    rows = list(await service.session.scalars(select(ProgressItem).where(
        ProgressItem.owner_id == meeting.owner_id, ProgressItem.kind == 'goal')))
    exact = [r for r in rows if r.source_key == f'goal:{meeting.id}:{origin}' or
        origin in r.content.get('meetingSources', {}).get(meeting.id, {}).get('originIds', [])]
    words = [u.get('correction') or u['raw'] for u in meeting.content['utterances']
        if u['id'] in content.get('evidenceIds', []) and not u.get('withdrawn')]
    matches = exact or [r for r in rows if ongoing(output(r)) and not r.content.get('reviewRejected')
        and await service.source_current(r) and mentions(output(r), words)]
    if len(matches) == 1:
        row = matches[0]
        previous = row.content.get('meetingSources', {}).get(meeting.id, {})
        if preview or previous.get('revision', -1) > revision:
            return row
        row = await attach(service, row, meeting, incoming=content, origin=origin)
        if row.meeting_id == meeting.id:
            row.source_revision = max(row.source_revision, revision)
        return row
    row = await service._upsert(meeting, 'goal', origin, content, revision=revision, preview=preview)
    if not preview:
        await attach(service, row, meeting, incoming=content, origin=origin)
    return row


async def continue_explicit_goals(service, meeting):
    from sqlalchemy import select
    from app.modules.progress.models.progress_item import ProgressItem
    from app.modules.progress.services.progress_service import output
    sources = [u for u in meeting.content['utterances'] if not u.get('withdrawn')
        and u.get('turn_status', 'complete') == 'complete']
    words = [u.get('correction') or u['raw'] for u in sources]
    rows = list(await service.session.scalars(select(ProgressItem).where(
        ProgressItem.owner_id == meeting.owner_id, ProgressItem.kind == 'goal')))
    matches = [r for r in rows if ongoing(output(r)) and not r.content.get('reviewPending')
        and not r.content.get('reviewRejected') and await service.source_current(r)
        and mentions(output(r), words)]
    # 多目标会议不把后面的无归属状态套给每一个目标。
    if len(matches) == 1:
        await attach(service, matches[0], meeting)
