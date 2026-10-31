"""固定版本 Skill 目录：原文留存，按业务边界选取规范，不执行其中的安装命令。"""

import hashlib
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=16)
def skill_content(name):
    if name not in {
        "document-to-action-items",
        "qmd",
        "decision-questionnaire",
        "one-three-one-rule",
        "weekly-review-planning",
        "proactive-agent",
        'pdf', 'docx', 'xlsx', 'powerpoint',
    }:
        raise ValueError("不支持的 Skill")
    directory = Path(__file__).parent / "skills" / name
    meta = json.loads((directory / "upstream.json").read_text(encoding="utf-8"))
    raw = (directory / "SKILL.md").read_bytes()
    if hashlib.sha256(raw).hexdigest() != meta["sha256"]:
        raise RuntimeError("Skill 校验失败")
    return raw.decode("utf-8"), {
        "name": name,
        **{k: meta[k] for k in ["commit", "sha256", "adapter"]},
    }


def document_processing_skills():
    """固定官方工作方法；底层文件仅由认证工具读取，不执行 Skill 的任意终端命令。"""
    return [skill_content(name)[1] for name in ['pdf', 'docx', 'xlsx', 'powerpoint']]


def document_processing_guidance():
    parts = []
    for name in ['pdf', 'docx', 'xlsx', 'powerpoint']:
        raw, _ = skill_content(name)
        section = raw.split('## When to Use', 1)[-1].split('\n## ', 1)[0]
        parts.append(name + ':\n' + section[:1200])
    return '\n'.join(parts) + '\n言枢适配：只使用已注册工具；不运行终端、任意路径或网络提取。旧格式转换及中文OCR由本机受限引擎提供；不伪称存在尚未开放的文档编辑工具。'


def document_skill():
    text, meta = skill_content("document-to-action-items")
    excerpt = text.split("### 3. Classify evidence", 1)[1].split("### 6.", 1)[0]
    return (
        excerpt
        + "\n言枢适配：只返回当前 Schema 的候选和原文引用，不创建正式承诺，不执行或发送。履约方仅我方/对方/未知，禁止凭乙方、客户、会议主人猜测；相对交期没有约定起点则保持未知。",
        meta,
    )


def questionnaire_skill():
    text, meta = skill_content("decision-questionnaire")
    excerpt = text.split("## When to Use", 1)[1].split("## Core Principle", 1)[0]
    excerpt += text.split("## Pitfalls", 1)[1].split("## Verification", 1)[0]
    return (
        excerpt
        + "\n言枢适配：现有业务上下文提供收件方及目标，不强制再问两轮。先查已有授权证据；只针对给定缺口返回最多两条单一问题，按阻塞影响排序。不发送消息、不生成独立问卷页面、不推断答案或批准。",
        meta,
    )


def qmd_skill():
    text, meta = skill_content("qmd")
    excerpt = text.split("## When to Use", 1)[1].split("## Prerequisites", 1)[0]
    return (
        excerpt
        + "\n言枢适配：知识检索由已绑定账号的 knowledge_search 提供，QMD 使用 LlamaIndex 授权节点。只引用返回的 nodeId 与版本，不运行安装命令、不访问任意目录或其他账号。检索命中并非批准，匹配分值不代表事实置信度。",
        meta,
    )


def followup_skill(kind):
    name = {
        "decision": "one-three-one-rule",
        "weekly": "weekly-review-planning",
        "proactive": "proactive-agent",
    }[kind]
    text, meta = skill_content(name)
    if kind == "decision":
        excerpt = text.split("## Procedure", 1)[1].split("## Example", 1)[0]
    elif kind == "weekly":
        excerpt = text.split("## Procedure", 1)[1]
    else:
        excerpt = text.split("## Verify Implementation, Not Intent", 1)[1].split(
            "## Tool Migration Checklist", 1
        )[0]
        excerpt += text.split("## Heartbeat System", 1)[1].split(
            "## Reverse Prompting", 1
        )[0]
    return (
        excerpt
        + "\n言枢适配：仅分析当前账号已授权来源，结果是建议不是事实或批准。时间窗口、调度、去重由服务端提供；不修改工作区、记忆文件、配置、交期和业务决定，不发消息，不调用外部工具，不自动执行建议。来源不够时明确缺口。",
        meta,
    )
