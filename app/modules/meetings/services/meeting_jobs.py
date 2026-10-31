"""让耗时模型任务立即返回任务号，适配移动端短请求超时。"""

import asyncio
import time
import uuid
import logging
from app.modules.meetings.services.meeting_archive_service import MeetingArchiveService
from app.modules.meetings.services.meeting_analysis_service import (
    MeetingAnalysisBusyError,
)
from app.modules.meetings.services.meeting_service import MeetingConflictError
from app.modules.meetings.services.transcript_polish_service import (
    TranscriptPolishService,
)


class MeetingJobs:
    """本地单进程任务队列；会议及归档落库，任务状态仅短期保存在内存。"""

    def __init__(self, database, coordinator):
        self.database = database
        self.coordinator = coordinator
        self.jobs = {}
        self.tasks = set()

    def submit(self, owner_id, client_id, revision, operation, payload=None):
        # 只清理已完成任务，避免长任务尚在运行时丢失其状态。
        stamp = time.monotonic()
        self.jobs = {
            key: value
            for key, value in self.jobs.items()
            if value["status"] in {"queued", "running"}
            or stamp - value["updated"] < 600
        }
        active = [
            job for job in self.jobs.values() if job["status"] in {"queued", "running"}
        ]
        for job in self.jobs.values():
            if (
                job["owner_id"],
                job["client_id"],
                job["revision"],
                job["operation"],
            ) == (owner_id, client_id, revision, operation) and job["status"] not in {
                "failed",
                "stale",
            }:
                return self.output(job)
        if (
            len(active) >= 8
            or sum(job["owner_id"] == owner_id for job in active) >= 2
            or len(self.jobs) >= 256
        ):
            raise MeetingAnalysisBusyError()
        job = {
            "id": uuid.uuid4().hex,
            "owner_id": owner_id,
            "client_id": client_id,
            "revision": revision,
            "operation": operation,
            "status": "queued",
            "updated": stamp,
            "result": None,
            "error": None,
            "payload": payload,
            "progress": {"stage": "queued", "percent": 0, "label": "等待整理"},
        }
        self.jobs[job["id"]] = job
        task = asyncio.create_task(self.run(job))
        job["task"] = task
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return self.output(job)

    def invalidate(self, owner_id, client_id, revision):
        """新版本到达后取消旧模型工作，落库时仍进行最终版本检查。"""
        for job in self.jobs.values():
            if (job["owner_id"] == owner_id and job["client_id"] == client_id and
                job["operation"] in {"analysis", "archive"} and job["revision"] < revision and
                job["status"] in {"queued", "running"} and job.get("task") is not asyncio.current_task()):
                job["task"].cancel()

    def remove_meeting(self, owner_id, client_id):
        for key, job in list(self.jobs.items()):
            if job['owner_id'] != owner_id or not (
                job['client_id'] == client_id or (job.get('payload') or {}).get('meeting_id') == client_id):
                continue
            if job['status'] in {'queued', 'running'}:
                job['task'].cancel()
            job.update(result=None, payload=None, preview=None, status='failed', error='meeting_deleted')
            self.jobs.pop(key, None)

    async def run(self, job):
        job["status"] = "running"
        self.signal(job)
        try:
            async with self.database.sessions() as session:
                if job["operation"] == "polish":
                    result = await TranscriptPolishService(self.coordinator).polish(
                        job["payload"]
                    )
                elif job["operation"] == "analysis":
                    async def publish_core(value):
                        job['preview'] = {**job.get('preview', {}), 'kind': 'verified_inference_only', **value}
                        job['updated'] = time.monotonic()
                        self.signal(job)
                    result = await self.coordinator.analyze(
                        session, job["owner_id"], job["client_id"], on_core_ready=publish_core
                    )
                else:
                    def publish_progress(stage, percent, label):
                        job['progress'] = {'stage': stage, 'percent': max(job['progress']['percent'], percent), 'label': label}
                        job['updated'] = time.monotonic()
                        self.signal(job)
                    result = await MeetingArchiveService(self.coordinator).create(
                        session, job["owner_id"], job["client_id"], on_progress=publish_progress
                    )
                if result.get("generated") is not False and result["revision"] != job["revision"]:
                    raise MeetingConflictError()
                job.update(status="succeeded", result=result)
                if job['operation'] == 'archive':
                    job['progress'] = {'stage': 'skipped' if result.get('generated') is False else 'completed',
                                       'percent': 0 if result.get('generated') is False else 100,
                                       'label': '会议不足一分钟，未生成纪要' if result.get('generated') is False else '纪要与后续行动已保存'}
                if job['operation'] in {'analysis', 'archive'} and self.coordinator.on_actions_published:
                    # 业务提交成功后立即唤醒通知；纪要尚未落库时不能先报完成。
                    self.coordinator.on_actions_published()
        except asyncio.CancelledError:
            job.update(status="failed", error="meeting_job_interrupted")
            raise
        except MeetingConflictError:
            job.update(status="stale", error="meeting_revision_conflict")
        except Exception as error:
            # 不把数据库、模型或网络异常详情传给客户端。
            import traceback
            chain = error
            while chain.__cause__ is not None:
                chain = chain.__cause__
            frames = [(f.name, f.lineno) for f in traceback.extract_tb(chain.__traceback__)[-6:]]
            # 仅记录类型和函数位置，禁止记录异常正文、原话、凭据及模型输出。
            logging.getLogger("yanshu.meetings").warning("meeting_job_failed type=%s cause=%s frames=%s",
                type(error).__name__, type(chain).__name__, frames)
            code = 'meeting_job_failed'
            reason = '整理失败，请稍后重试'
            if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
                code, reason = 'meeting_job_timeout', '生成请求超时，请稍后重试'
            elif type(error).__name__ == 'HermesUnavailableError':
                code, reason = 'minutes_model_unavailable', '模型服务暂不可用或返回格式不正确，请重试'
            elif isinstance(error, MeetingAnalysisBusyError):
                code, reason = 'meeting_analysis_busy', '服务繁忙，请稍后重试'
            job.update(status="failed", error=code, error_message=reason)
        finally:
            job["updated"] = time.monotonic()
            self.signal(job)

    @staticmethod
    def signal(job):
        job.setdefault('changed', asyncio.Event()).set()

    async def watch(self, owner, job_id):
        """事件唤醒推送最新快照，避免移动端每两秒轮询才看见模型增量。"""
        job = self.jobs.get(job_id)
        if job is None or job['owner_id'] != owner:
            return
        changed = job.setdefault('changed', asyncio.Event())
        deadline = time.monotonic() + 360
        while time.monotonic() < deadline:
            changed.clear()
            value = self.get(owner, job_id)
            if value is None:
                return
            yield {'event': 'job', 'data': value}
            if value['status'] not in {'queued', 'running'}:
                return
            try:
                await asyncio.wait_for(changed.wait(), timeout=15)
            except TimeoutError:
                yield {'event': 'heartbeat'}

    def get(self, owner_id, job_id):
        job = self.jobs.get(job_id)
        if (
            job is None
            or job["owner_id"] != owner_id
            or (
                job["status"] not in {"queued", "running"}
                and time.monotonic() - job["updated"] >= 600
            )
        ):
            return None
        return self.output(job)

    @staticmethod
    def output(job):
        result = {key: job[key] for key in ["id", "revision", "operation", "status", "result", "error"]}
        if job.get('operation') == 'archive':
            result['progress'] = job.get('progress', {'stage': 'queued', 'percent': 0, 'label': '等待整理'})
            result['errorMessage'] = job.get('error_message') or {
                'meeting_revision_conflict': '会议原话已更新，请重新整理',
                'meeting_job_interrupted': '整理任务已中断，请重试',
            }.get(job.get('error'))
        if job.get('preview') is not None and job['status'] in {'running', 'succeeded'}:
            result['preview'] = job['preview']
        return result

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
