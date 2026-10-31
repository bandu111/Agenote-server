"""真实会议的中断标记、同源拼接、低延迟通道及权限边界。"""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.modules.meetings.services.live_hints import source_preview, LiveMeetingHints
from app.modules.meetings.services.meeting_agents import analysis_triggers, select_context
from tests.test_meetings import account


def test_merged_short_interruption_without_ellipsis_can_link_resumed_topic_without_inventing_speaker():
    sources = [utterance('mixed', '验收标准我们就按，先插一句明天给测试账号。', at=100),
        utterance('other', '先谈预算，保持十万元。', 'b', 200),
        utterance('resume', '接着刚才的验收标准，我们按双方确认的功能清单验收。', at=300)]
    original = deepcopy(sources)
    result = source_preview(sources)
    assert not result['repairs']
    link = next(i for i in result['insights'] if i['kind'] == 'stitch')
    assert link['evidence_ids'] == ['mixed', 'resume']
    assert '未据此确认同一发言人' in link['body']
    assert sources == original


def test_resumption_does_not_link_unrelated_or_withdrawn_topics():
    sources = [utterance('old', '讨论接口权限。'),
        utterance('resume', '接着刚才的预算，我们按十万元执行。', at=100)]
    assert not source_preview(sources)['insights']
    sources[0]['raw'] = '讨论预算。'
    sources[0]['withdrawn'] = True
    assert not source_preview(sources)['insights']


def utterance(id, text, speaker='a', at=0, interrupted=False):
    return {'id': id, 'speaker': '发言人' + speaker, 'speaker_id': speaker,
            'speaker_separated': True, 'raw': text, 'at': at,
            'turn_status': 'interrupted' if interrupted else 'complete'}


@pytest.mark.parametrize('marker', [True, False])
def test_quote_stitch_works_after_topic_switch_without_asr_ellipsis(marker):
    sources = [utterance('old', '验收就按……' if marker else '验收就按。', interrupted=True),
               utterance('other', '先谈预算，预算保持十万元。', 'b', 10000),
               utterance('resume', '验收就按双方确认的功能清单。', at=30000)]
    original = deepcopy(sources)
    result = source_preview(sources)
    assert result['repairs'][0]['text'] == '双方确认的功能清单'
    assert result['repairs'][0]['evidence_ids'] == ['old', 'resume']
    assert result['repairs'][0]['start'] == 4
    assert sources == original


@pytest.mark.parametrize('mode', ['different_speaker', 'unknown', 'withdrawn', 'correction', 'proposal', 'time_reversed'])
def test_no_unjustified_inline_stitch(mode):
    sources = [utterance('old', '验收就按……', interrupted=True),
               utterance('resume', '验收就按双方确认的功能清单。', at=30000)]
    if mode == 'different_speaker': sources[1]['speaker_id'] = 'b'
    if mode == 'unknown': sources[0]['speaker_separated'] = False
    if mode == 'withdrawn': sources[1]['withdrawn'] = True
    if mode == 'correction': sources[1]['correction'] = '只谈预算'
    if mode == 'proposal': sources[1]['raw'] = '考虑验收就按双方确认的功能清单。'
    if mode == 'time_reversed': sources[1]['at'] = -1
    assert not source_preview(sources)['repairs']


def test_old_non_ellipsis_interruption_is_recalled_and_triggers_memory():
    sources = [utterance('gap', '验收就按', interrupted=True)]
    sources += [utterance(str(i), '休息一下。', at=i+1) for i in range(50)]
    assert 'memory' in analysis_triggers(sources[:1])
    assert 'gap' in {u['id'] for u in select_context(sources)}


async def test_immediate_stitch_does_not_wait_for_or_call_model():
    provider = SimpleNamespace(run=AsyncMock(side_effect=RuntimeError('not configured')))
    result = await LiveMeetingHints(provider).analyze('owner', [utterance('a', '验收就按', interrupted=True),
        utterance('b', '验收就按功能清单。', at=30)], include_model=False)
    assert result['repairs'] and result['businessEffect'] == 'none'
    provider.run.assert_not_awaited()


async def test_live_hints_auth_and_no_business_projection(client, app):
    assert (await client.post('/api/v1/meetings/live-hints', json={})).status_code == 401
    headers = await account(client)
    app.state.live_meeting_hints = LiveMeetingHints(SimpleNamespace(run=AsyncMock(return_value={})))
    response = await client.post('/api/v1/meetings/live-hints', headers=headers, json={
        'include_model': False, 'utterances': [utterance('old', '验收就按', interrupted=True),
                                             utterance('new', '验收就按功能清单。', at=30)]})
    assert response.status_code == 200 and response.json()['data']['repairs']
    assert (await client.get('/api/v1/meetings', headers=headers)).json()['data'] == []
    assert not (await client.get('/api/v1/progress', headers=headers)).json()['data']['commitments']
    duplicate = {'utterances': [utterance('same', '你好'), utterance('same', '再见')]}
    assert (await client.post('/api/v1/meetings/live-hints', headers=headers, json=duplicate)).status_code == 422
