"""真实启动隔离的休眠子进程，验证取消不会留下 Windows 虚拟环境工作进程。"""

import asyncio
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path
import pytest
from pydantic import SecretStr
from app.integrations.hermes.provider import HermesProvider


async def test_sdk_concurrency_leaves_room_for_realtime_and_cancellation_releases_slots(monkeypatch):
    provider = HermesProvider(None)
    release = asyncio.Event()
    running, maximum, started = 0, 0, []
    async def fake(role, system, payload, **kwargs):
        nonlocal running, maximum
        running += 1
        maximum = max(maximum, running)
        started.append(role)
        try:
            await release.wait()
            yield {'type': 'done', 'value': {}}
        finally:
            running -= 1
    monkeypatch.setattr(provider, '_stream_unbounded', fake)
    async def consume(role):
        async for _ in provider._stream_once(role, '', {}): pass
    tasks = [asyncio.create_task(consume(role)) for role in ['state', 'memory', 'forecast', 'business_realtime']]
    try:
        for _ in range(100):
            if len(started) >= 3: break
            await asyncio.sleep(.01)
        assert maximum == 3
        assert 'business_realtime' in started
        assert len(set(started) & {'state', 'memory', 'forecast'}) == 2
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks[1:]), 2)
        assert running == 0 and maximum <= 3
    finally:
        release.set()
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_cancelling_provider_reaps_owned_process_tree(monkeypatch, tmp_path):
    psutil = pytest.importorskip("psutil")
    original = subprocess.Popen
    processes = []
    def launch(_args, **kwargs):
        process = original([sys.executable, "-c", "import time; time.sleep(40)"], **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr("app.integrations.hermes.provider.subprocess.Popen", launch)
    settings = SimpleNamespace(deepseek_api_key=SecretStr("synthetic-key-not-real"), deepseek_model="test-only",
        hermes_runtime_path=tmp_path, hermes_timeout_seconds=50)
    task = asyncio.create_task(HermesProvider(settings).run("test", "", {}))
    while not processes:
        await asyncio.sleep(.01)
    await asyncio.sleep(.4)
    children = psutil.Process(processes[0].pid).children(recursive=True)
    # taskkill 自己也需要创建进程，取消前恢复真正的 Popen。
    monkeypatch.setattr("app.integrations.hermes.provider.subprocess.Popen", original)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].poll() is not None
    for child in children:
        child.wait(timeout=5)
        assert not child.is_running()


async def test_preloaded_workers_are_one_shot_and_shutdown_reaps_idle_processes(monkeypatch, tmp_path):
    original = subprocess.Popen
    script = ('import sys,json; print(json.dumps({"type":"ready"}),flush=True); '
        'task=json.load(sys.stdin); print(json.dumps({"type":"delta","text":task["input"]}),flush=True); '
        'print(json.dumps({"type":"done","ok":True,"response":"{}"}),flush=True)')
    def launch(args, **kwargs):
        return original([sys.executable, '-c', script] if args[0] == sys.executable else args, **kwargs)
    monkeypatch.setattr('app.integrations.hermes.provider.subprocess.Popen', launch)
    settings = SimpleNamespace(deepseek_api_key=SecretStr('synthetic-key-not-real'), deepseek_model='test-only',
        hermes_runtime_path=tmp_path, hermes_timeout_seconds=50)
    provider = HermesProvider(settings)
    provider.start_stream_workers()
    pool = provider.stream_workers
    try:
        async with asyncio.timeout(5):
            while pool.idle.qsize() != 2: await asyncio.sleep(.01)
        original_workers = set(pool.processes)
        events = [e async for e in provider.stream('live', '', {'owner': 'a'})]
        assert events[0]['text'] == '{"owner": "a"}' and events[-1]['type'] == 'done'
        assert len(original_workers & pool.processes) == 1
        assert len([p for p in original_workers if p.poll() is not None]) == 1
    finally:
        all_workers = set(pool.processes) | original_workers
        await provider.close()
    assert not pool.tasks and not pool.processes
    assert all(p.poll() is not None for p in all_workers)


async def test_stream_delivers_child_prefix_before_exit_and_close_reaps_worker(monkeypatch, tmp_path):
    original = subprocess.Popen
    processes = []
    # 不调用模型；真实管道先输出增量，然后保持运行，验证不会等进程结束才返回。
    script = 'import sys,json,time; json.load(sys.stdin); print(json.dumps({"type":"delta","text":"prefix"}),flush=True); time.sleep(40)'
    def launch(_args, **kwargs):
        process = original([sys.executable, '-c', script], **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr('app.integrations.hermes.provider.subprocess.Popen', launch)
    settings = SimpleNamespace(deepseek_api_key=SecretStr('synthetic-key-not-real'), deepseek_model='test-only',
        hermes_runtime_path=tmp_path, hermes_timeout_seconds=50)
    iterator = HermesProvider(settings).stream('live', '', {})
    try:
        assert await asyncio.wait_for(anext(iterator), 4) == {'type': 'delta', 'text': 'prefix'}
        assert processes[0].poll() is None
    finally:
        monkeypatch.setattr('app.integrations.hermes.provider.subprocess.Popen', original)
        await iterator.aclose()
    assert processes[0].poll() is not None

async def test_worker_ready_phase_is_telemetry_not_display_text(monkeypatch,tmp_path):
    original=subprocess.Popen
    script=('import sys,json; json.load(sys.stdin); '
        'print(json.dumps({"type":"generation_ready"}),flush=True); '
        'print(json.dumps({"type":"delta","text":"{}"}),flush=True); '
        'print(json.dumps({"type":"done","ok":True,"response":"{}"}),flush=True)')
    def launch(args,**kwargs): return original([sys.executable,'-c',script],**kwargs)
    monkeypatch.setattr('app.integrations.hermes.provider.subprocess.Popen',launch)
    settings=SimpleNamespace(deepseek_api_key=SecretStr('synthetic'),deepseek_model='test-only',
        hermes_runtime_path=tmp_path,hermes_timeout_seconds=30)
    values=[e async for e in HermesProvider(settings).stream('live','',{})]
    assert [e['type'] for e in values]==['delta','done']
    assert values[-1]['runtime']['agentReadySeconds'] is not None
    assert values[-1]['runtime']['firstTextSeconds'] is not None
