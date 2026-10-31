"""Cloud-backed, account-scoped onboarding with optimistic concurrency."""
from fastapi import HTTPException
from app.core.database.mixins import now
from app.modules.identity.services.account_service import AccountService


def initial_personalization():
    return dict(schema=1, status='pending', step=1, profession='', profession_other='',
                scenarios=[], scenario_other='')


class PersonalizationService(AccountService):
    async def save(self, owner, session_id, payload):
        user = await self.locked_user(owner, session_id)
        if payload.version != user.profile_version:
            raise HTTPException(409, 'profile_version_conflict')
        profile = dict(user.profile or {})
        current = profile.get('personalization')
        # No retroactive forced enrollment for legacy users or terminal accounts.
        if not current or current.get('status') in ('completed', 'skipped'):
            raise HTTPException(409, 'personalization_closed')
        if payload.action == 'complete' and current.get('step') != 2:
            raise HTTPException(409, 'personalization_step_conflict')
        value = dict(schema=1, status='in_progress', step=payload.step,
                     profession=payload.profession,
                     profession_other=payload.profession_other if payload.profession == 'other' else '',
                     scenarios=payload.scenarios,
                     scenario_other=payload.scenario_other if 'other' in payload.scenarios else '',
                     updated_at=now().isoformat())
        if payload.action == 'continue': value['step'] = 2
        elif payload.action in ('complete', 'skip'):
            value['status'] = 'completed' if payload.action == 'complete' else 'skipped'
            value['finished_at'] = value['updated_at']
        if payload.name: user.name = payload.name
        profile['personalization'] = value
        from app.modules.identity.services.professional_profile import professional_profile
        if profile.get('job_source') == 'onboarding':
            profile.pop('job', None)
            profile.pop('outcome_profession', None)
        profile = professional_profile(profile)
        user.profile = profile
        user.profile_version += 1
        await self.session.commit()
        return self.output(user)
