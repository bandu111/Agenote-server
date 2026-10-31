"""会议持久化与版本冲突处理，不在此层处理 HTTP。"""

import uuid
import hashlib
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.modules.meetings.models.meeting import Meeting
from app.modules.meetings.schemas.meeting import MeetingSync
from app.modules.meetings.repositories.meeting_repository import MeetingRepository
from app.modules.meetings.repositories.archive_repository import ArchiveRepository


class MeetingConflictError(Exception):
    """同步版本冲突，或试图改写已保存的原话。"""


class MeetingNotFoundError(Exception):
    """当前账户不存在指定会议。"""


def meeting_output(meeting: Meeting, archive=None):
    # Older/imported records can omit optional sync fields. Reading them must
    # not break every other meeting in the owner's history.
    content = {
        "minutes": "", "paused": False, "ended": False,
        "title": "录音会议", "utterances": [],
        **meeting.content,
    }
    return {
        **{
            key: value
            for key, value in content.items()
            if key not in {"client_id", "started_at", "elapsed_ms"}
        },
        "deltaSupported": True,
        "id": meeting.client_id,
        "revision": meeting.revision,
        "speakerIdentityRevision": meeting.speaker_identity_revision or 0,
        "speakerIdentities": meeting.speaker_identities or {},
        "sessionId": meeting.id,
        "ownerUserId": meeting.owner_id,
        "startedAt": content["started_at"],
        "elapsed": content["elapsed_ms"],
        "analysis": meeting.analysis,
        "analysisRevision": meeting.analysis_revision,
        "archived": bool(
            content["ended"]
            and (
                content["minutes"] or (archive and archive.revision == meeting.revision)
            )
        ),
        "documentId": archive.id if archive else None,
        "archiveRevision": archive.revision if archive else None,
    }


