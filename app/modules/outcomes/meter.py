"""Log actual calls without leaking prompts or inventing token/currency usage."""
import json
import logging
import time
from app.integrations.hermes.model_policy import select_model

class MeteredProvider:
    def __init__(self, provider, run_id):
        self.provider, self.run_id, self.calls = provider, run_id, []
    async def run(self, role, system, payload):
        route = select_model(self.provider.settings,role) if hasattr(self.provider,'settings') else None
        record={'role':role,'model':route.model if route else 'test',
            'inputCharacters':len(system)+len(json.dumps(payload,ensure_ascii=False)),
            'status':'failed','tokens':None,'currencyCost':None,
            'usageStatus':'not_exposed_by_current_sdk'}
        started=time.monotonic()
        try:
            value=await self.provider.run(role,system,payload)
            record.update(status='succeeded',outputCharacters=len(json.dumps(value,ensure_ascii=False)))
            return value
        finally:
            record['elapsedMs']=round((time.monotonic()-started)*1000)
            self.calls.append(record)
            logging.getLogger('agenote.outcomes.cost').info('outcome_model_call',
                extra={'run_id':self.run_id,**record})
