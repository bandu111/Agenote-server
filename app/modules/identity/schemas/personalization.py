"""First two personalization steps; user-declared context, never speaker identity."""
from typing import Literal
from pydantic import Field, field_validator, model_validator
from app.modules.identity.schemas.auth import StrictInput

PROFESSIONS = {'software', 'design', 'project', 'marketing', 'finance', 'education', 'other'}
SCENARIOS = {'work', 'project', 'technical', 'customer', 'learning', 'brainstorm', 'other'}

class PersonalizationInput(StrictInput):
    version: int = Field(ge=1)
    step: Literal[1, 2]
    action: Literal['draft', 'continue', 'complete', 'skip']
    name: str | None = Field(default=None, max_length=40)
    profession: str = Field(default='', max_length=40)
    profession_other: str = Field(default='', max_length=80)
    scenarios: list[str] = Field(default_factory=list, max_length=7)
    scenario_other: str = Field(default='', max_length=120)

    @field_validator('name', 'profession', 'profession_other', 'scenario_other', mode='before')
    @classmethod
    def trim(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode='after')
    def choices(self):
        if self.profession and self.profession not in PROFESSIONS:
            raise ValueError('invalid profession')
        if len(set(self.scenarios)) != len(self.scenarios) or not set(self.scenarios) <= SCENARIOS:
            raise ValueError('invalid scenarios')
        if self.action != 'draft':
            if self.profession == 'other' and not self.profession_other:
                raise ValueError('profession requires description')
            if 'other' in self.scenarios and not self.scenario_other:
                raise ValueError('scenario requires description')
        if self.action == 'continue' and self.step != 1:
            raise ValueError('continue only from step one')
        if self.action == 'complete' and self.step != 2:
            raise ValueError('complete only from step two')
        return self
