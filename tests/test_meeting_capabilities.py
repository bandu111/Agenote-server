"""认证连接和服务配置状态不能伪装成模型已经完成一次分析。"""
from pydantic import SecretStr
from tests.test_meetings import account


async def test_analysis_capabilities_are_authenticated_and_configuration_only(client, app, tmp_path):
    assert (await client.get('/api/v1/meetings/analysis/capabilities')).status_code == 401
    headers = await account(client)
    app.state.settings.deepseek_api_key = None
    result = (await client.get('/api/v1/meetings/analysis/capabilities', headers=headers)).json()['data']
    assert not result['configured'] and not result['requiresPause'] and not result['modelVerified']
    (tmp_path / 'run_agent.py').write_text('# synthetic runtime', encoding='utf-8')
    app.state.settings.hermes_runtime_path = tmp_path
    app.state.settings.deepseek_api_key = SecretStr('synthetic-config-only')
    result = (await client.get('/api/v1/meetings/analysis/capabilities', headers=headers)).json()['data']
    assert result['configured'] and not result['modelVerified']
    assert {'memory', 'risk', 'forecast', 'commitment', 'verify'} <= set(result['roles'])
    assert 'synthetic-config-only' not in str(result)


async def test_core_preview_returns_before_auxiliary_work_and_cannot_cross_owner(client, app):
    import asyncio
    from tests.test_meetings import meeting
    headers = await account(client)
    await client.post('/api/v1/meetings/sync', headers=headers, json=meeting())
    preview_ready, finish = asyncio.Event(), asyncio.Event()
    async def analyze(utterances, context=None, on_core_ready=None, on_actions_ready=None):
        value = {'insights': [{'id': 'core-risk', 'kind': 'risk', 'title': '范围需要核对', 'body': '新增范围尚未批准', 'evidence_ids': ['u1'], 'inference': True}],
            'repairs': [], 'withdrawals': [], 'state': [], 'agents_completed': ['state', 'risk', 'verify']}
        if on_core_ready:
            await on_core_ready(value)
        preview_ready.set()
        await finish.wait()
        return value
    app.state.meeting_analysis.agents.analyze = analyze
    response = await client.post('/api/v1/meetings/phone-meeting-1/jobs', headers=headers, json={'operation': 'analysis'})
    assert response.status_code == 202
    job_id = response.json()['data']['id']
    try:
        await asyncio.wait_for(preview_ready.wait(), 3)
        job = (await client.get('/api/v1/meetings/jobs/' + job_id, headers=headers)).json()['data']
        assert job['status'] == 'running' and job['result'] is None
        assert job['preview']['kind'] == 'verified_inference_only'
        assert job['preview']['insights'][0]['id'] == 'core-risk'
        assert 'commitments' not in job['preview'] and 'withdrawals' not in job['preview']
        assert not (await client.get('/api/v1/progress', headers=headers)).json()['data']['commitments']
        other = await account(client, 'preview-other@example.com')
        assert (await client.get('/api/v1/meetings/jobs/' + job_id, headers=other)).status_code == 404
    finally:
        finish.set()
        await asyncio.gather(*list(app.state.meeting_jobs.tasks))
    job = (await client.get('/api/v1/meetings/jobs/' + job_id, headers=headers)).json()['data']
    assert job['status'] == 'succeeded'
