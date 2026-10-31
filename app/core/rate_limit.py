"""单进程请求限流；生产多进程部署应使用共享计数。"""
from collections import defaultdict, deque
import time
from fastapi import HTTPException, Request


class AuthRateLimiter:
    """开发单进程限流；多 worker 部署应替换为共享计数存储。"""
    def __init__(self):
        self._attempts = defaultdict(deque)

    def check(self, key: str, maximum: int):
        current = time.monotonic()
        for old_key in list(self._attempts):
            bucket = self._attempts[old_key]
            while bucket and bucket[0] <= current - 60:
                bucket.popleft()
            if not bucket:
                del self._attempts[old_key]
        if key not in self._attempts and len(self._attempts) >= 4096:
            raise HTTPException(status_code=429, detail="too_many_requests")
        bucket = self._attempts[key]
        if len(bucket) >= maximum:
            raise HTTPException(status_code=429, detail="too_many_requests")
        bucket.append(current)


def limit_auth(request: Request):
    # 不信任客户端 X-Forwarded-For，不记录邮箱/密码等输入。
    ip = request.client.host if request.client else "unknown"
    request.app.state.auth_limiter.check(ip, request.app.state.settings.auth_requests_per_minute)
