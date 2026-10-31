"""通过受限子进程调用官方 Hermes SDK，密钥不进入命令行和日志。"""

import asyncio
import json
import os
import subprocess
import logging
import sys
from pathlib import Path
from app.core.config import Settings, ROOT
from app.integrations.hermes.model_policy import select_model
from app.integrations.hermes.request_slots import RequestSlots


class HermesUnavailableError(Exception):
    """模型配置缺失、运行时失败或返回格式不可用。"""


def lower_worker_priority(process):
    """只降低自己创建的 SDK 进程优先级，优先保证 API 和音频转发响应。"""
    if sys.platform != 'win32':
        try:
            os.setpriority(os.PRIO_PROCESS, process.pid, 10)
        except (OSError, AttributeError):
            pass


def stop_worker(process):
    """终止自己启动的隔离工作进程；Windows 虚拟环境还会生成解释器子进程。"""
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


class HermesProvider:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.stream_workers = None
        self.live_text_model = None
        self.role_text_model = None
        # 三个网络名额分别保留给风险、普通后台和实时业务，不增加 CPU 并行度。
        self._sdk_capacity = asyncio.Semaphore(3)
        self._background_capacity = RequestSlots(1)
        self._risk_capacity = RequestSlots(1)
        self._startup_capacity = asyncio.Semaphore(1)

    def start_stream_workers(self):
        if self.settings.deepseek_api_key is None or self.stream_workers is not None: return
        from app.integrations.hermes.live_model import LiveTextModel
        self.live_text_model = LiveTextModel(self.settings)
        from app.integrations.hermes.preloaded_workers import PreloadedWorkers
        self.stream_workers = PreloadedWorkers(self)
        self.stream_workers.start()

    async def close(self):
        if self.role_text_model is not None:
            await self.role_text_model.close()
            self.role_text_model = None
        if self.live_text_model is not None:
            await self.live_text_model.close()
            self.live_text_model = None
        if self.stream_workers is not None:
            await self.stream_workers.close()
            self.stream_workers = None

    def _prepare(self, role, system, payload, *, fallback=False):
        if self.settings.deepseek_api_key is None:
            raise HermesUnavailableError()
        env = os.environ.copy()
        env["DEEPSEEK_API_KEY"] = self.settings.deepseek_api_key.get_secret_value()
        minimax_key = getattr(self.settings, 'minimax_api_key', None)
        if minimax_key is not None:
            env['MINIMAX_API_KEY'] = minimax_key.get_secret_value()
        env["HERMES_HOME"] = str(ROOT / "storage" / "hermes-meeting-runtime")
        env["PYTHONIOENCODING"] = "utf-8"
        for name in ['OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS']:
            env[name] = '1'
        env['TOKENIZERS_PARALLELISM'] = 'false'
        env["PYTHONPATH"] = str(self.settings.hermes_runtime_path)
        env["NO_PROXY"] = "api.deepseek.com,api.minimax.io,api.minimaxi.com,127.0.0.1,localhost"
        route = select_model(self.settings, role, fallback=fallback)
        task = {
            "model": route.model,
            'base_url': route.base_url,
            'key_env': route.key_env,
            'reasoning': route.reasoning,
            'max_tokens': route.max_tokens,
            "system": system,
            "input": json.dumps(payload, ensure_ascii=False),
        }
        return env, task

    async def run(self, role: str, system: str, payload: dict) -> dict:
        """后台角色也复用预载传输，避免每次完整导入 SDK；结果仍按各自 Schema 校验。"""
        from contextlib import aclosing
        async with aclosing(self.stream(role, system, payload)) as events:
            async for event in events:
                if event['type'] == 'done': return event['value']
        raise HermesUnavailableError()

    async def stream(self, role: str, system: str, payload: dict):
        from contextlib import aclosing
        started = asyncio.get_running_loop().time()
        _, task = self._prepare(role, system, payload)
        selected = {'MINIMAX_API_KEY': 'minimax', 'DASHSCOPE_API_KEY': 'qwen'}.get(task['key_env'], 'deepseek')
        actual_model, outcome = task['model'], 'failed'
        metrics = {}
        try:
            if role == 'live_text':
                if self.live_text_model is None:
                    from app.integrations.hermes.live_model import LiveTextModel
                    self.live_text_model = LiveTextModel(self.settings)
                async with aclosing(self.live_text_model.stream(system, payload)) as events:
                    async for event in events:
                        if event['type'] == 'done':
                            outcome = 'ok'
                            metrics.update(event.get('runtime', {}))
                        yield event
                return
            async with aclosing(self._stream_once(role, system, payload)) as events:
                async for event in events:
                    if event['type'] == 'done':
                        outcome = 'ok'
                        metrics.update(event.get('runtime', {}))
                    yield event
        except HermesUnavailableError:
            if selected != 'minimax': raise
            # 提取 Worker 尚未发布业务结果；失败后回到原主模型，绝不降低审批防线。
            selected = 'deepseek_fallback'
            actual_model = self.settings.deepseek_model
            async with aclosing(self._stream_once(role, system, payload, fallback=True)) as events:
                async for event in events:
                    if event['type'] == 'done':
                        outcome = 'ok'
                        metrics.update(event.get('runtime', {}))
                    yield event
        finally:
            from app.core.logging import model_logger
            model_logger().info('model_run', extra={'role': role, 'provider': selected,
                'model': actual_model, 'outcome': outcome,
                'seconds': round(asyncio.get_running_loop().time() - started, 3),
                **{k: metrics.get(k) for k in ['queueSeconds', 'agentReadySeconds', 'firstTextSeconds', 'completeSeconds']}})

    async def _stream_once(self, role: str, system: str, payload: dict, *, fallback=False):
        from contextlib import aclosing
        queued_at = asyncio.get_running_loop().time()
        async def run():
            async with self._sdk_capacity:
                queue_seconds = round(asyncio.get_running_loop().time() - queued_at, 3)
                async with aclosing(self._stream_unbounded(role, system, payload, fallback=fallback)) as events:
                    async for event in events:
                        if event['type'] == 'done':
                            event = {**event, 'runtime': {**event.get('runtime', {}), 'queueSeconds': queue_seconds}}
                        yield event
        if role in {'live', 'business_realtime', 'business_verify', 'meeting_title'}:
            async with aclosing(run()) as events:
                async for event in events: yield event
        elif role == 'risk':
            async with self._risk_capacity.slot(-1 if payload.get('preview_only') else 0):
                async with aclosing(run()) as events:
                    async for event in events: yield event
        else:
            priority = {'verify': 0, 'minutes': 1, 'state': 2, 'commitment': 3, 'planner': 3,
                'controlled_tools': 3, 'risk': 1, 'memory': 5, 'forecast': 8, 'questionnaire': 20}.get(role, 10)
            async with self._background_capacity.slot(priority):
                async with aclosing(run()) as events:
                    async for event in events: yield event

    async def _stream_unbounded(self, role: str, system: str, payload: dict, *, fallback=False):
        """SDK 真正的文本增量；取消 HTTP 流时终止对应子进程，不遗留模型请求。"""
        from app.integrations.hermes.role_model import LIGHT_ROLES
        if role in LIGHT_ROLES:
            from contextlib import aclosing
            from app.integrations.hermes.role_model import RoleTextModel
            if self.role_text_model is None:
                self.role_text_model = RoleTextModel(self.settings)
            try:
                async with asyncio.timeout(self.settings.hermes_timeout_seconds):
                    async with aclosing(self.role_text_model.stream(role, system, payload, fallback=fallback)) as events:
                        async for event in events:
                            yield event
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise HermesUnavailableError() from error
            return
        env, task = self._prepare(role, system, payload, fallback=fallback)
        task['stream'] = True
        process = None
        started = asyncio.get_running_loop().time()
        ready_at, first_text = None, None
        starting = False
        try:
            await self._startup_capacity.acquire()
            starting = True
            process = self.stream_workers.take() if self.stream_workers is not None else None
            preloaded = process is not None
            if process is None: process = subprocess.Popen(
                [sys.executable, str(ROOT / 'scripts/hermes_meeting_worker.py')],
                cwd=str(self.settings.hermes_runtime_path), env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
            lower_worker_priority(process)
            def write_input():
                process.stdin.write(json.dumps(task).encode())
                process.stdin.close()
            await asyncio.to_thread(write_input)
            limit = 30 if role in {'live', 'live_text', 'business_realtime', 'business_verify'} else self.settings.hermes_timeout_seconds
            deadline = asyncio.get_running_loop().time() + min(self.settings.hermes_timeout_seconds, limit)
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0: raise TimeoutError()
                line = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline, 65536), remaining)
                if not line: raise HermesUnavailableError()
                event = json.loads(line.decode('utf-8'))
                if event.get('type') == 'generation_ready':
                    ready_at = round(asyncio.get_running_loop().time() - started, 3)
                    if starting:
                        self._startup_capacity.release()
                        starting = False
                    continue
                if event.get('type') == 'delta':
                    text = event.get('text')
                    if isinstance(text, str) and text:
                        if first_text is None:
                            first_text = round(asyncio.get_running_loop().time() - started, 3)
                        yield {'type': 'delta', 'text': text}
                elif event.get('type') == 'done' and event.get('ok'):
                    raw = event['response'].strip()
                    if raw.startswith('```'): raw = raw.split('\n', 1)[1].rsplit('```', 1)[0]
                    value = {'text': raw} if role == 'live_text' else json.loads(raw)
                    if not isinstance(value, dict): raise HermesUnavailableError()
                    yield {'type': 'done', 'value': value, 'runtime': {
                        'model': task['model'],
                        'preloaded': preloaded, 'agentReadySeconds': ready_at, 'firstTextSeconds': first_text,
                        'completeSeconds': round(asyncio.get_running_loop().time() - started, 3),
                        'provider': 'minimax' if task['key_env'] == 'MINIMAX_API_KEY' else 'deepseek'}}
                    return
                else:
                    raise HermesUnavailableError()
        except (ValueError, KeyError, OSError, TimeoutError) as error:
            raise HermesUnavailableError() from error
        finally:
            if starting:
                self._startup_capacity.release()
            if process is not None:
                if process.poll() is None: await asyncio.to_thread(stop_worker, process)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None and not pipe.closed: pipe.close()
                if self.stream_workers is not None: self.stream_workers.consumed(process)
