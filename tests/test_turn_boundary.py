"""未结束的发言保留证据，但不能越过业务审慎校验。"""
import pytest
from app.modules.meetings.schemas.meeting import Transcript
from app.modules.meetings.services.business_guard import guarded_report


@pytest.mark.parametrize('status', ['pending', 'interrupted'])
def test_unfinished_turn_does_not_create_business_candidates(status):
    source = {'id': 'u1', 'speaker': '发言人 A', 'raw': '本期交付移动端应用', 'at': 0, 'turn_status': status}
    report = {
        'state': [{'value': source['raw'], 'status': 'confirmed', 'evidence_ids': ['u1']}],
        'goals': [{'origin_id': 'u1', 'condition': source['raw'], 'evidence_ids': ['u1']}],
        'commitments': [{'origin_id': 'u1', 'action': '交付移动端应用', 'events': [{'kind': 'proposed', 'evidence_ids': ['u1']}]}],
        'action_intents': [{'title': '进度会', 'evidence_ids': ['u1'], 'status': 'confirmed'}],
        'repairs': [{'target_id': 'u1', 'text': '待核对补全'}],
    }
    result = guarded_report(report, [source])
    for key in ['state', 'goals', 'commitments', 'action_intents']:
        assert result[key] == []
    assert result['repairs'] == report['repairs']
    assert source['raw'] == '本期交付移动端应用'


def test_turn_contract_accepts_new_and_legacy_records():
    raw = {'id': 'u', 'speaker': 'A', 'raw': '我们需要', 'at': 0}
    assert Transcript.model_validate(raw).turn_status == 'complete'
    assert Transcript.model_validate({**raw, 'turnStatus': 'pending', 'turnId': 'turn1'}).turn_id == 'turn1'
    with pytest.raises(ValueError):
        Transcript.model_validate({**raw, 'turnStatus': 'approved'})


async def test_pending_speech_syncs_without_calling_agents(client):
    from tests.test_meetings import account, meeting
    from unittest.mock import AsyncMock
    headers = await account(client, 'turn-boundary@example.com')
    payload = meeting()
    payload['utterances'][0]['turnStatus'] = 'pending'
    response = await client.post('/api/v1/meetings/sync', headers=headers, json=payload)
    assert response.status_code == 200
    response = await client.post('/api/v1/meetings/phone-meeting-1/analysis', headers=headers)
    assert response.status_code == 200
    report = response.json()['data']['analysis']
    assert report['agents_completed'] == []
    assert report['commitments'] == []
    assert report['goals'] == []
