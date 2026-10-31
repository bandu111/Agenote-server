import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from datetime import timedelta
from types import SimpleNamespace
import jwt
import pytest
import pytest_asyncio
from pydantic import SecretStr,ValidationError
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from app.core.database.base import Base
from app.core.database.mixins import now
from app.modules.identity.models.identity import User,AuthSession
from app.modules.identity.schemas.auth import RecoveryLookupInput,DemoResetInput
from app.modules.identity.services.demo_recovery import DemoRecoveryService
from app.modules.identity.services.password_service import password_hasher

@pytest_asyncio.fixture
async def store():
    engine=create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as con:await con.run_sync(Base.metadata.create_all)
    settings=SimpleNamespace(jwt_secret=SecretStr('isolated-test-secret-only-not-for-live-0123456789'),jwt_issuer='test')
    async with async_sessionmaker(engine,expire_on_commit=False)() as db:
        yield db,DemoRecoveryService(db,settings),settings
    await engine.dispose()

async def add(db):
    user=User(name='QA',email='qa@example.com',password_hash=password_hasher.hash('old-password-42'),is_active=True)
    db.add(user);await db.flush()
    session=AuthSession(user_id=user.id,expires_at=now()+timedelta(days=1));db.add(session);await db.commit()
    return user,session

@pytest.mark.asyncio
async def test_real_database_lookup_reset_hash_revocation_and_replay(store):
    db,service,_=store;user,session=await add(db)
    lookup=await service.lookup(str(RecoveryLookupInput(email=' QA@Example.Com ').email))
    assert lookup['found'] is True
    old=user.password_hash
    payload=DemoResetInput(email='qa@example.com',reset_token=lookup['reset_token'],new_password='new-password-42')
    await service.reset(payload)
    await db.refresh(user);await db.refresh(session)
    assert user.password_hash != old
    assert password_hasher.verify('new-password-42',user.password_hash)
    assert not password_hasher.verify('old-password-42',user.password_hash)
    assert session.revoked_at is not None
    with pytest.raises(HTTPException) as error:await service.reset(payload)
    assert error.value.status_code==403

@pytest.mark.asyncio
async def test_unknown_disabled_mismatch_expired_and_forged_do_not_change_password(store):
    db,service,settings=store;user,_=await add(db);old=user.password_hash
    assert await service.lookup('unknown@example.com')=={'found':False,'reset_token':None}
    good=(await service.lookup(user.email))['reset_token']
    with pytest.raises(HTTPException):await service.reset(DemoResetInput(email='other@example.com',reset_token=good,new_password='new-password-42'))
    claims=jwt.decode(good,settings.jwt_secret.get_secret_value(),algorithms=['HS256'],audience='agenote-demo-recovery',issuer='test')
    claims['exp']=int(now().timestamp())-1
    expired=jwt.encode(claims,settings.jwt_secret.get_secret_value(),algorithm='HS256')
    for token in [expired,good[:-8]+'tampered']:
        with pytest.raises(HTTPException):await service.reset(DemoResetInput(email=user.email,reset_token=token,new_password='new-password-42'))
    await db.refresh(user);assert user.password_hash==old
    user.is_active=False;await db.commit();assert (await service.lookup(user.email))['found'] is False

@pytest.mark.parametrize('password',['short1','abcdefgh','123','x'*129])
def test_password_rules(password):
    with pytest.raises(ValidationError):DemoResetInput(email='qa@example.com',reset_token='x'*64,new_password=password)


@pytest.mark.asyncio
async def test_actual_http_contract_register_lookup_reset_and_login():
    from fastapi import FastAPI
    from httpx import AsyncClient,ASGITransport
    from app.core.config import Settings
    from app.core.rate_limit import AuthRateLimiter
    from app.core.database.session import get_db
    from app.core.error_handlers import register_error_handlers
    from app.modules.identity.routers.auth_router import router
    engine=create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as con:await con.run_sync(Base.metadata.create_all)
    sessions=async_sessionmaker(engine,expire_on_commit=False)
    app=FastAPI();app.include_router(router,prefix='/api/v1');register_error_handlers(app)
    app.state.settings=Settings(_env_file=None,app_env='test',database_url='postgresql+psycopg://unused:unused@127.0.0.1/unused',
        jwt_secret='isolated-http-qa-only-never-live-secret-0123456789',auth_requests_per_minute=1000)
    app.state.auth_limiter=AuthRateLimiter()
    async def db():
        async with sessions() as session:yield session
    app.dependency_overrides[get_db]=db
    try:
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
            account={'name':'QA','email':'qa@example.com','password':'old-password-42'}
            registered=await client.post('/api/v1/auth/register',json=account)
            assert registered.status_code==201,registered.text
            old_access=registered.json()['data']['access_token']
            missing=await client.post('/api/v1/auth/recovery/lookup',json={'email':'missing@example.com'})
            assert missing.json()['data']['found'] is False
            found=await client.post('/api/v1/auth/recovery/lookup',json={'email':' QA@EXAMPLE.COM '})
            assert found.status_code==200,found.text
            data=found.json()['data'];assert data['found'] is True
            reset=await client.post('/api/v1/auth/recovery/reset',json={'email':account['email'],
                'reset_token':data['reset_token'],'new_password':'new-password-42'})
            assert reset.status_code==200,reset.text
            old=await client.post('/api/v1/auth/login',json={'email':account['email'],'password':account['password']})
            assert old.status_code==401
            fresh=await client.post('/api/v1/auth/login',json={'email':account['email'],'password':'new-password-42'})
            assert fresh.status_code==200,fresh.text
            revoked=await client.get('/api/v1/auth/me',headers={'Authorization':'Bearer '+old_access})
            assert revoked.status_code==401
    finally:await engine.dispose()
