"""只提前发布完整、可校验的单项；不把半个业务 JSON 当作正式决定。"""
from contextlib import aclosing
from app.modules.meetings.schemas.meeting import StateFact, GoalFact, Withdrawal
from app.modules.meetings.services.commitment_agent import CommitmentCandidate
from app.modules.meetings.services.action_planner import CalendarIntent
from app.modules.execution.schemas import ToolPlan
from app.modules.meetings.services.streaming_insights import _streaming_array


def complete_items(text):
    result = {}
    for field, key, schema, limit in [('state', 'value', StateFact, 2),
        ('goals', 'condition', GoalFact, 2), ('commitments', 'action', CommitmentCandidate, 4),
        ('action_intents', 'title', CalendarIntent, 2), ('tool_plans', 'title', ToolPlan, 2),
        ('withdrawals', 'reason', Withdrawal, 8)]:
        values = []
        for candidate, partial in _streaming_array(text, field, key, limit):
            if partial: continue
            try: values.append(schema.model_validate(candidate).model_dump(mode='json'))
            except ValueError: pass
        if values: result[field] = values
    return result


async def extraction(provider, prompt, payload):
    text, previous = '', None
    async with aclosing(provider.stream('business_realtime', prompt, payload)) as events:
        async for event in events:
            if event['type'] == 'delta':
                text += event['text']
                if len(text) > 48000: raise ValueError('business_stream_too_large')
                items = complete_items(text)
                if items and items != previous:
                    previous = items
                    yield {'type': 'partial', 'value': items}
            elif event['type'] == 'done':
                yield event
                return
    raise ValueError('realtime_model_incomplete')
