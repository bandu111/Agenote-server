import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datetime import timedelta, timezone
from sqlalchemy import event

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.core.database.base import Base
from app.core.database.mixins import now
from app.modules.identity.models.identity import User, AuthSession
@event.listens_for(AuthSession, 'load')
def _sqlite_utc(target, context):
    if target.expires_at and target.expires_at.tzinfo is None: target.expires_at=target.expires_at.replace(tzinfo=timezone.utc)

@event.listens_for(AuthSession, 'refresh')
def _sqlite_utc_refresh(target, context, attrs):
    _sqlite_utc(target, context)

from app.modules.identity.schemas.personalization import PersonalizationInput
from app.modules.identity.schemas.account import ProfileInput
from app.modules.identity.services.personalization_service import PersonalizationService, initial_personalization
from app.modules.identity.services.account_service import AccountService

@pytest_asyncio.fixture
async def store():
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as con: await con.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[Base.metadata.tables[name] for name in ('users', 'auth_sessions', 'refresh_tokens')]))
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        users=[]
        for i in range(2):
            user=User(name='Original', email=f'user{i}@example.com', password_hash='unused',
                profile={'personalization': initial_personalization(), 'company': 'Preserve'})
            db.add(user); await db.flush()
            auth=AuthSession(user_id=user.id, expires_at=now()+timedelta(days=1))
            db.add(auth); await db.flush(); users.append((user,auth))
        await db.commit()
        yield db,users
    await engine.dispose()

def payload(version=1,step=1,action='continue',**kw):
    return PersonalizationInput(version=version,step=step,action=action,**kw)

@pytest.mark.asyncio
async def test_real_cloud_state_resume_complete_and_profile_edit_preserves_context(store):
    db,users=store; u,a=users[0]; service=PersonalizationService(db)
    first=await service.save(u.id,a.id,payload(name='  新昵称  ',profession='software'))
    assert first['name']=='新昵称' and first['personalization']['step']==2
    assert first['company']=='Preserve'
    assert first['job'] == '软件开发'
    assert u.profile['outcome_profession']['categoryId'] == 'engineering'
    assert u.profile['outcome_profession']['confirmed'] is True
    # Persisted state survives a re-read, not a process-global flag.
    owner=u.id; session_id=a.id; db.expire_all(); u=await db.get(User,owner)
    assert u.profile['personalization']['step']==2
    result=await service.save(owner,session_id,payload(2,2,'complete',name='',profession='software',scenarios=['work','technical']))
    assert result['name']=='新昵称' and result['personalization']['status']=='completed'
    saved=await AccountService(db).save_profile(owner,session_id,ProfileInput(version=3,name='Edited',job='Developer'))
    assert saved['personalization']['scenarios']==['work','technical']
    assert saved['job'] == 'Developer'
    assert u.profile['outcome_profession']['raw'] == 'Developer'
    other=await db.get(User, users[1][0].__dict__['_sa_instance_state'].identity[0])
    assert other.profile['personalization']['status']=='pending'
    with pytest.raises(HTTPException) as closed:
        await service.save(owner,session_id,payload(4,1,'draft'))
    assert closed.value.status_code==409

@pytest.mark.asyncio
async def test_drafts_skip_conflict_and_account_isolation(store):
    db,users=store;u,a=users[0]; service=PersonalizationService(db)
    draft=await service.save(u.id,a.id,payload(action='draft',name='Draft',profession='other'))
    assert draft['personalization']['status']=='in_progress'
    with pytest.raises(HTTPException) as conflict: await service.save(u.id,a.id,payload(name='stale'))
    assert conflict.value.status_code==409
    owner=u.id; session_id=a.id; other_session_id=users[1][1].id
    await db.rollback()
    # Session belonging to another account cannot mutate this account.
    with pytest.raises(HTTPException): await service.save(owner,other_session_id,payload(2,1,'skip'))
    result=await service.save(owner,session_id,payload(2,1,'skip',name=''))
    assert result['name']=='Draft' and result['personalization']['status']=='skipped'

@pytest.mark.asyncio
async def test_legacy_not_enrolled_and_invalid_step(store):
    db,users=store;u,a=users[0];service=PersonalizationService(db)
    with pytest.raises(HTTPException): await service.save(u.id,a.id,payload(1,2,'complete'))
    u.profile={};await db.commit()
    assert AccountService.output(u)['personalization'] is None
    with pytest.raises(HTTPException): await service.save(u.id,a.id,payload())