class MeetingService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.repository = MeetingRepository(session)
        self.archives = ArchiveRepository(session)

    async def output(self, row):
        value = meeting_output(row, await self.archives.get(row.owner_id, row.id))
        from app.modules.meetings.services.source_changes import read_changes
        value['sourceChanges'] = await read_changes(self.session, row)
        from app.modules.meetings.models.audio import MeetingAudio
        from sqlalchemy import select
        recordings = await self.session.scalars(select(MeetingAudio).where(
            MeetingAudio.meeting_id == row.id, MeetingAudio.owner_id == row.owner_id,
            MeetingAudio.state == 'complete'))
        sources = {u['id']:u for u in row.content['utterances']}
        value['audioSources'] = {s['id']:{'audioId':r.id,'start':s['start'],'end':s['end']}
            for r in recordings for s in r.metadata_json['sources']
            if s['id'] in sources and hashlib.sha256(sources[s['id']]['raw'].encode()).hexdigest() == s['hash']}
        return value

    async def sync(self, owner_id: str, data: MeetingSync):
        try:
            # 数据库事务锁同时覆盖首次创建，避免两个设备重复写入同一会议。
            lock_key = int.from_bytes(
                hashlib.sha256(f"{owner_id}:{data.client_id}".encode()).digest()[:8],
                "big",
                signed=True,
            )
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key}
            )
            row = await self.repository.get(owner_id, data.client_id, lock=True, include_deleted=True)
            if row is not None and row.deleted:
                from fastapi import HTTPException
                raise HTTPException(410, 'meeting_deleted')
            if len({u.id for u in data.utterances}) != len(data.utterances):
                raise MeetingConflictError()
            # Older clients may still submit the original recording placeholder.
            from app.modules.meetings.services.meeting_title import is_default_title
            if row and is_default_title(data.title) and not is_default_title(row.content.get('title')):
                data = data.model_copy(update={'title': row.content['title']})
            content = data.model_dump(mode="json")
            if row:
                if data.revision < row.revision:
                    return await self.output(row)
                if data.revision == row.revision:
                    # 新增可选协议字段后，旧记录按默认值规范化，保持幂等重试兼容。
                    if content != MeetingSync.model_validate(row.content).model_dump(
                        mode="json"
                    ):
                        raise MeetingConflictError()
                    return await self.output(row)
                # 原话、来源时间及发言人一经保存不能被 AI 或同步重试覆盖。
                previous = row.content["utterances"]
                if len(data.utterances) < len(previous) or content["started_at"] != row.content["started_at"]:
                    raise MeetingConflictError()
                for old, new in zip(previous, content["utterances"]):
                    if any(old.get(k) != new.get(k) for k in ["id", "raw", "at"]):
                        raise MeetingConflictError()
                    old_identity = (old["speaker"], old.get("speaker_id"), old.get("speaker_separated", False))
                    new_identity = (new["speaker"], new.get("speaker_id"), new.get("speaker_separated", False))
                    old_revision = old.get("speaker_revision", 0)
                    history = new.get("speaker_history", [])
                    if old_identity != new_identity:
                        old_history = old.get("speaker_history", []) or [{"revision":old_revision,"speaker":old_identity[0],"speaker_id":old_identity[1] or "unseparated","speaker_separated":old_identity[2]}]
                        if (new["speaker_revision"] <= old_revision or len(history) <= len(old_history)
                            or history[:len(old_history)] != old_history
                            or history[-1] != {"revision":new["speaker_revision"],"speaker":new_identity[0],"speaker_id":new_identity[1] or "unseparated","speaker_separated":new_identity[2]}
                            or any(a["revision"] >= b["revision"] for a,b in zip(history,history[1:]))):
                            raise MeetingConflictError()
                    elif new["speaker_revision"] != old_revision or history != old.get("speaker_history", []):
                        raise MeetingConflictError()
                if data.elapsed_ms < row.content["elapsed_ms"]:
                    raise MeetingConflictError()
                if row.content["ended"] and not data.ended:
                    raise MeetingConflictError()
                row.content = content
                row.revision = data.revision
            else:
                row = Meeting(
                    id=str(uuid.uuid4()),
                    owner_id=owner_id,
                    client_id=data.client_id,
                    revision=data.revision,
                    content=content,
                    analysis_revision=-1,
                )
                await self.repository.add(row)
            await self.session.commit()
            return await self.output(row)
        except Exception:
            await self.session.rollback()
            raise

    async def sync_delta(self, owner_id, delta):
        # 同一把事务锁覆盖读取、合并及原始证据校验；不能先读取再无锁覆盖。
        lock_key = int.from_bytes(hashlib.sha256(f"{owner_id}:{delta.client_id}".encode()).digest()[:8], 'big', signed=True)
        await self.session.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key':lock_key})
        row = await self.repository.get(owner_id, delta.client_id, lock=True)
        if row is None: raise MeetingNotFoundError()
        if delta.base_revision != row.revision and delta.revision != row.revision:
            raise MeetingConflictError()
        values = delta.model_dump(mode='json', exclude={'base_revision'})
        old = row.content['utterances']
        updates = {u['id']:u for u in values['utterances']}
        if len(updates) != len(values['utterances']): raise MeetingConflictError()
        known = {u['id'] for u in old}
        values['utterances'] = [updates.get(u['id'],u) for u in old] + [u for u in values['utterances'] if u['id'] not in known]
        # 完整校验与旧入口共用，增量协议不能绕过原话不可变、结束状态与版本规则。
        from pydantic import ValidationError
        try: complete = MeetingSync.model_validate(values)
        except ValidationError as error: raise MeetingConflictError() from error
        saved = await self.sync(owner_id, complete)
        # 长会议的增量响应也只回读版本与接收 ID，不再下载整场历史。
        return {'id':saved['id'], 'revision':saved['revision'], 'deltaSupported':True,
                'acceptedUtteranceIds':list(updates)}

    async def get(self, owner_id: str, client_id: str):
        row = await self.repository.get(owner_id, client_id)
        if row is None:
            raise MeetingNotFoundError()
        return await self.output(row)

    async def list(self, owner_id: str):
        rows = await self.repository.list(owner_id)
        archives = await self.archives.for_meetings(owner_id, [row.id for row in rows])
        from app.modules.meetings.services.source_changes import read_changes
        from sqlalchemy import select
        from app.modules.meetings.models.audio import MeetingAudio
        recordings = list(await self.session.scalars(select(MeetingAudio).where(
            MeetingAudio.owner_id == owner_id, MeetingAudio.state == 'complete',
            MeetingAudio.meeting_id.in_([row.id for row in rows]))))
        result = []
        for row in rows:
            sources = {u['id']:u for u in row.content['utterances']}
            audio = {s['id']:{'audioId':r.id,'start':s['start'],'end':s['end']}
                for r in recordings if r.meeting_id == row.id for s in r.metadata_json['sources']
                if s['id'] in sources and hashlib.sha256(sources[s['id']]['raw'].encode()).hexdigest() == s['hash']}
            result.append({**meeting_output(row, archives.get(row.id)),
                'sourceChanges': await read_changes(self.session, row), 'audioSources': audio})
        return result
