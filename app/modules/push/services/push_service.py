"""设备绑定只接受当前认证会话，不信任客户端账户编号。"""
import hashlib
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from datetime import datetime, timezone
from app.modules.push.models import PushDelivery
from app.modules.push.services.push_jobs import current_events, digest
from app.modules.push.models import PushDevice
from app.modules.push.services.jpush_client import JPushClient


class PushService:
    def __init__(self, session, settings):
        self.session, self.settings = session, settings
        self.configured = JPushClient(settings).configured

    async def bind(self, payload, identity):
        settings = self.settings
        if settings.jpush_app_key and payload.appKey != settings.jpush_app_key:
            raise HTTPException(409, "push_app_mismatch")
        key = hashlib.sha256(f"{payload.appKey}:{payload.registrationId}".encode()).hexdigest()
        installation = hashlib.sha256(payload.installationId.encode()).hexdigest()
        # 首次登记也串行化，避免同一设备并发插入产生唯一键冲突。
        await self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": int(key[:15], 16)})
        row = await self.session.get(PushDevice, key, with_for_update=True)
        if row and row.installation_hash != installation:
            raise HTTPException(409, "push_device_mismatch")
        baseline = payload.enabled and (row is None or not row.enabled or row.owner_id != identity[0].id)
        if row is None:
            row = PushDevice(id=key, installation_hash=installation)
            self.session.add(row)
        row.owner_id, row.session_id = identity[0].id, identity[1].id
        row.registration_id, row.app_key = payload.registrationId, payload.appKey
        row.platform, row.enabled = payload.platform, payload.enabled
        # 新设备开启时只提醒待决定事项，不补发过去全部完成记录。
        if baseline:
            now = datetime.now(timezone.utc)
            for notice, body in (await current_events(self.session, identity[0].id)).items():
                if not body["completed"]:
                    continue
                await self.session.execute(insert(PushDelivery).values(
                    id=digest([identity[0].id, notice, body["fingerprint"]]), owner_id=identity[0].id,
                    status="suppressed", payload={"notice": notice, **body}, attempts=0,
                    next_attempt_at=now, created_at=now, updated_at=now
                ).on_conflict_do_nothing(index_elements=["id"]))
        await self.session.commit()
        return {"bound": True, "configured": self.configured}
