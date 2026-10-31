"""设备跨账户隔离、退出即失效，以及缺失推送配置的真实状态。"""
from sqlalchemy import select
from test_meetings import account
from app.modules.push.models import PushDevice
from app.modules.push.services.push_jobs import eligible_devices


async def test_binding_requires_auth_and_does_not_claim_configured(client, app):
    payload = {"registrationId": "registration-abcdefgh", "installationId": "installation-private-device-0001",
        "appKey": "b34e401ae30b161e0c21c996", "platform": "android"}
    assert (await client.post('/api/v1/push/devices', json=payload)).status_code == 401
    headers = await account(client)
    response = await client.post('/api/v1/push/devices', headers=headers, json=payload)
    assert response.status_code == 200
    assert response.json()['data'] == {"bound": True, "configured": False}
    other = await account(client, 'push-other@example.com')
    assert (await client.post('/api/v1/push/devices', headers=other,
        json={**payload, 'installationId': 'another-installation-0000001'})).status_code == 409
    async with app.state.database.sessions() as session:
        assert len(await eligible_devices(session)) == 1
    assert (await client.post('/api/v1/auth/logout', headers=headers)).status_code == 200
    async with app.state.database.sessions() as session:
        assert await eligible_devices(session) == []


async def test_disabled_binding_is_not_a_push_target(client, app):
    headers = await account(client)
    response = await client.post('/api/v1/push/devices', headers=headers, json={
        "registrationId": "registration-disabled", "installationId": "installation-private-device-0002",
        "appKey": "b34e401ae30b161e0c21c996", "enabled": False})
    assert response.status_code == 200
    async with app.state.database.sessions() as session:
        assert await eligible_devices(session) == []


async def test_outbox_deduplicates_rechecks_and_retries(client, app, monkeypatch):
    from app.modules.push.services import push_jobs
    from app.modules.push.models import PushDelivery
    headers = await account(client)
    payload = {"registrationId": "registration-outbox", "installationId": "installation-outbox-00000001",
        "appKey": "b34e401ae30b161e0c21c996"}
    assert (await client.post('/api/v1/push/devices', headers=headers, json=payload)).status_code == 200
    events = {"notice": {"fingerprint": "v1", "completed": False}}
    async def current(session, owner): return events
    monkeypatch.setattr(push_jobs, "current_events", current)
    jobs = push_jobs.PushJobs(app.state.database, app.state.settings.model_copy(update={"jpush_app_key": payload["appKey"]}))
    class Sender:
        calls = 0
        async def send(self, registrations, key, completed):
            self.calls += 1
            assert registrations == [payload["registrationId"]]
            assert len(key) == 64 and not completed
            raise RuntimeError("transport")
    sender = Sender()
    jobs.client = sender
    await jobs.scan()
    await jobs.scan()
    async with app.state.database.sessions() as session:
        rows = list(await session.scalars(select(PushDelivery)))
        assert len(rows) == 1
        assert rows[0].status == "queued"
    assert await jobs.process_one()
    async with app.state.database.sessions() as session:
        row = await session.scalar(select(PushDelivery))
        assert row.status == "queued" and row.attempts == 1
        from datetime import datetime, timezone
        row.next_attempt_at = datetime.now(timezone.utc)
        await session.commit()
    events.clear()
    assert await jobs.process_one()
    assert sender.calls == 1
    async with app.state.database.sessions() as session:
        assert (await session.scalar(select(PushDelivery))).status == "stale"


async def test_enabling_push_suppresses_old_completed_events(client, app, monkeypatch):
    from app.modules.push.services import push_service
    from app.modules.push.models import PushDelivery
    async def current(session, owner):
        return {"archive:old": {"fingerprint": "1", "completed": True},
                "approval": {"fingerprint": "1", "completed": False}}
    monkeypatch.setattr(push_service, "current_events", current)
    headers = await account(client)
    response = await client.post('/api/v1/push/devices', headers=headers, json={
        "registrationId": "registration-baseline", "installationId": "installation-baseline-0000001",
        "appKey": "b34e401ae30b161e0c21c996"})
    assert response.status_code == 200
    async with app.state.database.sessions() as session:
        rows = list(await session.scalars(select(PushDelivery)))
        assert len(rows) == 1
        assert rows[0].status == "suppressed"
        assert rows[0].payload["notice"] == "archive:old"


async def test_new_event_wakes_push_loop_and_drains_batch_without_per_notice_sleep(app):
    import asyncio
    from types import SimpleNamespace
    from app.modules.push.services.push_jobs import PushJobs
    jobs = PushJobs(app.state.database, app.state.settings)
    jobs.client = SimpleNamespace(configured=True)
    first, drained = asyncio.Event(), asyncio.Event()
    pending = []
    async def scan(): first.set()
    async def process():
        if not pending: return False
        pending.pop()
        if not pending: drained.set()
        return True
    jobs.scan, jobs.process_one = scan, process
    jobs.start()
    try:
        await asyncio.wait_for(first.wait(), 1)
        pending.extend([1, 2, 3])
        jobs.wake()
        await asyncio.wait_for(drained.wait(), .5)
        assert not pending
    finally:
        await jobs.close()
