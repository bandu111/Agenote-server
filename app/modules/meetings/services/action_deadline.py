"""Parse evidence-backed action deadlines once, anchored to the meeting date."""
import re
from datetime import datetime, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8))


def resolve_action_deadline(text, started_at):
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        anchor = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
        if anchor.tzinfo is None:
            return None
        anchor = anchor.astimezone(SHANGHAI)
        value = text.strip()
        # Fully normalized timestamps are already absolute, never relative to today.
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})', value):
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return {'at': parsed.isoformat(), 'hasTime': True}
        value = re.sub(r'(?:之前|以前|前)$', '', value).strip()
        digits = '零一二三四五六七八九'
        def number(m):
            word = m[1].replace('两', '二')
            if '十' in word:
                tens, _, units = word.partition('十')
                n = (digits.index(tens) if tens else 1) * 10 + (digits.index(units) if units else 0)
            else:
                n = digits.index(word)
            return str(n) + m[2]
        value = re.sub(r'([零一二两三四五六七八九十]{1,3})(点|时|分)', number, value)
        date_pattern = r'(?:(\d{4})[-/年])?(\d{1,2})[-/月](\d{1,2})日?'
        match = re.fullmatch(r'(今天|明天|后天|大后天|(?:本|这|下)?(?:周|星期)[一二三四五六日天]|'+date_pattern+r')(?P<clock>.*)', value)
        if not match:
            return None
        date, year, month, day = match.group(1, 2, 3, 4)
        relative = {'今天': 0, '明天': 1, '后天': 2, '大后天': 3}
        if date in relative:
            base = anchor + timedelta(days=relative[date])
        elif month is not None:
            base = datetime(int(year or anchor.year), int(month), int(day), tzinfo=SHANGHAI)
        else:
            week = re.fullmatch(r'(本|这|下)?(?:周|星期)([一二三四五六日天])', date)
            index = '一二三四五六日'.index('日' if week[2] == '天' else week[2])
            delta = index - anchor.weekday() + (7 if week[1] == '下' else 0)
            if week[1] is None and delta < 0:
                delta += 7
            base = anchor + timedelta(days=delta)
        clock = match['clock'].strip()
        if not clock:
            return {'at': base.replace(hour=23, minute=59, second=0, microsecond=0).isoformat(), 'hasTime': False}
        clock = re.fullmatch(r'(?:的\s*)?(凌晨|早上|上午|中午|下午|晚上)?\s*(\d{1,2})(?:(?:点|时)(?:(\d{1,2})分?|(半))?|:([0-5]\d))', clock)
        if not clock:
            return None
        period, hour, minute, half, colon = clock.groups()
        hour, minute = int(hour), 30 if half else int(minute or colon or 0)
        if period in {'下午', '晚上'} and 1 <= hour < 12:
            hour += 12
        if period == '中午' and 1 <= hour < 11:
            hour += 12
        if period == '凌晨' and hour == 12:
            hour = 0
        return {'at': base.replace(hour=hour, minute=minute, second=0, microsecond=0).isoformat(), 'hasTime': True}
    except (ValueError, TypeError, AttributeError):
        return None
