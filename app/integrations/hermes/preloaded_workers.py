"""预载一次性 SDK 进程：消除会中冷启动，不复用账户请求或模型会话。"""
import asyncio
import json
import subprocess
import sys
from app.core.config import ROOT


class PreloadedWorkers:
    def __init__(self, provider):
        self.provider = provider
        self.idle = asyncio.Queue(maxsize=2)
        self.processes = set()
        self.tasks = set()
        self.closed = False

    def start(self):
        for _ in range(2): self.fill()

    def fill(self):
        if self.closed: return
        task = asyncio.create_task(self.prepare())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def prepare(self):
        from app.integrations.hermes.provider import stop_worker, lower_worker_priority
        process = None
        starting = False
        try:
            await self.provider._startup_capacity.acquire()
            starting = True
            env, _ = self.provider._prepare('live', '', {})
            process = subprocess.Popen(
                [sys.executable, str(ROOT / 'scripts/hermes_meeting_worker.py'), '--preload'],
                cwd=str(self.provider.settings.hermes_runtime_path), env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
            lower_worker_priority(process)
            self.processes.add(process)
            line = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline, 65536), 20)
            if json.loads(line) != {'type': 'ready'}: raise ValueError('worker_not_ready')
            self.provider._startup_capacity.release()
            starting = False
            await self.idle.put(process)
            process = None
        except asyncio.CancelledError:
            raise
        except Exception:
            # 预载失败不阻断服务；单次请求仍可走受限冷启动路径。
            pass
        finally:
            if starting:
                self.provider._startup_capacity.release()
            if process is not None:
                await asyncio.to_thread(stop_worker, process)
                self.processes.discard(process)
                for pipe in (process.stdin, process.stdout):
                    if pipe is not None and not pipe.closed: pipe.close()

    def take(self):
        while not self.idle.empty():
            process = self.idle.get_nowait()
            if process.poll() is None: return process
            self.processes.discard(process)
            for pipe in (process.stdin, process.stdout):
                if pipe is not None and not pipe.closed: pipe.close()
            self.fill()
        for _ in range(max(0, 2 - len(self.processes) - len(self.tasks))):
            self.fill()
        return None

    def consumed(self, process):
        if process in self.processes:
            self.processes.discard(process)
            self.fill()

    async def close(self):
        from app.integrations.hermes.provider import stop_worker
        self.closed = True
        tasks = list(self.tasks)
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for process in list(self.processes):
            await asyncio.to_thread(stop_worker, process)
            for pipe in (process.stdin, process.stdout):
                if pipe is not None and not pipe.closed: pipe.close()
        self.processes.clear()
