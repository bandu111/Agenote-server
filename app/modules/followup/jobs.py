"""Heartbeat 只检查业务变化；LangGraph 执行受限分析，不自主改写系统。"""

import asyncio, logging, time
from datetime import timedelta
from typing import TypedDict
from sqlalchemy import select, update
from langgraph.graph import StateGraph, START, END
from app.modules.followup.service import FollowupService, clock
from app.modules.followup.context import SHANGHAI
from app.modules.followup.models import FollowupRun
from app.modules.followup.agent import FollowupAgent, FollowupOutputError
from app.modules.identity.models.identity import User

logger = logging.getLogger(__name__)


class FollowupJobs:
    def __init__(self, database, provider):
        self.database = database
        self.agent = FollowupAgent(provider)
        self.task = None

        class State(TypedDict):
            kind: str
            data: dict
            result: dict

        graph = StateGraph(State)

        async def policy(state):
            if not state["data"].get("sources"):
                raise ValueError("missing_followup_evidence")
            return {}

        async def draft(state):
            return {"result": await self.agent.analyze(state["kind"], state["data"])}

        async def verify(state):
            if state["result"].get("advisory") is not True:
                raise ValueError("not_advisory")
            return {}

        graph.add_node("policy", policy)
        graph.add_node("draft", draft)
        graph.add_node("verify", verify)
        graph.add_edge(START, "policy")
        graph.add_edge("policy", "draft")
        graph.add_edge("draft", "verify")
        graph.add_edge("verify", END)
        self.graph = graph.compile()

    def start(self):
        self.task = asyncio.create_task(self.loop())

    async def loop(self):
        last_scan = 0
        while True:
            try:
                if time.monotonic() - last_scan >= 60:
                    await self.scan()
                    last_scan = time.monotonic()
                if await self.process_one():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "followup_unavailable", extra={"error_type": type(error).__name__}
                )
            await asyncio.sleep(10)

    async def scan(self, now=None):
        now = now or clock()
        local = now.astimezone(SHANGHAI)
        # 夜间静默；周任务只补跑本周一的固定窗口，不在每次启动时生成周报。
        if not 8 <= local.hour < 22:
            return
        async with self.database.sessions() as session:
            owners = list(
                await session.scalars(select(User.id).where(User.is_active.is_(True)))
            )
        for owner in owners:
            async with self.database.sessions() as session:
                service = FollowupService(session)
                for kind in ["decision", "proactive"] + (
                    ["weekly"] if local.weekday() == 0 and local.hour >= 9 else []
                ):
                    await service.enqueue(owner, kind, now, automatic=True)
                await session.commit()

    async def process_one(self):
        now = clock()
        async with self.database.sessions() as session:
            await session.execute(
                update(FollowupRun)
                .where(
                    FollowupRun.status == "running",
                    FollowupRun.updated_at < now - timedelta(minutes=5),
                )
                .values(status="queued")
            )
            row = await session.scalar(
                select(FollowupRun)
                .where(FollowupRun.status == "queued")
                .order_by(FollowupRun.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not row:
                await session.commit()
                return False
            key, owner = row.id, row.owner_id
            if row.attempts >= 3:
                row.status = "failed"
                await session.commit()
                return True
            if not await FollowupService(session).current(row):
                row.status = "stale"
                await session.commit()
                return True
            row.status = "running"
            row.attempts += 1
            row.updated_at = now
            kind, data = row.kind, {**row.input["data"], "assessment_at": row.input["clock"]}
            await session.commit()
        try:
            state = await asyncio.wait_for(
                self.graph.ainvoke({"kind": kind, "data": data}), timeout=180
            )
            result, status = state["result"], "succeeded"
        except asyncio.CancelledError:
            async with self.database.sessions() as session:
                await session.execute(
                    update(FollowupRun)
                    .where(FollowupRun.id == key, FollowupRun.status == "running")
                    .values(status="queued")
                )
                await session.commit()
            raise
        except Exception as error:
            result, status = {"error": type(error).__name__}, "failed"
            if isinstance(error, FollowupOutputError):
                result["format_validation"] = error.diagnostics
        async with self.database.sessions() as session:
            await lock_library(session, owner)
            row = await session.get(FollowupRun, key, with_for_update=True)
            if row:
                service = FollowupService(session)
                if not await service.current(row, lock=True):
                    row.status = "stale"
                else:
                    row.status, row.result = status, result
                    if kind == "weekly" and status == "succeeded":
                        await service.archive(row)
                await session.commit()
        return True

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


async def lock_library(session, owner):
    from app.modules.knowledge.repositories.knowledge_repository import (
        KnowledgeRepository,
    )

    await KnowledgeRepository(session).library(owner, lock=True)
