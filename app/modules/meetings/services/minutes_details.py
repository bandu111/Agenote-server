"""Evidence-bound page data; old Markdown remains readable without another model call."""
import re


def legacy_details(markdown, utterances):
    result = {'summary': '', 'topics': [], 'decisions': [], 'actions': [], 'open_questions': []}
    section = None
    for line in markdown.splitlines():
        if line.startswith('## '):
            section = {'当前共识': 'decisions', '讨论中（未形成共识）': 'discussed', '后续行动（未自动执行）': 'actions', '待确认': 'open_questions'}.get(line[3:].strip())
        match = re.match(r'^- (.+)〔来源：(.+)〕$', line)
        if match and section:
            target = 'decisions' if section == 'discussed' else section
            result[target].append({'text': match[1], 'evidence_ids': [x.strip() for x in match[2].split(',')],
                                   **({'status': 'discussed' if section == 'discussed' else 'confirmed'} if target == 'decisions' else {})})
    return validate_details(result, utterances)


def validate_details(value, utterances):
    sources = {u['id']: u for u in utterances if not u.get('withdrawn')}
    result = {'schema': 1, 'summary': value.get('summary', ''), 'topics': [],
              'decisions': [], 'actions': [], 'open_questions': []}
    for kind in ['topics', 'decisions', 'actions', 'open_questions']:
        for index, item in enumerate(value.get(kind, [])):
            ids = item.get('evidence_ids', [])
            if not ids or any(i not in sources for i in ids):
                continue
            if kind in {'decisions', 'actions'} and any(sources[i].get('turn_status', 'complete') != 'complete' for i in ids):
                continue
            if kind == 'actions':
                text = ' '.join((sources[i].get('correction') or sources[i]['raw']) for i in ids)
                item = {**item, **{field: item.get(field) if item.get(field) and item[field] in text else None for field in ('owner', 'deadline')}}
            result[kind].append({**item, 'id': f'{kind}-{index}',
                                 'at': min(sources[i]['at'] for i in ids)})
    # Never manufacture an overview from unanswered questions or speculative repairs.
    if not result['summary']:
        result['summary'] = '；'.join(i['text'] for i in result['decisions'][:3])
    if not result['topics']:
        result['topics'] = [dict(i, title=i['text'][:32], points=[]) for i in result['decisions']]
    return result


def enrich_actions(details, progress, meeting_id):
    """Only bind real tasks from this meeting with the same source and action text."""
    from app.modules.home.services.task_checklist import checklist_item
    result = {**details, 'actions': []}
    for action in details.get('actions', []):
        row = {**action, 'completed': False}
        for item in progress:
            session_id = item.get('evidence', {}).get('sessionId') or item.get('sessionId')
            ids = set(item.get('evidenceIds', [])) | set(item.get('evidenceHashes', {}))
            origin = item.get('originId') or item.get('evidence', {}).get('id')
            if origin:
                ids.add(origin)
            if session_id != meeting_id or not ids.intersection(action['evidence_ids']):
                continue
            title = item.get('title', '').strip().rstrip('。')
            text = action['text'].strip().rstrip('。')
            if not title or not (title in text or text in title):
                continue
            task = checklist_item(item, include_expired=True)
            if task:
                row.update(taskId=task['id'], taskVersion=task['version'], completed=task['completed'],
                           owner=item.get('ownerName') or item.get('responsible') or action.get('owner'), deadline=task.get('deadline') or action.get('deadline'))
                break
        result['actions'].append(row)
    return result
