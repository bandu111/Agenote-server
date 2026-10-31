"""录音流的认证、并发租约与供应商生命周期；不持久化音频。"""
import asyncio
import hashlib
import json
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
import re
from fastapi import WebSocketDisconnect
from sqlalchemy import text
from websockets.asyncio.client import connect
from app.modules.identity.services.auth_service import AuthService
from app.modules.identity.services.token_service import read_access
from app.modules.meetings.services.tencent_asr import AsrProtocolError, signed_url, transcript_events
from app.modules.meetings.services.speaker_context import SpeakerContextStore
from app.modules.meetings.services.asr_speaker_stats import AsrSpeakerStats
from app.modules.meetings.services.transcript_highlights import HighlightPipeline, HighlightMetrics
from app.modules.voiceprints.realtime import RealtimeVoiceprintSession


@asynccontextmanager
async def owner_stream_lock(database, owner_id):
    # PostgreSQL 会话级锁跨进程生效；连接关闭时自动释放，避免崩溃后的永久占用。
    lock_id = int.from_bytes(hashlib.sha256(("asr:" + owner_id).encode()).digest()[:8], "big", signed=True)
    async with database.engine.connect() as connection:
        locked = await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_id})
        if not locked:
            raise AsrProtocolError("asr_account_busy")
        slot = None
        try:
            # 全局保留十路供应商余量；所有进程共享最多四十路。
            for number in range(40):
                candidate = -8000000000000000000 + number
                if await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": candidate}):
                    slot = candidate
                    break
            if slot is None:
                raise AsrProtocolError("asr_capacity_busy")
            yield
        finally:
            if slot is not None:
                await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": slot})
            await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_id})


async def authenticate(socket, claims):
    async with socket.app.state.database.sessions() as db:
        await AuthService(db, socket.app.state.settings).authenticate(claims["sub"], claims["sid"])


def highlight_pipeline(socket, claims):
    settings = socket.app.state.settings
    pipeline = None
    if settings.transcript_highlights_enabled and (
        (settings.qwen_workers_enabled and settings.dashscope_api_key) or settings.deepseek_api_key
    ):
        registry = socket.app.state.highlight_metrics
        metrics = registry.setdefault(claims['sub'], HighlightMetrics())
        registry.move_to_end(claims['sub'])
        while len(registry) > 128:
            registry.popitem(last=False)
        pipeline = HighlightPipeline(settings, socket.send_json, socket.app.state.highlight_slots, metrics)
    return pipeline


async def relay(socket, upstream, claims, context_store=None, speaker_scope=None, pipeline=None, identity=None):
    started = time.monotonic()
    speaker_stats = AsrSpeakerStats()
    received_bytes = 0
    ending = asyncio.Event()
    audio_arrivals = deque(maxlen=4096)

    async def send_audio():
        nonlocal received_bytes
        while True:
            message = await asyncio.wait_for(socket.receive(), 5)
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect()
            audio = message.get("bytes")
            if audio is not None:
                if ending.is_set() or not audio or len(audio) > 6400 or len(audio) % 2:
                    raise AsrProtocolError("asr_invalid_audio")
                received_bytes += len(audio)
                audio_arrivals.append((received_bytes / 32, time.monotonic()))
                if received_bytes / 32000 > time.monotonic() - started + 1:
                    raise AsrProtocolError("asr_audio_too_fast")
                if identity:
                    identity.feed(audio)
                await upstream.send(audio)
            elif message.get("text") == '{"type":"end"}':
                ending.set()
                await upstream.send('{"type":"end"}')
                return
            elif pipeline and message.get('text') and len(message['text']) <= 512:
                try:
                    control = json.loads(message['text'])
                except ValueError:
                    raise AsrProtocolError('asr_invalid_control')
                if not isinstance(control, dict) or control.get('type') != 'highlight_displayed':
                    raise AsrProtocolError('asr_invalid_control')
                pipeline.displayed(control)
            else:
                raise AsrProtocolError("asr_invalid_control")

    async def receive_text():
        stable = {}
        while True:
            # 长时间静音也会持续上传 PCM，但供应商可能没有文字回调。
            # 网络存活交给 WebSocket ping；结束刷尾由父任务的 20 秒期限保护。
            payload = json.loads(await upstream.recv())
            stable_at = time.monotonic()
            speaker_stats.observe_provider(payload)
            if context_store:
                await context_store.save(speaker_scope, payload)
            for event in transcript_events(payload):
                previous = stable.get(event["segment"])
                if previous is not None:
                    # 最终原文不重写；后续聚类更正只发布有版本的归属事件。
                    if previous.get("speaker_id") != event.get("speaker_id"):
                        previous["speaker_id"] = event.get("speaker_id")
                        previous["speaker_revision"] += 1
                        update = {**previous, "type": "speaker_update"}
                        await socket.send_json(update)
                        speaker_stats.observe_forwarded(update)
                        if identity:
                            await identity.observe_safely(update)
                    continue
                event["speaker_revision"] = 0
                if event["is_final"]:
                    stable[event["segment"]] = dict(event)
                await socket.send_json(event)
                speaker_stats.observe_forwarded(event)
                # 原有文字先发出，声纹仅提交候选，不在这里等待模型计算。
                if identity:
                    await identity.observe_safely(event)
                if pipeline and event['is_final']:
                    arrival = next((at for end, at in audio_arrivals if end >= event['end_ms']), None)
                    pipeline.submit(event, arrival, stable_at=stable_at)
            if payload.get("final") == 1:
                if pipeline:
                    await pipeline.close(drain=True)
                await socket.send_json({"type": "done"})
                return

    async def check_session():
        while True:
            await asyncio.sleep(30)
            try:
                await authenticate(socket, claims)
            except Exception as error:
                if identity:
                    identity.diagnostic('authentication_failed', error_type=type(error).__name__)
                raise

    tasks = [asyncio.create_task(fn()) for fn in (send_audio, receive_text, check_session)]
    try:
        # 上传结束后仍等待腾讯云刷尾；任何失败都取消同组任务。
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if tasks[1] not in done:
            await asyncio.wait_for(tasks[1], 20)
        if identity:
            identity.diagnostic('stream_finished', reason='provider_done')
    except BaseException as error:
        # 不打印异常正文，避免供应商地址、令牌或转写内容进入诊断日志。
        if identity:
            identity.diagnostic('stream_interrupted', error_type=type(error).__name__)
        raise
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if pipeline:
            await pipeline.close()
        if identity:
            await identity.close()
        speaker_stats.log()


