"""验证资料持久化、版本竞争、口令撤销与跨账户隔离。"""
import asyncio
import base64
from io import BytesIO
from PIL import Image
from sqlalchemy import select, text
from app.modules.identity.models.identity import User
from tests.test_auth import register, bearer, PASSWORD


def png():
    stream = BytesIO()
    Image.new('RGB', (16, 16), 'blue').save(stream, format='PNG')
    return base64.b64encode(stream.getvalue()).decode()


async def test_profile_sync_version_isolation_and_avatar_removal(client):
    first = await register(client)
    second = await register(client, 'second@example.com')
    payload = dict(version=1, name='新昵称', avatar=png(), job='工程师', email='contact@example.com')
    saved = await client.post('/api/v1/auth/profile', headers=bearer(first), json=payload)
    assert saved.status_code == 200
    data = saved.json()['data']
    assert data['version'] == 2 and data['avatar'] and data['job'] == '工程师'
    assert data['email'] == 'contact@example.com' and data['loginEmail'] == 'yan@example.com'
    assert (await client.get('/api/v1/auth/me', headers=bearer(first))).json()['data']['name'] == '新昵称'
    other = (await client.get('/api/v1/auth/profile', headers=bearer(second))).json()['data']
    assert other['avatar'] is None and other['version'] == 1
    assert (await client.post('/api/v1/auth/profile', headers=bearer(first), json=payload)).status_code == 409
    payload.update(version=2, avatar=None)
    assert (await client.post('/api/v1/auth/profile', headers=bearer(first), json=payload)).json()['data']['avatar'] is None
    payload.update(version=3, avatar=base64.b64encode(b'<svg>unsafe</svg>').decode())
    assert (await client.post('/api/v1/auth/profile', headers=bearer(first), json=payload)).status_code == 422


async def test_concurrent_profile_edit_cannot_silently_overwrite(client):
    account = await register(client)
    results = await asyncio.gather(*[client.post('/api/v1/auth/profile', headers=bearer(account), json=dict(version=1, name=name)) for name in ('甲', '乙')])
    assert sorted(r.status_code for r in results) == [200, 409]


async def test_password_change_revokes_all_devices_and_old_password(client):
    account = await register(client)
    second = (await client.post('/api/v1/auth/login', json=dict(email='yan@example.com', password=PASSWORD))).json()['data']
    path = '/api/v1/auth/password'
    assert (await client.post(path, headers=bearer(account), json=dict(current_password='wrong', new_password='new-password-123'))).status_code == 403
    assert (await client.get('/api/v1/auth/me', headers=bearer(account))).status_code == 200
    assert (await client.post(path, headers=bearer(account), json=dict(current_password=PASSWORD, new_password=PASSWORD))).status_code == 422
    assert (await client.post(path, headers=bearer(account), json=dict(current_password=PASSWORD, new_password='new-password-123'))).status_code == 200
    for session in (account, second):
        assert (await client.get('/api/v1/auth/me', headers=bearer(session))).status_code == 401
        assert (await client.post('/api/v1/auth/refresh', json={'refresh_token': session['refresh_token']})).status_code == 401
    assert (await client.post('/api/v1/auth/login', json=dict(email='yan@example.com', password=PASSWORD))).status_code == 401
    assert (await client.post('/api/v1/auth/login', json=dict(email='yan@example.com', password='new-password-123'))).status_code == 200


async def test_delete_requires_password_and_does_not_delete_other_owner(client, app):
    first = await register(client)
    second = await register(client, 'second@example.com')
    path = '/api/v1/auth/delete-account'
    assert (await client.post(path, headers=bearer(first), json=dict(current_password='wrong', confirmation='DELETE'))).status_code == 403
    assert (await client.post(path, headers=bearer(first), json=dict(current_password=PASSWORD, confirmation='DELETE', owner_id=second['user']['id']))).status_code == 422
    assert (await client.post(path, headers=bearer(first), json=dict(current_password=PASSWORD, confirmation='DELETE'))).status_code == 200
    assert (await client.get('/api/v1/auth/me', headers=bearer(first))).status_code == 401
    assert (await client.post('/api/v1/auth/refresh', json={'refresh_token': first['refresh_token']})).status_code == 401
    assert (await client.get('/api/v1/auth/me', headers=bearer(second))).status_code == 200
    async with app.state.database.sessions() as db:
        assert await db.get(User, first['user']['id']) is None
        assert await db.get(User, second['user']['id']) is not None


async def test_delete_cascades_business_rows_and_graph_checkpoints(client, app):
    from app.modules.meetings.models.meeting import Meeting
    from app.modules.knowledge.models.library import KnowledgeLibrary
    from app.modules.identity.services.account_cleanup import AccountCheckpointCleanup
    import uuid
    first = await register(client)
    owner = first['user']['id']
    thread = owner + ':' + str(uuid.uuid4())
    other_thread = str(uuid.uuid4()) + ':' + str(uuid.uuid4())
    async with app.state.database.sessions() as db:
        db.add(Meeting(id=str(uuid.uuid4()), owner_id=owner, client_id='qa-meeting', revision=1, content={}))
        db.add(KnowledgeLibrary(owner_id=owner, snapshot={'documents': []}))
        for tid in (thread, other_thread, 'unrelated-app:keep'):
            await db.execute(text("INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) VALUES (:thread, '', 'qa', '{}'::jsonb, '{}'::jsonb)"), {'thread': tid})
        await db.commit()
    assert (await client.post('/api/v1/auth/delete-account', headers=bearer(first), json=dict(current_password=PASSWORD, confirmation='DELETE'))).status_code == 200
    async with app.state.database.sessions() as db:
        assert not (await db.scalars(select(Meeting))).all()
        assert not (await db.scalars(select(KnowledgeLibrary))).all()
        assert not await db.scalar(text('SELECT count(*) FROM checkpoints WHERE thread_id=:thread'), {'thread': thread})
    await AccountCheckpointCleanup(app.state.database).sweep()
    async with app.state.database.sessions() as db:
        assert not await db.scalar(text('SELECT count(*) FROM checkpoints WHERE thread_id=:thread'), {'thread': other_thread})
        assert await db.scalar(text("SELECT count(*) FROM checkpoints WHERE thread_id='unrelated-app:keep'")) == 1


async def test_password_change_serializes_with_refresh(client):
    account = await register(client)
    changed, refreshed = await asyncio.gather(
        client.post('/api/v1/auth/password', headers=bearer(account), json=dict(current_password=PASSWORD, new_password='new-password-123')),
        client.post('/api/v1/auth/refresh', json={'refresh_token': account['refresh_token']}),
    )
    assert changed.status_code == 200
    assert refreshed.status_code in (200, 401)
    if refreshed.status_code == 200:
        assert (await client.get('/api/v1/auth/me', headers=bearer(refreshed.json()['data']))).status_code == 401


def test_direct_entrypoint_import_bootstrap(tmp_path):
    import subprocess, sys
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / 'app/main.py'
    # 与 IDE/服务直接运行文件保持相同导入顺序，但不真正启动监听端口。
    result = subprocess.run([sys.executable, '-c', 'import runpy; runpy.run_path('+repr(str(path))+', run_name="entrypoint_import_check")'],
                            cwd=tmp_path, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
