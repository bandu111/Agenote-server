from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select
from app.core.database.mixins import now
from app.modules.outcomes.wechat_models import WechatBinding
from app.modules.outcomes.wechat_service import cipher_for
from app.modules.outcomes.wechat_jobs import WechatJobs
from app.modules.outcomes.models import OutcomeRun
from app.modules.identity.models.identity import User
from tests.test_auth import register,bearer
import pytest
from app.modules.meetings.models.meeting import Meeting
from app.modules.outcomes.weixin_transport import ChannelError

def transport():
    return SimpleNamespace(qr=AsyncMock(return_value={'qrcode':'qr-owner-session',
        'qrcode_img_content':'https://liteapp.weixin.qq.com/mock-test-only'}),
        poll_qr=AsyncMock(return_value={'status':'confirmed','ilink_bot_id':'test-bot',
            'bot_token':'test-secret','ilink_user_id':'verified-peer'}),
        updates=AsyncMock(return_value={'msgs':[]}), send=AsyncMock(return_value={}),close=AsyncMock())

async def age(session,owner):
    row=await session.get(WechatBinding,owner)
    row.updated_at=now()-timedelta(seconds=3);await session.commit()

async def test_independent_qr_confirmation_and_unbind(client,app):
    first=await register(client);second=await register(client,'second@example.com')
    app.state.settings.wechat_binding_key=SecretStr(Fernet.generate_key().decode())
    fake=transport();app.state.wechat_transport=fake
    h=bearer(first);h2=bearer(second)
    created=await client.post('/api/v1/assistant/wechat/qr',headers=h,json={})
    assert created.status_code==200
    assert created.json()['data']['bindingStatus']=='pending'
    # Repeated screen loads do not obtain another QR or overwrite its nonce.
    await client.post('/api/v1/assistant/wechat/qr',headers=h,json={})
    assert fake.qr.await_count==1
    async with app.state.database.sessions() as session:
        owner=await session.scalar(select(User.id).where(User.email=='yan@example.com'))
        await age(session,owner)
    worker=WechatJobs(app.state.database,app.state.settings,fake)
    assert await worker.process_one()
    state=(await client.get('/api/v1/assistant/wechat',headers=h)).json()['data']
    assert state['bindingStatus']=='bound' and state['qr'] is None
    assert state['canSend'] is False  # Login success does not invent a context token.
    assert state['analysisPreferences']['autoAnalyze'] is True
    other=(await client.get('/api/v1/assistant/wechat',headers=h2)).json()['data']
    assert other['bindingStatus']=='unbound' and other['peerLabel'] is None
    async with app.state.database.sessions() as session:
        row=await session.get(WechatBinding,owner)
        assert 'test-secret' not in row.encrypted
        assert cipher_for(app.state.settings).decode(row.encrypted)['peer']=='verified-peer'
    removed=await client.post('/api/v1/assistant/wechat/unbind',headers=h,json={'version':state['version']})
    assert removed.status_code==200
    async with app.state.database.sessions() as session:
        row=await session.get(WechatBinding,owner)
        assert row.encrypted is None and row.peer_hash is None and row.status=='unbound'
    assert fake.send.await_count==0

async def test_duplicate_weixin_peer_cannot_bind_two_accounts(client,app):
    first=await register(client);second=await register(client,'second@example.com')
    app.state.settings.wechat_binding_key=SecretStr(Fernet.generate_key().decode())
    fake=transport();app.state.wechat_transport=fake
    worker=WechatJobs(app.state.database,app.state.settings,fake)
    for auth in [first,second]:
        await client.post('/api/v1/assistant/wechat/qr',headers=bearer(auth),json={})
        async with app.state.database.sessions() as session:
            pending=await session.scalar(select(WechatBinding).where(WechatBinding.status=='pending'))
            pending.updated_at=now()-timedelta(seconds=4);await session.commit()
        await worker.process_one()
    state=(await client.get('/api/v1/assistant/wechat',headers=bearer(second))).json()['data']
    assert state['bindingStatus']=='failed' and state['canSend'] is False

async def test_wrong_sender_never_creates_request_or_context(client,app):
    auth=await register(client)
    app.state.settings.wechat_binding_key=SecretStr(Fernet.generate_key().decode())
    fake=transport();app.state.wechat_transport=fake
    await client.post('/api/v1/assistant/wechat/qr',headers=bearer(auth),json={})
    async with app.state.database.sessions() as session:
        row=await session.scalar(select(WechatBinding));await age(session,row.owner_id)
    worker=WechatJobs(app.state.database,app.state.settings,fake);await worker.process_one()
    fake.updates.return_value={'get_updates_buf':'cursor','msgs':[{'message_id':'malicious-message',
        'from_user_id':'other-peer','context_token':'wrong-context','item_list':[{'type':1,
        'text_item':{'text':'read another meeting'}}]}]}
    async with app.state.database.sessions() as session:
        row=await session.scalar(select(WechatBinding));await age(session,row.owner_id)
    await worker.process_one()
    state=(await client.get('/api/v1/assistant/wechat',headers=bearer(auth))).json()['data']
    assert state['canSend'] is False
    assert fake.send.await_count==0
    async with app.state.database.sessions() as session:
        assert list(await session.scalars(select(OutcomeRun)))==[]

@pytest.mark.parametrize('uncertain',[False,True])
async def test_delivery_receipt_dedup_and_pause(client,app,uncertain):
    await register(client)
    app.state.settings.wechat_binding_key=SecretStr(Fernet.generate_key().decode())
    fake=transport()
    if uncertain:fake.send.side_effect=ChannelError('transport_timeout',uncertain=True)
    async with app.state.database.sessions() as session:
        owner=await session.scalar(select(User.id).where(User.email=='yan@example.com'))
        session.add(Meeting(id='push-meeting',owner_id=owner,client_id='push-client',revision=1,content={'ended':True}))
        await session.flush()
        binding=WechatBinding(owner_id=owner,version=1,status='bound',peer_hash='unique-peer',
            encrypted=cipher_for(app.state.settings).encode({'base':'https://ilinkai.weixin.qq.com',
                'bot':'test-bot','peer':'verified-peer','token':'test-secret','context':'test-context','cursor':''}),
            state={'boundAt':(now()-timedelta(hours=1)).isoformat(),'generation':'test-generation',
                'preferences':{'paused':True,'autoMeeting':True,'importantOnly':True,'aiDone':True}},
            updated_at=now()-timedelta(seconds=3))
        report=OutcomeRun(id='push-report',owner_id=owner,meeting_id='push-meeting',intent_key='test-intent',
            version=1,status='succeeded',content={'kind':'role_outcome','createdAt':now().isoformat(),
                'result':{'title':'测试成果','summary':'真实证据还需核验','notify':True},'delivery':{'status':'not_sent'}})
        session.add_all([binding,report]);await session.commit()
    worker=WechatJobs(app.state.database,app.state.settings,fake)
    assert await worker.process_one()
    assert fake.send.await_count==0
    async with app.state.database.sessions() as session:
        binding=await session.get(WechatBinding,owner)
        binding.state={**binding.state,'preferences':{**binding.state['preferences'],'paused':False}}
        binding.updated_at=now()-timedelta(seconds=3);await session.commit()
    assert await worker.process_one()
    assert fake.send.await_count==1
    async with app.state.database.sessions() as session:
        report=await session.get(OutcomeRun,'push-report')
        assert report.content['delivery']['status']==('uncertain' if uncertain else 'accepted')
        await age(session,owner)
    await worker.process_one()
    assert fake.send.await_count==1
