"""A single persisted completion state for minutes actions and the home checklist."""
import hashlib
import json
from datetime import datetime, timezone
from urllib.parse import quote
from fastapi import HTTPException
from sqlalchemy import select
from app.modules.meetings.models.meeting import Meeting
from app.modules.meetings.models.archive import MeetingArchive
from app.modules.meetings.services.minutes_details import validate_details, legacy_details


def action_details(meeting, archive):
    details = (validate_details(archive.details, meeting.content['utterances']) if archive.details
               else legacy_details(archive.markdown, meeting.content['utterances']))
    valid = (not meeting.deleted and meeting.content.get('ended')
             and meeting.content.get('elapsed_ms', 0) >= 60000 and archive.revision == meeting.revision)
    sources = {u['id']: u for u in meeting.content['utterances']}
    completions = (archive.details or {}).get('_action_completions', {})
    versions = (archive.details or {}).get('_action_versions', {})
    actions = []
    seen = set()
    from app.modules.meetings.services.action_deadline import resolve_action_deadline
    for action in details['actions']:
        deadline = resolve_action_deadline(action.get('deadline'), meeting.content['started_at'])
        action = {**action, 'deadlineAt': deadline['at'] if deadline else None,
                  'deadlineHasTime': deadline['hasTime'] if deadline else False}
        evidence = [(key, sources[key]['raw'], sources[key].get('correction')) for key in sorted(action['evidence_ids'])]
        key = hashlib.sha256(json.dumps([action['text'].strip(), evidence], ensure_ascii=False).encode()).hexdigest()[:32]
        if key in seen:
            continue
        seen.add(key)
        actions.append({**action, 'id': key, 'actionKey': key, 'completed': bool(completions.get(key)),
                        'taskId': f'minutes:{meeting.client_id}:{key}' if valid else None,
                        'taskVersion': (archive.revision + int(versions.get(key, 0)) * 1000000000) if valid else None})
    return {**details, 'actions': actions}


def checklist_entry(meeting, archive, action):
    return {'id': action['taskId'], 'title': action['text'], 'version': action['taskVersion'],
            'deadline': action.get('deadlineAt'), 'deadlineHasTime': action.get('deadlineHasTime', False),
            'deadlineText': action.get('deadline'),
            'createdAt': meeting.content['started_at'],
            'sourceMeetingDate': datetime.fromisoformat(meeting.content['started_at'].replace('Z', '+00:00')).date().isoformat(),
            'completed': action['completed'],
            'sessionId': meeting.client_id, 'actionId': action['actionKey'],
            'source': '会议纪要 · ' + meeting.content['title'], 'notes': '', 'reviewPending': False,
            'sourceRoute': f'/meetings/{quote(meeting.client_id, safe="")}/minutes?action={action["actionKey"]}'}


async def list_minutes_tasks(session, owner, *, now=None, include_expired=False):
    from app.modules.home.services.task_checklist import expired
    rows = await session.execute(select(Meeting, MeetingArchive).join(MeetingArchive, MeetingArchive.meeting_id == Meeting.id)
        .where(Meeting.owner_id == owner, MeetingArchive.owner_id == owner, Meeting.deleted.is_(False))
        .order_by(Meeting.updated_at.desc()))
    tasks = []
    for meeting, archive in rows:
        for action in action_details(meeting, archive)['actions']:
            if not action['taskId']:
                continue
            task = checklist_entry(meeting, archive, action)
            if include_expired or not expired(task, now):
                tasks.append(task)
    return tasks


async def complete_minutes_action(session, owner, task_id, version, *, completed=True):
    from app.modules.home.services.task_checklist import expired
    _, _, rest = task_id.partition(':')
    client_id, _, key = rest.rpartition(':')
    meeting = await session.scalar(select(Meeting).where(Meeting.owner_id == owner,
        Meeting.client_id == client_id, Meeting.deleted.is_(False)).with_for_update())
    if not meeting:
        raise HTTPException(404, 'checklist_task_not_found')
    archive = await session.scalar(select(MeetingArchive).where(MeetingArchive.owner_id == owner,
        MeetingArchive.meeting_id == meeting.id).with_for_update())
    if not archive:
        raise HTTPException(404, 'checklist_task_not_found')
    action = next((a for a in action_details(meeting, archive)['actions'] if a['actionKey'] == key and a['taskId']), None)
    if not action:
        raise HTTPException(409, 'checklist_source_changed')
    item = checklist_entry(meeting, archive, action)
    if item['completed'] == completed:
        return item
    if action['taskVersion'] != version:
        raise HTTPException(409, 'checklist_version_changed')
    if completed and expired(item):
        raise HTTPException(409, 'checklist_task_expired')
    details = archive.details or legacy_details(archive.markdown, meeting.content['utterances'])
    completions = dict(details.get('_action_completions', {}))
    if completed:
        completions[key] = {'at': datetime.now(timezone.utc).isoformat(), 'actorUserId': owner}
    else:
        completions.pop(key, None)
    versions = dict(details.get('_action_versions', {}))
    versions[key] = int(versions.get(key, 0)) + 1
    archive.details = {**details, '_action_completions': completions, '_action_versions': versions}
    await session.commit()
    return {**item, 'completed': completed, 'version': archive.revision + versions[key] * 1000000000}
