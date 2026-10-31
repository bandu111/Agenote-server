"""新增普通发言不废弃已有审批；纠错、缺失及未审查的变更仍阻止执行。"""
import re
from app.modules.meetings.services.business_guard import fingerprint


def snapshot_current(prepared, sources):
    if not prepared:
        return False
    current = {u['id']: fingerprint([u['raw'], u.get('correction'), u.get('withdrawn')]) for u in sources}
    if any(current.get(key) != value for key, value in prepared.items()):
        return False
    # 这里只做保守的执行暂停筛选，不据关键词撤回业务记录。
    for source in sources:
        if source['id'] in prepared:
            continue
        words = source.get('correction') or source['raw']
        if source.get('withdrawn') or re.search(r'取消|撤回|改期|改为|改成|调整到|调整为|延后|提前|不再|不要|作废|变更', words):
            return False
    return True
