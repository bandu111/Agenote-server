"""固定版本的官方 Skill 适配；只加载提取步骤，不开放文件或执行工具。"""

import hashlib
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def meeting_skill():
    directory = Path(__file__).parent / "skills" / "meeting-action-items"
    metadata = json.loads((directory / "upstream.json").read_text(encoding="utf-8"))
    raw = (directory / "SKILL.md").read_bytes()
    if hashlib.sha256(raw).hexdigest() != metadata["sha256"]:
        raise RuntimeError("会议 Skill 内容与固定版本不一致")
    # 复用证据分类和行动字段规范；连接器写入由言枢 Service/LangGraph 独占。
    text = raw.decode("utf-8")
    extraction = text.split("### 2. Separate evidence types", 1)[1].split(
        "### 4. Reconcile existing records", 1
    )[0]
    adapter = """
言枢适配边界：上述 Skill 是提取质量规范，输出仍严格遵守本角色 Schema。
owner 在言枢映射为我方/对方/未知履约方，不新增职位或真实账户推断。
未知字段使用 Schema 的 null/unknown，不用字符串 unresolved 替代日期。
日期逐字摘录原话，明确日期不能遗漏；仅作为提议的改期不得覆盖旧日期。
工具、文件读取、外部写入和审批均由已认证的言枢业务层处理，本角色不执行。
候选不是正式批准、验收或身份认证。原始转写和来源引用保持独立。
"""
    return extraction + adapter, {
        "name": "meeting-action-items",
        "commit": metadata["commit"],
        "sha256": metadata["sha256"],
        "adapter": metadata["adapter"],
    }
