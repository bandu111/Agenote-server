"""两秒扫描业务事件，持久去重和重试；极光接收不等于手机已展示。"""
import asyncio
import hashlib
import json
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from app.modules.identity.models.identity import AuthSession
from app.modules.knowledge.services.document_actions import KnowledgeAssistance
from app.modules.home.services.home_service import HomeService
from app.modules.push.models import PushDevice, PushDelivery
from app.modules.push.services.jpush_client import JPushClient


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


async def eligible_devices(session, owner=None):
    q = select(PushDevice).join(AuthSession, PushDevice.session_id == AuthSession.id).where(
        PushDevice.enabled.is_(True), PushDevice.platform == "android",
        AuthSession.revoked_at.is_(None), AuthSession.expires_at > datetime.now(timezone.utc),
        PushDevice.owner_id == AuthSession.user_id)
    if owner: q = q.where(PushDevice.owner_id == owner)
    return list(await session.scalars(q))


async def current_events(session, owner):
    from app.modules.knowledge.services.notice_inbox import cleared_ids
    cleared = await cleared_ids(session, owner)
    events = {}
    for n in await KnowledgeAssistance(session).notices(owner):
        # 只能“知道了”的澄清建议静默；真正可填写的缺项由进展 pending 承载。
        if n["state"] == "pending" and n.get('kind') == 'warning':
            events[n["id"]] = {"fingerprint": n["fingerprint"], "completed": False}
    home = await HomeService(session).snapshot(owner)
    for n in home["decisions"]:
        # 风险与问卷已由上面的版本化通知覆盖；这里只补真正的进展缺项。
        # 普通授权、成果完成及归档保持静默，不生成系统提示或重复风险投递。
        if n['id'].startswith('pending:') and "progress-pending:" + n["id"][8:] not in cleared:
            events[n["id"]] = {"fingerprint": digest([n["id"], "pending"]), "completed": False}
    return events


class PushJobs:
    def __init__(self, database, settings):
        self.database, self.settings = database, settings
        self.client = JPushClient(settings)
        self.task = None
        self.changed = asyncio.Event()

    def wake(self):
        """新审批已提交时立即扫描，不等待固定轮询。"""
        self.changed.set()

    def start(self):
        self.task = asyncio.create_task(self.loop())

    async def close(self):
        if self.task:
            self.task.cancel()
            try: await self.task
            except asyncio.CancelledError: pass

    async def loop(self):
        while True:
            try:
                if self.client.configured:
                    await self.scan()
                    # 一批待确认事项不能每条再等两秒；失败仍沿用持久回执和退避。
                    for _ in range(10):
                        if not await self.process_one(): break
            except asyncio.CancelledError: raise
            except Exception:
                # 禁止记录推送凭据、设备标识或业务正文；下轮按持久游标重试。
                pass
            try:
                await asyncio.wait_for(self.changed.wait(), 2)
            except TimeoutError:
                pass
            self.changed.clear()

    async def scan(self):
        async with self.database.sessions() as session:
            owners = {d.owner_id for d in await eligible_devices(session)}
        for owner in owners:
            async with self.database.sessions() as session:
                events = await current_events(session, owner)
                for identity, body in events.items():
                    key = digest([owner, identity, body["fingerprint"]])
                    await session.execute(insert(PushDelivery).values(id=key, owner_id=owner,
                        status="queued", payload={"notice": identity, **body}, attempts=0,
                        next_attempt_at=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc)).on_conflict_do_nothing(index_elements=["id"]))
                await session.commit()

    async def process_one(self):
        async with self.database.sessions() as session:
            now = datetime.now(timezone.utc)
            # 进程退出后的发送租约会过期；可能重复展示同一通用提醒，业务审批仍幂等。
            await session.execute(update(PushDelivery).where(PushDelivery.status == "sending",
                PushDelivery.updated_at < now - timedelta(minutes=2)).values(status="queued"))
            item = await session.scalar(select(PushDelivery).where(PushDelivery.status == "queued",
                PushDelivery.next_attempt_at <= now).order_by(PushDelivery.created_at).with_for_update(skip_locked=True).limit(1))
            if not item: await session.commit(); return False
            current = (await current_events(session, item.owner_id)).get(item.payload["notice"])
            devices = await eligible_devices(session, item.owner_id)
            if not current or current["fingerprint"] != item.payload["fingerprint"] or not devices:
                item.status = "stale"; await session.commit(); return True
            key, payload = item.id, item.payload
            registrations = [d.registration_id for d in devices if d.app_key == self.settings.jpush_app_key]
            if not registrations:
                item.status = "stale"; await session.commit(); return True
            item.status = "sending"; item.attempts += 1; item.updated_at = now
            await session.commit()
        try:
            await self.client.send(registrations, key, payload["completed"])
            status = "sent"
        except Exception: status = "queued"
        async with self.database.sessions() as session:
            row = await session.get(PushDelivery, key, with_for_update=True)
            if row and row.status == "sending":
                row.status = "failed" if status == "queued" and row.attempts >= 5 else status
                row.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=min(300, 2 ** row.attempts * 5))
                await session.commit()
        return True