@pytest.mark.parametrize('extra', [dict(profession='fake'),dict(scenarios=['fake']),dict(scenarios=['work','work']),
    dict(name='x'*41),dict(profession='other'),dict(scenarios=['other'])])
def test_unknown_options_and_invalid_fields(extra):
    with pytest.raises(ValidationError): payload(**extra)

@pytest.mark.asyncio
async def test_actual_http_registration_enrollment_and_owner_endpoint():
    from fastapi import FastAPI
    from httpx import AsyncClient, ASGITransport
    from app.core.config import Settings
    from app.core.rate_limit import AuthRateLimiter
    from app.core.database.session import get_db
    from app.core.error_handlers import register_error_handlers
    from app.modules.identity.routers.auth_router import router
    engine=create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as con: await con.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[Base.metadata.tables[name] for name in ('users', 'auth_sessions', 'refresh_tokens')]))
    sessions=async_sessionmaker(engine,expire_on_commit=False)
    app=FastAPI();app.include_router(router,prefix='/api/v1');register_error_handlers(app)
    app.state.settings=Settings(_env_file=None,app_env='test',database_url='postgresql+psycopg://unused:unused@127.0.0.1/unused',jwt_secret='isolated-test-secret-01234567890123456789',auth_requests_per_minute=1000)
    app.state.auth_limiter=AuthRateLimiter()
    async def db():
        async with sessions() as s: yield s
    app.dependency_overrides[get_db]=db
    try:
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as c:
            registered=await c.post('/api/v1/auth/register',json={'name':'Registered Name','email':'new@example.com','password':'password42'})
            assert registered.status_code==201,registered.text
            headers={'Authorization':'Bearer '+registered.json()['data']['access_token']}
            p=(await c.get('/api/v1/auth/profile',headers=headers)).json()['data']
            assert p['personalization']['status']=='pending' and p['name']=='Registered Name'
            first=await c.post('/api/v1/auth/personalization',headers=headers,json=payload(name='Updated',profession='finance').model_dump())
            assert first.status_code==200,first.text
            last=await c.post('/api/v1/auth/personalization',headers=headers,json=payload(2,2,'complete',scenarios=['work','customer']).model_dump())
            assert last.status_code==200,last.text
            me=(await c.get('/api/v1/auth/me',headers=headers)).json()['data']
            assert me['name']=='Updated'
            assert (await c.get('/api/v1/auth/profile',headers=headers)).json()['data']['personalization']['status']=='completed'
            assert (await c.post('/api/v1/auth/personalization',json=payload().model_dump())).status_code==401
    finally: await engine.dispose()

@pytest.mark.asyncio
async def test_onboarding_job_changes_and_custom_title_share_analysis_profile(store):
    db, users = store
    user, auth = users[0]
    service = PersonalizationService(db)
    await service.save(user.id, auth.id, payload(action='draft', profession='software'))
    changed = await service.save(user.id, auth.id, payload(2, action='continue', profession='other', profession_other='销售总监'))
    assert changed['job'] == '销售总监'
    assert user.profile['outcome_profession']['raw'] == '销售总监'
    assert user.profile['outcome_profession']['categoryId'] == 'sales_business'
    assert user.profile['outcome_profession']['confirmed'] is False
    assert users[1][0].profile.get('job') is None


def test_legacy_onboarding_is_readable_without_overwriting_explicit_profile():
    from app.modules.identity.services.professional_profile import professional_profile
    for key, title, category in [('software','软件开发','engineering'), ('design','产品设计','product_design'),
                                 ('project','项目管理','project_delivery'), ('marketing','市场运营','marketing_growth'),
                                 ('education','教育','education_research'), ('finance','财务','finance_legal')]:
        profile = {'personalization': {'profession': key, 'status': 'completed'}}
        normalized = professional_profile(profile)
        assert normalized['job'] == title
        assert normalized['outcome_profession']['categoryId'] == category
        assert 'job' not in profile
        assert professional_profile({**profile, 'job':'架构师'})['job'] == '架构师'
        assert professional_profile({**profile, 'job':'', 'job_source':'profile'})['job'] == ''
