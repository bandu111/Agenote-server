"""只接收供应商返回的续接凭据；从不接受客户端指定声纹对象。"""
import hashlib
import re
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import delete
from app.modules.meetings.models.speaker_context import SpeakerContext


class SpeakerContextStore:
    def __init__(self, database, owner, meeting):
        self.database, self.owner = database, owner
        self.key = hashlib.sha256(f"{owner}:{meeting}".encode()).hexdigest()

    async def load(self):
        now = datetime.now(timezone.utc)
        async with self.database.sessions() as db:
            # 短期聚类标识最多保存 24 小时，不形成跨会议身份画像。
            await db.execute(delete(SpeakerContext).where(SpeakerContext.expires_at <= now))
            row = await db.get(SpeakerContext, self.key)
            result = (row.scope, row.provider_context) if row and row.owner_id == self.owner else None
            await db.commit()
            return result

    async def save(self, scope, payload):
        value = payload.get("speaker_context_id")
        if value is None and isinstance(payload.get("sentences"), dict):
            value = payload["sentences"].get("speaker_context_id")
        if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_\-=:./+]{1,512}", value):
            return
        async with self.database.sessions() as db:
            row = await db.get(SpeakerContext, self.key)
            now = datetime.now(timezone.utc)
            if row is None:
                row = SpeakerContext(id=self.key, owner_id=self.owner, scope=scope,
                    provider_context=value, expires_at=now + timedelta(hours=23))
                db.add(row)
            else:
                # 相同标识不滑动延长供应商的 24h 有效期。
                if row.provider_context != value or row.scope != scope:
                    row.expires_at = now + timedelta(hours=23)
                row.scope, row.provider_context = scope, value
            await db.commit()

    async def clear(self):
        async with self.database.sessions() as db:
            await db.execute(delete(SpeakerContext).where(SpeakerContext.id == self.key))
            await db.commit()


class SpeakerContextCleanup:
    """没有新会议时也清理过期的临时聚类凭据，不依赖客户端重新连接。"""
    def __init__(self, database):
        self.database, self.task = database, None

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def run(self):
        while True:
            try:
                async with self.database.sessions() as db:
                    await db.execute(delete(SpeakerContext).where(SpeakerContext.expires_at <= datetime.now(timezone.utc)))
                    await db.commit()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logging.getLogger("yanshu.asr").warning("speaker_context_cleanup_failed", extra={"error_type": type(error).__name__})
            await asyncio.sleep(600)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
