import asyncio
import pytest
from app.modules.meetings.services.turn_end_service import TurnEndService


class Provider:
    calls = 0
    answer = {'status': 'continue', 'confidence': .95}
    async def run(self, role, system, payload):
        assert role == 'turn_end'
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.mark.asyncio
async def test_continue_complete_uncertain_and_invalid():
    provider = Provider()
    service = TurnEndService(provider)
    for answer, expected in [
        ({'status': 'continue', 'confidence': .95}, 'continue'),
        ({'status': 'complete', 'confidence': .95}, 'complete'),
        ({'status': 'complete', 'confidence': .4}, 'uncertain'),
        ({'status': 'complete', 'confidence': .86}, 'uncertain'),
        ({'status': 'complete', 'confidence': 2}, 'uncertain'),
        ({'status': 'complete', 'confidence': .99, 'task': 'execute'}, 'uncertain'),
        (RuntimeError('unavailable'), 'uncertain'),
    ]:
        provider.answer = answer
        assert (await service.classify('a', '会议片段'))['status'] == expected
        assert not service.owners


@pytest.mark.asyncio
async def test_busy_owner_does_not_duplicate_model_calls():
    entered, release = asyncio.Event(), asyncio.Event()
    class Slow(Provider):
        async def run(self, *args):
            entered.set()
            await release.wait()
            return self.answer
    service = TurnEndService(Slow())
    task = asyncio.create_task(service.classify('a', '原话'))
    await entered.wait()
    assert (await service.classify('a', '新原话'))['status'] == 'uncertain'
    release.set()
    assert (await task)['status'] == 'continue'
    assert not service.owners


@pytest.mark.asyncio
async def test_endpoint_requires_auth_and_only_returns_readonly_classification(client, app):
    provider = Provider()
    app.state.turn_end_service = TurnEndService(provider)
    assert (await client.post('/api/v1/meetings/turn-end', json={'text': '测试'})).status_code == 401
    account = await client.post('/api/v1/auth/register', json={
        'name': '语义收尾测试', 'email': 'turn-only@example.com', 'password': 'synthetic-Password-2026'})
    headers = {'Authorization': 'Bearer ' + account.json()['data']['access_token']}
    response = await client.post('/api/v1/meetings/turn-end', headers=headers,
        json={'text': '今天我们讨论三个事情，第一个是试点情况。'})
    assert response.status_code == 200
    assert response.json()['data'] == {'status': 'continue', 'confidence': .95}
    assert (await client.post('/api/v1/meetings/turn-end', headers=headers,
        json={'text': 'x' * 3001})).status_code == 422
