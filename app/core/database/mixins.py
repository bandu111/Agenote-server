"""数据库模型通用时间函数。"""
from datetime import datetime, timezone


def now() -> datetime:
    return datetime.now(timezone.utc)
