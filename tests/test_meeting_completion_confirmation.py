"""会议内明确确认：缺失信息与承诺授权原子保存，隔离账户与源版本。"""
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select
from test_meetings import account
from test_progress import meeting, report
from test_progress import projected
from app.modules.progress.models.progress_item import ProgressItem


async def pending(client, app, headers):
    value = report()
    value['goals'] = []
    value['commitments'][0].update(side='unknown', deadline_text=None,
        missing=['履约方待明确', '交付时间待明确', '是否承诺待确认'])
    app.state.meeting_analysis.agents.analyze = AsyncMock(return_value=value)
    payload = meeting()
    payload['utterances'][1]['raw'] = '承诺提交报告，履约方和交期稍后确认。'
    await client.post('/api/v1/meetings/sync', headers=headers, json=payload)
    response = await client.post('/api/v1/meetings/real-progress-session/analysis', headers=headers)
    assert response.status_code == 200
    return (await client.get('/api/v1/progress', headers=headers)).json()['data']['pending'][0]


async def test_confirm_completion_is_atomic_authorized_persisted_and_not_fulfilled(client, app):
    headers = await account(client)
    item = await pending(client, app, headers)
    path = f"/api/v1/progress/{item['id']}/complete"
    payload = {'version': item['version'], 'side': 'theirs',
        'deadline': '2026-11-01T23:59:59+08:00', 'confirm': True}
    response = await client.post(path, headers=headers, json=payload)
    assert response.status_code == 200, response.text
    saved = response.json()['data']
    assert saved['version'] == item['version'] + 1 and len(saved['history']) == 1
    assert saved['side'] == 'theirs' and saved['status'] == 'planned'
    assert saved['authorizationStatus'] == 'owner_confirmed'
    assert not {'履约方待明确', '交付时间待明确', '是否承诺待确认'} & set(saved['missing'])
    assert saved['evidence'] == item['evidence']
    snapshot = (await client.get('/api/v1/progress', headers=headers)).json()['data']
    assert not snapshot['pending'] and snapshot['commitments'][0]['status'] == 'planned'
    assert (await client.post(path, headers=headers, json=payload)).status_code == 409
    other = await account(client, 'other-confirm@example.com')
    assert (await client.post(path, headers=other, json=payload)).status_code == 404


async def test_confirm_without_both_fields_never_partially_writes_side(client, app):
    headers = await account(client)
    item = await pending(client, app, headers)
    result = await client.post(f"/api/v1/progress/{item['id']}/complete", headers=headers,
        json={'version': item['version'], 'side': 'ours', 'confirm': True})
    assert result.status_code == 422
    after = (await client.get('/api/v1/progress', headers=headers)).json()['data']['pending'][0]
    assert after['version'] == item['version'] and after['side'] == 'unknown'
    assert after['status'] == 'pending' and not after['history']


async def test_confirm_already_recorded_side_and_deadline_does_not_change_them(client, app):
    headers = await account(client)
    await projected(client, app, headers)
    before = (await client.get('/api/v1/progress', headers=headers)).json()['data']['commitments'][0]
    result = await client.post(f"/api/v1/progress/{before['id']}/complete", headers=headers,
        json={'version': before['version'], 'side': before['side'], 'confirm': True})
    assert result.status_code == 200
    after = result.json()['data']
    assert after['status'] == 'planned' and after['deadline'] == before['deadline']
    assert after['side'] == before['side'] and after['authorizationStatus'] == 'owner_confirmed'


@pytest.mark.parametrize('reason', ['review', 'source_changed'])
async def test_confirmation_does_not_bypass_review_or_source_checks(client, app, reason):
    headers = await account(client)
    item = await pending(client, app, headers)
    if reason == 'review':
        async with app.state.database.sessions() as session:
            row = await session.scalar(select(ProgressItem).where(ProgressItem.id == item['id']))
            row.content = {**row.content, 'reviewPending': True}
            await session.commit()
    else:
        changed = meeting(2)
        changed['utterances'][1]['raw'] = '承诺提交报告，履约方和交期稍后确认。'
        changed['utterances'][1]['correction'] = '这件事先不要承诺。'
        synced = await client.post('/api/v1/meetings/sync', headers=headers, json=changed)
        assert synced.status_code == 200
    result = await client.post(f"/api/v1/progress/{item['id']}/complete", headers=headers,
        json={'version': item['version'], 'side': 'ours',
            'deadline': '2026-11-01T23:59:59+08:00', 'confirm': True})
    assert result.status_code == 409
