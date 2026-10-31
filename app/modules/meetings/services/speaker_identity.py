"""Server-owned identity mapping; never trusts identity claims in transcript sync."""
from datetime import datetime, timezone
from app.modules.meetings.repositories.meeting_repository import MeetingRepository


def identity_output(meeting):
    return dict(revision=meeting.speaker_identity_revision or 0,
                identities=meeting.speaker_identities or {})


def speaker_display(source, identities):
    original = source.get('speaker', '待区分发言人')
    identity = identities.get(source.get('speaker_id'), {})
    if source.get('manual') or not source.get('speaker_separated') or identity.get('identity') != 'self':
        return original
    short = original[4:] if original.startswith('发言人 ') else original
    return f'我（{short}）'


class MeetingSpeakerIdentities:
    def __init__(self, database, owner, client_id):
        self.database, self.owner, self.client_id = database, owner, client_id

    async def snapshot(self):
        async with self.database.sessions() as db:
            meeting = await MeetingRepository(db).get(self.owner, self.client_id)
            return identity_output(meeting) if meeting else dict(revision=0, identities={})

    async def save(self, scope, speaker, identity):
        async with self.database.sessions() as db:
            meeting = await MeetingRepository(db).get(self.owner, self.client_id, lock=True)
            if meeting is None:
                raise LookupError('meeting_not_found')
            key = f'asr:{scope}:{speaker}'
            values = dict(meeting.speaker_identities or {})
            if values.get(key, {}).get('identity') != identity:
                values[key] = dict(identity=identity, source='voiceprint',
                                   updated_at=datetime.now(timezone.utc).isoformat())
                meeting.speaker_identities = values
                meeting.speaker_identity_revision = (meeting.speaker_identity_revision or 0) + 1
            revision = meeting.speaker_identity_revision or 0
            await db.commit()
            return revision