async def run_asr_stream(socket):
    settings = socket.app.state.settings
    try:
        authorization = socket.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise ValueError("missing bearer")
        claims = read_access(authorization[7:], settings)
        # client_id 仅为本次录音关联标识，不用于选择或修改他人的会议。
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", socket.query_params.get("client_id", "")):
            raise ValueError("invalid meeting id")
        await authenticate(socket, claims)
    except Exception:
        await socket.close(code=4401)
        return
    await socket.accept()
    identity = None
    try:
        async with owner_stream_lock(socket.app.state.database, claims["sub"]):
            store = SpeakerContextStore(socket.app.state.database, claims["sub"], socket.query_params["client_id"])
            previous = await store.load() if settings.tencent_asr_engine_model_type == "16k_zh_en_speaker_2.0" else None
            scope = previous[0] if previous else uuid.uuid4().hex
            async with connect(signed_url(settings, previous[1] if previous else None), proxy=None, open_timeout=10,
                               max_size=1048576, max_queue=16) as upstream:
                hello = json.loads(await asyncio.wait_for(upstream.recv(), 10))
                try:
                    transcript_events(hello)
                except AsrProtocolError:
                    # 供应商拒绝续接时撤销上下文，下次使用新作用域，绝不强行合并身份。
                    if previous:
                        await store.clear()
                    raise
                await store.save(scope, hello)
                if getattr(socket.app.state, 'voiceprints', None):
                    identity = await RealtimeVoiceprintSession.create(socket.app.state.voiceprints, claims['sub'], scope, socket.send_json)
                    from app.modules.meetings.services.speaker_identity import MeetingSpeakerIdentities
                    identity.persistence = MeetingSpeakerIdentities(socket.app.state.database, claims['sub'], socket.query_params['client_id'])
                    from app.modules.voiceprints.realtime import SpeakerEvidence
                    snapshot = await identity.persistence.snapshot()
                    for number in range(10):
                        saved = snapshot['identities'].get(f'asr:{scope}:{number}', {}).get('identity')
                        if saved in ('self', 'other'):
                            identity.speakers[number] = SpeakerEvidence(identity=saved)
                await socket.send_json({"type": "ready", "provider": "tencent", "sample_rate": 16000, "speaker_scope": scope,
                                        **(identity.ready_fields() if identity else {})})
                if identity:
                    identity.start()
                async with asyncio.timeout(settings.asr_max_stream_seconds):
                    await relay(socket, upstream, claims, store, scope, highlight_pipeline(socket, claims), identity)
        await socket.close(code=1000)
    except WebSocketDisconnect:
        return
    except Exception as error:
        # 异常正文可能含签名 URL，禁止直接记日志或透传供应商正文。
        code = str(error) if isinstance(error, AsrProtocolError) else "asr_stream_interrupted"
        try:
            await socket.send_json({"type": "error", "code": code})
            await socket.close(code=1011)
        except (RuntimeError, WebSocketDisconnect):
            pass
    finally:
        if identity:
            await identity.close()
