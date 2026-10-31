"""极光凭据仅在服务端使用；推送只携带通用提醒，不发送会议原文。"""
import httpx


class JPushClient:
    def __init__(self, settings):
        self.settings = settings

    @property
    def configured(self):
        return bool(self.settings.jpush_app_key and self.settings.jpush_master_secret)

    async def send(self, registration_ids, event_id, completed=False):
        if not self.configured:
            raise RuntimeError("jpush_not_configured")
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.post("https://api.jpush.cn/v3/push",
                auth=(self.settings.jpush_app_key, self.settings.jpush_master_secret.get_secret_value()),
                json={"platform": ["android"],
                    "audience": {"registration_id": registration_ids},
                    "notification": {"android": {
                        "alert": "点击查看处理结果" if completed else "点击查看并决定是否允许",
                        "title": "言枢后台任务已完成" if completed else "言枢有待确认事项",
                        "extras": {"event_id": event_id, "route": "/notifications"}}},
                    "options": {"time_to_live": 3600, "override_msg_id": 0}})
            response.raise_for_status()
            if not response.json().get("msg_id"):
                raise RuntimeError("jpush_response_invalid")
