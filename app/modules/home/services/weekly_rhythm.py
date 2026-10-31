"""Read-only seven-day projection; never calls a model or invents sample counts."""
from datetime import datetime, timedelta, timezone
import re


def weekly_rhythm(meetings, archives, progress, tasks, *, now=None):
    now = now or datetime.now(timezone(timedelta(hours=8)))
    today = now.date()
    days = [{'date': (today - timedelta(days=6-i)).isoformat(), 'meetings': 0,
             'decisions': 0, 'activeGoals': 0, 'missingItems': 0, 'pendingActions': 0} for i in range(7)]
    by_date = {d['date']: d for d in days}
    source_dates = {}
    for meeting in meetings:
        content = meeting.content
        date = str(content.get('started_at', ''))[:10]
        if date not in by_date:
            continue
        source_dates[meeting.client_id] = date
        if not content.get('ended') or not any(u.get('raw', '').strip() and not u.get('withdrawn') for u in content.get('utterances', [])):
            continue
        by_date[date]['meetings'] += 1
        archive = archives.get(meeting.id)
        if not archive or archive.revision != meeting.revision:
            continue
        from app.modules.meetings.services.minutes_details import validate_details, legacy_details
        details = validate_details(archive.details, content['utterances']) if archive.details else legacy_details(archive.markdown, content['utterances'])
        seen = {re.sub(r'\s+', '', item['text']) for item in details['decisions'] if item.get('status', 'confirmed') == 'confirmed'}
        by_date[date]['decisions'] += len(seen)
    for item in progress:
        if item.get('kind') == 'goal' and item.get('removed'):
            continue
        if item.get('reviewRejected') or not item.get('evidenceCurrent', True) or item.get('status') in {'withdrawn','completed','verified'}:
            continue
        ids = {item.get('sessionId'), item.get('evidence', {}).get('sessionId')}
        ids.update(s.get('sessionId') for s in item.get('meetingSources', {}).values())
        dates = {source_dates[i] for i in ids if i in source_dates}
        for date in dates:
            if item.get('kind') == 'goal' and item.get('status') == 'active':
                by_date[date]['activeGoals'] += 1
            if item.get('missing') or item.get('reviewPending') or (item.get('kind') in {'goal','commitment'} and not item.get('deadline')):
                by_date[date]['missingItems'] += 1
    for task in tasks:
        if task.get('completed'):
            continue
        date = str(task.get('deadline') or task.get('sourceMeetingDate') or task.get('createdAt') or '')[:10]
        if date in by_date:
            by_date[date]['pendingActions'] += 1
    return {'days': days, 'meetings': sum(d['meetings'] for d in days),
            'scope': 'recent_7_days', 'goalScope': 'current_status_of_goals_linked_to_day_meetings'}
