"""工具名与参数白名单：模型不能传用户身份、路径、URL 或任意命令。"""
from typing import Literal
from pydantic import Field, model_validator
from app.modules.meetings.schemas.meeting import StrictModel


class ToolStep(StrictModel):
    id: str = Field(pattern=r'^[a-z][a-z0-9_]{0,19}$')
    tool: Literal['knowledge_search', 'knowledge_read', 'progress_read', 'task_create', 'document_create', 'task_attach', 'task_verify', 'task_read', 'task_update', 'task_link', 'source_change']
    args: dict = Field(default_factory=dict)


class ToolPlan(StrictModel):
    title: str = Field(min_length=1, max_length=120)
    evidence_ids: list[str] = Field(min_length=1, max_length=8)
    steps: list[ToolStep] = Field(min_length=1, max_length=6)

    @model_validator(mode='after')
    def check_ids(self):
        if len({s.id for s in self.steps}) != len(self.steps): raise ValueError('duplicate_step_id')
        import re
        seen = set()
        task_versions = {}
        task_identities = {}
        for step in self.steps:
            schema = ARGUMENTS[step.tool]
            if set(step.args) - set(schema.model_fields): raise ValueError('tool_argument_not_allowed')
            if any(field.is_required() and name not in step.args for name, field in schema.model_fields.items()): raise ValueError('tool_argument_missing')
            references = False
            for value in step.args.values():
                if value is None: continue
                if not isinstance(value, str): raise ValueError('tool_argument_type')
                if '${' in value:
                    match = re.fullmatch(r'\$\{([a-z][a-z0-9_]{0,19})\.([a-zA-Z][a-zA-Z0-9_]{0,39})\}', value)
                    if not match or match[1] not in seen: raise ValueError('tool_reference_order')
                    references = True
            if not references: schema.model_validate(step.args)
            # 同一计划中的任务写入会递增版本，后续操作必须引用最近回执。
            # 在展示审批指纹之前规范化，不能批准后悄悄替换数据库版本。
            identity = task_identities.get(step.args.get('taskId'), step.args.get('taskId'))
            if step.tool in {'task_update', 'task_link'} and identity in task_versions:
                step.args = {**step.args, 'version': task_versions[identity]}
            if step.tool in {'task_create', 'task_read'}:
                identity = '${' + step.id + '.taskId}'
                task_identities[identity] = identity
                task_versions[identity] = '${' + step.id + '.version}'
            if step.tool in {'task_update', 'task_link', 'task_attach', 'task_verify'}:
                task_identities['${' + step.id + '.taskId}'] = identity
                task_versions[identity] = '${' + step.id + '.version}'
            seen.add(step.id)
        if len(self.model_dump_json()) > 180000: raise ValueError('tool_plan_too_large')
        return self


class SearchArgs(StrictModel):
    query: str = Field(min_length=1, max_length=1000)


class ReadArgs(StrictModel):
    documentId: str = Field(min_length=1, max_length=100)
    versionId: str = Field(min_length=1, max_length=100)
    hash: str = Field(pattern=r'^[a-f0-9]{64}$')


class TaskArgs(StrictModel):
    title: str = Field(min_length=2, max_length=300)
    deadline_text: str | None = Field(default=None, max_length=100)


class DocumentArgs(StrictModel):
    title: str = Field(min_length=2, max_length=160)
    content: str = Field(min_length=1, max_length=60000)


class TaskEvidenceArgs(StrictModel):
    taskId: str = Field(min_length=1, max_length=100)
    documentId: str = Field(min_length=1, max_length=100)
    versionId: str = Field(min_length=1, max_length=100)
    hash: str = Field(pattern=r'^[a-f0-9]{64}$')


class TaskReadArgs(StrictModel):
    query: str = Field(min_length=2, max_length=300)


class TaskUpdateArgs(StrictModel):
    taskId: str = Field(min_length=1, max_length=100)
    version: str = Field(pattern=r'^[1-9][0-9]{0,8}$')
    status: Literal['active', 'claimed_completed']
    note: str = Field(min_length=1, max_length=1000)


class TaskLinkArgs(StrictModel):
    taskId: str = Field(min_length=1, max_length=100)
    version: str = Field(pattern=r'^[1-9][0-9]{0,8}$')
    goalId: str = Field(min_length=1, max_length=100)
    goalVersion: str = Field(pattern=r'^[1-9][0-9]{0,8}$')
    milestoneTitle: str = Field(min_length=1, max_length=300)
    note: str = Field(min_length=1, max_length=1000)


class SourceChangeArgs(StrictModel):
    targetId: str = Field(min_length=1, max_length=100)
    replacementId: str = Field(min_length=1, max_length=100)
    targetHash: str = Field(pattern=r'^[a-f0-9]{64}$')
    replacementHash: str = Field(pattern=r'^[a-f0-9]{64}$')
    versions: str = Field(min_length=2, max_length=6000)
    operation: Literal['replace', 'cancel']


ARGUMENTS = {'knowledge_search': SearchArgs, 'knowledge_read': ReadArgs,
    'progress_read': StrictModel, 'task_create': TaskArgs, 'document_create': DocumentArgs,
    'task_attach': TaskEvidenceArgs, 'task_verify': TaskEvidenceArgs,
    'task_read': TaskReadArgs, 'task_update': TaskUpdateArgs, 'task_link': TaskLinkArgs,
    'source_change': SourceChangeArgs}
