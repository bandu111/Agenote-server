"""One durable receive loop for per-user Weixin input and result delivery."""
import asyncio
import hashlib
import logging
from datetime import timedelta

from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import IntegrityError

from app.core.database.mixins import now
from app.modules.identity.models.identity import User
from app.modules.actions.models import ActionRun
from app.modules.meetings.models.archive import MeetingArchive
from app.modules.meetings.models.meeting import Meeting
from app.modules.outcomes.assistant_query import KIND as ASSISTANT_KIND, classify_local, enqueue_query
from app.modules.outcomes.jobs import enqueue
from app.modules.outcomes.meeting_preparation import KIND as PREPARATION_KIND
from app.modules.outcomes.models import OutcomeRun
from app.modules.outcomes.wechat_models import WechatBinding
from app.modules.outcomes.wechat_service import cipher_for, peer_hash, verified_credentials
from app.modules.outcomes.weixin_transport import ChannelError, allowed_base


def _reply(row, message_id, text_value, suffix="reply"):
    key = hashlib.sha256((message_id + ":" + suffix).encode()).hexdigest()
    pending = list(row.state.get("pendingReplies", []))
    if not any(item.get("id") == key for item in pending):
        pending.append({"id": key, "text": text_value[:1800], "status": "pending", "attempts": 0})
    row.state = {**row.state, "pendingReplies": pending[-20:],
                 "reply": {"id": key, "text": text_value[:1800]}}


def _assistant_message(result):
    message = "Agenote · 资料问答\n" + str(result.get("answer") or "当前资料不足，无法回答。")[:1000]
    sources = result.get("sources") or []
    if sources:
        lines = []
        for index, source in enumerate(sources[:6], 1):
            if source.get("kind") == "document":
                detail = "文档：" + str(source.get("title") or "资料")
                if source.get("versionId"):
                    detail += " · 版本 " + str(source["versionId"])
                if source.get("page") is not None:
                    detail += " · 第" + str(source["page"]) + "页"
                if source.get("source"):
                    detail += " · " + str(source["source"])
            elif source.get("kind") == "utterance":
                detail = "会议：" + str(source.get("meetingTitle") or "录音会议")
                detail += " · " + str(source.get("occurredDate") or "日期未知")
                detail += " · 原话 " + str(source.get("timestamp") or "00:00:00")
            elif source.get("kind") == "meeting":
                detail = "会议：" + str(source.get("meetingTitle") or source.get("title") or "录音会议")
                detail += " · " + str(source.get("occurredDate") or "日期未知")
            else:
                detail = "AI 派生结果：" + str(source.get("title") or "分析结果")
            lines.append(str(index) + ". " + detail)
        message += "\n来源：\n" + "\n".join(lines)
    else:
        message += "\n来源：当前授权范围内没有可核验来源。"
    if result.get("clarification"):
        message += "\n可补充：" + str(result["clarification"])[:260]
    return message[:1800]


def _preparation_message(result):
    meeting = result.get("meeting") or {}
    message = "Agenote · 会前准备\n会议：" + str(meeting.get("title") or "会议")[:120]
    message += "\n时间：" + str(meeting.get("start") or "待确认")[:80]
    message += "\n准备：" + str(result.get("notification_summary") or result.get("summary") or "")[:500]
    message += "\n完整内容：请在 Agenote 对应日程中查看。"
    return message[:1800]


class WechatJobs:
    def __init__(self, database, settings, transport):
        self.database, self.settings, self.transport = database, settings, transport
        self.task = None

    def start(self):
        # This remains the application's only Weixin get-updates loop.
        if self.settings.wechat_binding_key and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self.loop())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.transport.close()

    async def loop(self):
        while True:
            try:
                await self.process_one()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logging.getLogger(__name__).warning("wechat_worker_error", extra={"error_type": type(error).__name__})
            await asyncio.sleep(0.8)

    async def _preparation_gate(self, session, binding, report):
        from datetime import datetime
        stamp = now()
        if not getattr(self.settings, "meeting_preparation_enabled", True):
            return "suppressed", "meeting_preparation_disabled"
        send_text = report.content.get("sendAt")
        if not send_text:
            return "expired", "missing_send_at"
        try:
            send_at = datetime.fromisoformat(send_text)
        except ValueError:
            return "expired", "invalid_send_at"
        if stamp < send_at:
            return "wait", None
        recovery = int(report.content.get("policy", {}).get(
            "recoveryMinutes", getattr(self.settings, "meeting_preparation_recovery_minutes", 15)
        ))
        if stamp > send_at + timedelta(minutes=recovery):
            return "expired", "recovery_window_elapsed"
        prefs = (binding.state or {}).get("preferences", {}) if binding else {}
        if not binding or binding.status != "bound":
            return "suppressed", "wechat_not_bound"
        if not binding.state.get("generation") or not binding.state.get("contextReady", bool((binding.encrypted or ""))):
            return "suppressed", "wechat_context_unavailable"
        if prefs.get("paused"):
            return "suppressed", "wechat_paused"
        if prefs.get("preMeeting", True) is not True:
            return "suppressed", "pre_meeting_disabled"
        from app.modules.outcomes.meeting_preparation import preparation_context
        action, reason = await preparation_context(session, report, at=stamp)
        if action is None:
            return "stale", reason or "action_not_authoritative"
        if action.starts_at <= stamp:
            return "expired", "meeting_already_started"
        return "ready", None

    async def _suppress_unavailable_preparation(self, session):
        delivery_status = OutcomeRun.content["delivery"]["status"].astext
        candidates = list(await session.scalars(select(OutcomeRun).where(
            OutcomeRun.status == "succeeded",
            OutcomeRun.content["kind"].astext == PREPARATION_KIND,
            OutcomeRun.content["sendAt"].astext.is_not(None),
            OutcomeRun.content["sendAt"].astext <= now().isoformat(),
            or_(delivery_status.is_(None), delivery_status.in_(["not_sent", "retryable", "sending"])),
        ).order_by(OutcomeRun.content["sendAt"].astext).limit(50)))
        for report in candidates:
            delivery = report.content.get("delivery") or {}
            if delivery.get("status") in {"accepted", "uncertain", "suppressed", "stale", "expired"}:
                continue
            binding = await session.get(WechatBinding, report.owner_id)
            state, reason = await self._preparation_gate(session, binding, report)
            if state in {"suppressed", "stale", "expired"}:
                report.content = {**report.content, "delivery": {
                    "status": state, "reason": reason, "finishedAt": now().isoformat()
                }}
                await session.commit()
                return True
        return False

    async def process_one(self):
        cipher = cipher_for(self.settings)
        async with self.database.sessions() as session:
            locked = await session.scalar(text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"), {"key": "agenote:wechat-worker"})
            if not locked:
                return False
            if await self._suppress_unavailable_preparation(session):
                return True
            row = await session.scalar(select(WechatBinding).join(User, User.id == WechatBinding.owner_id).where(
                User.is_active.is_(True),
                WechatBinding.status.in_(["pending", "scanned", "bound"]),
                WechatBinding.updated_at < now() - timedelta(seconds=2),
            ).order_by(WechatBinding.updated_at).with_for_update(of=WechatBinding, skip_locked=True).limit(1))
            if not row:
                return False
            if row.state.get("nextAt", "") > now().isoformat():
                row.updated_at = now()
                await session.commit()
                return False
            owner = row.owner_id
            try:
                if row.status in ["pending", "scanned"]:
                    await self.poll_binding(session, row, cipher)
                else:
                    creds = cipher.decode(row.encrypted)
                    try:
                        result = await self.transport.updates(creds)
                    except ChannelError as error:
                        if error.code == "transport_timeout":
                            result = {"msgs": []}
                        else:
                            raise
                    await self.receive(session, row, creds, result)
                    await session.flush()
                    row.encrypted = cipher.encode(creds)
                    await self.deliver(session, row, creds)
            except ChannelError as error:
                row.state = {
                    **row.state,
                    "lastError": error.code,
                    "message": "微信会话需要重新授权" if error.code in ["-14", "http_401", "http_403"] else "微信通道暂时不可用，将稍后重试",
                    "nextAt": (now() + timedelta(seconds=30)).isoformat(),
                }
                if error.code in ["-14", "http_401", "http_403"]:
                    row.status = "reauth_required"
            except IntegrityError:
                await session.rollback()
                row = await session.get(WechatBinding, owner)
                row.status = "failed"
                row.encrypted = None
                row.peer_hash = None
                row.state = {"message": "这个微信已经绑定其他 Agenote 账号，请先解绑原账号"}
            row.updated_at = now()
            await session.commit()
            return True

    async def poll_binding(self, session, row, cipher):
        if row.state.get("expiresAt", "") <= now().isoformat():
            row.status = "expired"
            row.state = {**row.state, "qr": None, "code": None}
            return
        result = await self.transport.poll_qr(row.state["code"], row.state["base"])
        status = result.get("status")
        if status == "confirmed":
            creds = verified_credentials(result)
            row.peer_hash = peer_hash(creds["peer"])
            row.encrypted = cipher.encode(creds)
            row.status = "bound"
            row.version += 1
            row.state = {key: value for key, value in row.state.items() if key not in ["qr", "code"]}
            row.state = {**row.state, "boundAt": now().isoformat(), "contextReady": False, "message": ""}
            user = await session.get(User, row.owner_id, with_for_update=True)
            old = (user.profile or {}).get("outcome_preferences", {})
            user.profile = {**(user.profile or {}), "outcome_preferences": {
                "autoAnalyze": True, "enabledAt": old.get("enabledAt") or now().isoformat()
            }}
            user.profile_version += 1
            await session.flush()
        elif status == "scaned":
            row.status = "scanned"
        elif status == "expired":
            row.status = "expired"
            row.state = {**row.state, "qr": None, "code": None}
        elif status == "scaned_but_redirect":
            host = str(result.get("redirect_host") or "")
            row.state = {**row.state, "base": allowed_base("https://" + host)}

    async def _enqueue_assistant(self, session, row, message_id, body, classification, parent_id=None):
        user = await session.get(User, row.owner_id)
        request_key = "wechat:" + str(row.state.get("generation") or "") + ":" + message_id
        run = await enqueue_query(
            session,
            row.owner_id,
            body,
            request_key=request_key,
            profile=user.profile if user else {},
            source="wechat",
            parent_id=parent_id,
            classification=classification,
        )
        if run:
            run.content = {**run.content, "replyRequested": True}
        else:
            _reply(row, message_id, "当前资料查询正在处理中或已达到今日上限，请稍后再试。", "quota")
        return run

    async def receive(self, session, row, creds, result):
        for message in (result.get("msgs") or [])[:50]:
            if (message.get("from_user_id") != creds["peer"] or message.get("room_id")
                    or message.get("group_id") or message.get("message_type", message.get("msg_type", 1)) != 1):
                continue
            recipient = message.get("to_user_id")
            if recipient and recipient != creds["bot"]:
                continue
            if message.get("context_token"):
                creds["context"] = str(message["context_token"])
                row.state = {**row.state, "contextReady": True, "message": ""}
            message_id = str(message.get("message_id") or "")
            body = "\n".join(str(item.get("text_item", {}).get("text", ""))
                             for item in message.get("item_list", []) if item.get("type") == 1).strip()
            if not message_id or not body:
                continue
            seen = row.state.get("seenMessages", [])
            if message_id in seen:
                continue
            row.state = {**row.state, "seenMessages": (seen + [message_id])[-100:]}

            report_id = row.state.get("lastOutcomeId")
            pointers = list(row.state.get("questionPointers", []))
            classification = classify_local(body, has_current=bool(report_id or pointers))

            # Explicit global/history/knowledge/compare language always bypasses
            # question pointers and the current report pointer.
            if classification["intent"] in {"history", "knowledge", "compare"}:
                await self._enqueue_assistant(session, row, message_id, body, classification)
                continue

            if classification["intent"] == "question_number":
                index = classification["questionNumber"] - 1
                if not pointers or not 0 <= index < len(pointers):
                    _reply(row, message_id, "疑问编号无效，请按当前列表中的编号回复。", "invalid-number")
                    continue
                report_id = pointers[index]
                body = classification["normalizedQuery"] or "请进一步说明这个疑问"
            elif pointers:
                if len(pointers) > 1:
                    _reply(row, message_id, "请回复疑问编号加追问内容，例如：1 这个风险如何验证？", "choose-question")
                    continue
                report_id = pointers[0]

            report = await session.scalar(select(OutcomeRun).where(
                OutcomeRun.id == report_id,
                OutcomeRun.owner_id == row.owner_id,
                OutcomeRun.status == "succeeded",
            )) if report_id else None

            if report and report.content.get("kind") == ASSISTANT_KIND:
                followup = {**classification, "intent": "current_outcome_followup",
                            "source": "current_assistant_context", "modelFallback": False}
                await self._enqueue_assistant(session, row, message_id, body, followup, parent_id=report.id)
                continue

            if report and report.content.get("kind") == "meeting_question":
                question = report.content.get("question", {})
                if not question.get("deleted") and question.get("status") != "resolved":
                    stamp = now().isoformat()
                    report.version += 1
                    report.content = {
                        **report.content,
                        "request": body[:1000],
                        "mutationId": "wechat:" + hashlib.sha256((row.owner_id + ":" + message_id).encode()).hexdigest(),
                        "replyRequested": True,
                        "requestReceivedAt": stamp,
                        "inbound": {"source": "wechat", "receivedAt": stamp, "classifiedAt": stamp,
                                    "queuedAt": stamp, "messageIdHash": hashlib.sha256(message_id.encode()).hexdigest()},
                        "attempts": 0,
                        "nextAt": "",
                        "leaseUntil": "",
                        "result": None,
                        "delivery": {"status": "not_sent"},
                    }
                    report.status = "queued"
                else:
                    _reply(row, message_id, "这个疑问已经解决或删除，请在 App 中选择新的疑问。", "closed-question")
                continue

            if report:
                meeting = await session.scalar(select(Meeting).where(
                    Meeting.id == report.meeting_id,
                    Meeting.owner_id == row.owner_id,
                    Meeting.deleted.is_(False),
                ))
                archive = await session.scalar(select(MeetingArchive).where(
                    MeetingArchive.meeting_id == report.meeting_id,
                    MeetingArchive.owner_id == row.owner_id,
                ))
                if meeting:
                    request_hash = hashlib.sha256((row.owner_id + ":" + message_id).encode()).hexdigest()
                    run = await enqueue(session, meeting, archive, request_key="wechat:" + request_hash, request=body)
                    if run:
                        stamp = now().isoformat()
                        run.content = {
                            **run.content,
                            "replyRequested": True,
                            "parentOutcomeId": report.id,
                            "pdfRequested": any(word in body.casefold() for word in ["pdf", "生成报告", "完整报告"]),
                            "inbound": {"source": "wechat", "receivedAt": stamp, "classifiedAt": stamp,
                                        "queuedAt": stamp, "messageIdHash": hashlib.sha256(message_id.encode()).hexdigest()},
                        }
                continue

            # No current meeting is required. Unknown text is durably queued and the
            # light classifier may safely narrow it in OutcomeJobs.
            await self._enqueue_assistant(session, row, message_id, body, classification)

        if result.get("get_updates_buf"):
            creds["cursor"] = str(result["get_updates_buf"])

    async def _deliver_reply(self, session, row):
        pending = list(row.state.get("pendingReplies", []))
        legacy = row.state.get("reply")
        if legacy:
            if not any(item.get("id") == legacy.get("id") for item in pending):
                pending.append({"id": legacy["id"], "text": legacy["text"], "status": "pending", "attempts": 0})
            row.state = {**row.state, "reply": None, "pendingReplies": pending[-20:]}
        reply = next((item for item in pending
                      if item.get("status") in {"pending", "retryable"}
                      and item.get("nextAt", "") <= now().isoformat()
                      and item.get("attempts", 0) < 3), None)
        if reply is None:
            return False
        expected = (row.owner_id, row.version, row.state.get("generation"))
        client_id = reply.get("clientId") or "agenote-ack-" + hashlib.sha256(
            (row.owner_id + str(expected[2]) + reply["id"]).encode()
        ).hexdigest()
        attempts = reply.get("attempts", 0) + 1
        reply.update(status="sending", attempts=attempts, clientId=client_id, claimedAt=now().isoformat())
        row.state = {**row.state, "pendingReplies": pending}
        row.updated_at = now()
        await session.commit()
        current = await session.scalar(select(WechatBinding).where(
            WechatBinding.owner_id == expected[0]
        ).with_for_update().execution_options(populate_existing=True))
        if not current or current.status != "bound" or (current.version, current.state.get("generation")) != expected[1:]:
            return True
        pending = list(current.state.get("pendingReplies", []))
        reply = next((item for item in pending if item.get("id") == reply["id"]), None)
        if reply is None:
            return True
        try:
            await self.transport.send(cipher_for(self.settings).decode(current.encrypted), reply["text"], client_id)
            pending = [item for item in pending if item.get("id") != reply["id"]]
        except ChannelError as error:
            reply.update(
                status="uncertain" if error.uncertain else "retryable",
                error=error.code,
                nextAt=(now() + timedelta(seconds=30 * attempts)).isoformat(),
            )
        current.state = {**current.state, "pendingReplies": pending}
        return True

    async def deliver(self, session, row, creds):
        if not creds.get("context"):
            return
        if await self._deliver_reply(session, row):
            return
        prefs = row.state.get("preferences", {})
        kind = OutcomeRun.content["kind"].astext
        delivery_status = OutcomeRun.content["delivery"]["status"].astext
        pdf_status = OutcomeRun.content["delivery"]["pdfStatus"].astext
        actionable = or_(
            delivery_status.is_(None),
            delivery_status.in_(["not_sent", "retryable", "sending"]),
            and_(delivery_status == "accepted",
                 kind.notin_(["meeting_question", ASSISTANT_KIND, PREPARATION_KIND]),
                 pdf_status.is_(None)),
        )
        rows = list(await session.scalars(select(OutcomeRun).outerjoin(
            Meeting, Meeting.id == OutcomeRun.meeting_id
        ).where(
            OutcomeRun.owner_id == row.owner_id,
            OutcomeRun.status == "succeeded",
            kind != PREPARATION_KIND,
            OutcomeRun.content["createdAt"].astext >= row.state["boundAt"],
            actionable,
            or_(
                kind == ASSISTANT_KIND,
                and_(OutcomeRun.meeting_id.is_not(None), Meeting.owner_id == row.owner_id, Meeting.deleted.is_(False)),
            ),
        ).order_by(OutcomeRun.content["createdAt"].astext).limit(50)))
        # Due preparations get their own bounded lane so old accepted role reports
        # cannot occupy the generic first-50 window forever.
        preparation_delivery = OutcomeRun.content["delivery"]["status"].astext
        due_preparations = list(await session.scalars(select(OutcomeRun).join(
            Meeting, Meeting.id == OutcomeRun.meeting_id
        ).where(
            OutcomeRun.owner_id == row.owner_id,
            OutcomeRun.status == "succeeded",
            OutcomeRun.content["kind"].astext == PREPARATION_KIND,
            Meeting.owner_id == row.owner_id,
            Meeting.deleted.is_(False),
            OutcomeRun.content["sendAt"].astext <= now().isoformat(),
            or_(preparation_delivery.is_(None), preparation_delivery.in_(["not_sent", "retryable", "sending"])),
        ).order_by(OutcomeRun.content["sendAt"].astext).limit(20)))
        due_preparations = [candidate for candidate in due_preparations
                            if getattr(candidate, "id", None)
                            and getattr(candidate, "content", {}).get("kind") == PREPARATION_KIND]
        due_ids = {candidate.id for candidate in due_preparations}
        rows = due_preparations + [candidate for candidate in rows
                                   if getattr(candidate, "id", None) not in due_ids]
        for report in rows:
            if report.status != "succeeded" or not isinstance(report.content.get("result"), dict):
                continue
            report_kind = report.content.get("kind")
            delivery = report.content.get("delivery", {})
            if report_kind == PREPARATION_KIND:
                gate, reason = await self._preparation_gate(session, row, report)
                if gate == "wait":
                    continue
                if gate in {"suppressed", "stale", "expired"}:
                    report.content = {**report.content, "delivery": {
                        "status": gate, "reason": reason, "finishedAt": now().isoformat()
                    }}
                    continue
                if delivery.get("status") == "sending":
                    # A crash after the network call has an unknown outcome. Never resend it indefinitely.
                    report.content = {**report.content, "delivery": {
                        **delivery, "status": "uncertain", "reason": "send_outcome_unknown_after_restart"
                    }}
                    continue
            if delivery.get("status") == "accepted":
                if report_kind not in {"meeting_question", ASSISTANT_KIND, PREPARATION_KIND} and not delivery.get("pdfStatus"):
                    wants_pdf = (prefs.get("sendPdf", True) and not prefs.get("paused")
                                 and (not report.content.get("replyRequested") or report.content.get("pdfRequested")))
                    if wants_pdf:
                        await self.deliver_pdf(session, row, report, creds)
                        return
                    report.content = {**report.content, "delivery": {
                        **delivery, "pdfStatus": "suppressed", "pdfReason": "pdf_preference_disabled"
                    }}
                continue
            if delivery.get("status") == "sending":
                report.content = {**report.content, "delivery": {
                    **delivery, "status": "uncertain", "reason": "send_outcome_unknown_after_restart"
                }}
                continue
            if delivery.get("status") in ["uncertain", "suppressed"]:
                continue
            if delivery.get("attempts", 0) >= 3:
                report.content = {**report.content, "delivery": {
                    **delivery, "status": "failed", "reason": "delivery_retries_exhausted"
                }}
                continue
            if delivery.get("nextAt", "") > now().isoformat():
                continue
            explicit = report.content.get("replyRequested") is True
            if report_kind != PREPARATION_KIND and not explicit and (
                    prefs.get("paused") or not prefs.get("autoMeeting", True) or not prefs.get("aiDone", True)):
                continue
            result = report.content.get("result") or {}
            if not explicit and prefs.get("importantOnly", True) and result.get("notify") is not True:
                report.content = {**report.content, "delivery": {"status": "suppressed", "reason": "not_important"}}
                continue
            if report_kind == "meeting_question" and not explicit:
                pending = await session.scalar(select(OutcomeRun.id).where(
                    OutcomeRun.owner_id == row.owner_id,
                    OutcomeRun.meeting_id == report.meeting_id,
                    OutcomeRun.content["kind"].astext == "meeting_question",
                    OutcomeRun.status.in_(["queued", "running", "retryable"]),
                ).limit(1))
                if pending and report.content.get("finishedAt", "") > (now() - timedelta(minutes=2)).isoformat():
                    continue
            batch = [report]
            if report_kind == "meeting_question" and not explicit:
                peers = [candidate for candidate in rows
                         if candidate.meeting_id == report.meeting_id
                         and candidate.content.get("kind") == "meeting_question"
                         and candidate.content.get("delivery", {}).get("status") == "not_sent"
                         and candidate.content.get("result")]
                batch = ([report] + [candidate for candidate in peers if candidate.id != report.id])[:10]
            client_id = (
                "agenote-preparation-" + hashlib.sha256(report.id.encode()).hexdigest()
                if report_kind == PREPARATION_KIND else
                "agenote-" + hashlib.sha256((
                    report.id + row.state["generation"]
                    + (report.content.get("mutationId", "") if report_kind == "meeting_question" else "")
                ).encode()).hexdigest()
            )
            if report_kind == ASSISTANT_KIND:
                message = _assistant_message(result)
            elif report_kind == PREPARATION_KIND:
                message = _preparation_message(result)
            else:
                message = "Agenote 工作助手\n" + ("你的追问已分析" if explicit else "你的专属分析已完成")
                message += "\n成果：" + str(result.get("title", "分析报告"))[:120]
                message += "\n发现：" + str(result.get("notification_summary") or result.get("summary", ""))[:350]
                if report_kind == "meeting_question":
                    message = "Agenote · 会议疑问\n会议：" + str(result.get("meetingTitle", "录音会议"))[:80] + "\n" + "\n\n".join(
                        str(index + 1) + "、" + str(candidate.content.get("result", {}).get("notification_summary", ""))[:350]
                        for index, candidate in enumerate(batch)
                    )
                    message += "\n完整分析与原声：Agenote → 对应会议纪要 → 我的疑问。可直接回复追问或纠正问题。"
                else:
                    message += "\n完整报告和原始依据：请在 Agenote → 我的 → 微信助手 → 分析成果查看。"
            attempts = delivery.get("attempts", 0) + 1
            claimed_at = now().isoformat()
            claim_id = hashlib.sha256((report.id + client_id + str(attempts) + claimed_at).encode()).hexdigest()
            report.content = {**report.content, "delivery": {
                "status": "sending", "attempts": attempts, "clientId": client_id,
                "claimedAt": claimed_at, "claimId": claim_id,
                "bindingGeneration": row.state.get("generation"),
            }}
            for companion in batch[1:]:
                companion.content = {**companion.content, "delivery": report.content["delivery"]}
            row.updated_at = now()
            expected = (row.owner_id, row.version, row.state["generation"])
            await session.commit()
            current = await session.scalar(select(WechatBinding).where(
                WechatBinding.owner_id == expected[0]
            ).with_for_update().execution_options(populate_existing=True))
            if not current or current.status != "bound" or (current.version, current.state.get("generation")) != expected[1:]:
                report.content = {**report.content, "delivery": {"status": "suppressed", "reason": "binding_changed_before_send"}}
                return
            if report_kind == PREPARATION_KIND:
                # Lock in the same order as cancellation: target action, source meeting, outcome.
                action_id = report.content.get("actionId")
                await session.scalar(select(ActionRun).where(
                    ActionRun.id == action_id, ActionRun.owner_id == current.owner_id
                ).with_for_update().execution_options(populate_existing=True))
                await session.scalar(select(Meeting).where(
                    Meeting.id == report.meeting_id, Meeting.owner_id == current.owner_id
                ).with_for_update().execution_options(populate_existing=True))
                current_report = await session.scalar(select(OutcomeRun).where(
                    OutcomeRun.id == report.id, OutcomeRun.owner_id == current.owner_id
                ).with_for_update().execution_options(populate_existing=True))
                current_delivery = current_report.content.get("delivery", {}) if current_report else {}
                if (not current_report or current_report.status != "succeeded"
                        or current_delivery.get("status") != "sending"
                        or current_delivery.get("claimId") != claim_id):
                    return
                gate, reason = await self._preparation_gate(session, current, current_report)
                if gate != "ready":
                    current_report.content = {**current_report.content, "delivery": {
                        "status": gate, "reason": reason, "finishedAt": now().isoformat()
                    }}
                    return
                report = current_report
                batch = [current_report]
            creds = cipher_for(self.settings).decode(current.encrypted)
            try:
                receipt = await self.transport.send(creds, message, client_id)
                state = {
                    "status": "accepted",
                    "attempts": attempts,
                    "acceptedAt": now().isoformat(),
                    "clientId": client_id,
                    "receiptCode": receipt.get("ret", receipt.get("errcode")),
                    "receiptMeaning": "http_200_no_channel_error_not_device_read_receipt",
                }
                if report_kind != PREPARATION_KIND:
                    current.state = {
                        **current.state,
                        "lastOutcomeId": report.id,
                        "questionPointers": [candidate.id for candidate in batch] if report_kind == "meeting_question" else [],
                    }
            except ChannelError as error:
                state = {
                    "status": "uncertain" if error.uncertain else "retryable",
                    "attempts": attempts,
                    "error": error.code,
                    "clientId": client_id,
                    "nextAt": (now() + timedelta(seconds=30 * attempts)).isoformat(),
                }
                if error.code in ["-14", "context_required"]:
                    creds["context"] = None
                    current.encrypted = cipher_for(self.settings).encode(creds)
                    current.state = {**current.state, "contextReady": False, "message": "请在微信发一条消息，更新推送上下文"}
            for delivered in batch:
                delivered.content = {**delivered.content, "delivery": state}
            return

    async def deliver_pdf(self, session, binding, report, creds):
        from app.modules.outcomes.pdf_report import private_pdf
        from app.modules.outcomes.weixin_pdf import upload_pdf_sdk
        delivery = report.content.get("delivery", {})
        report.content = {**report.content, "delivery": {**delivery, "pdfStatus": "sending", "pdfClaimedAt": now().isoformat()}}
        binding.updated_at = now()
        expected = (binding.owner_id, binding.version, binding.state["generation"])
        await session.commit()
        current = await session.scalar(select(WechatBinding).where(
            WechatBinding.owner_id == expected[0]
        ).with_for_update().execution_options(populate_existing=True))
        if (not current or current.status != "bound"
                or (current.version, current.state.get("generation")) != expected[1:]
                or current.state.get("preferences", {}).get("paused")):
            report.content = {**report.content, "delivery": {**report.content["delivery"], "pdfStatus": "suppressed"}}
            return
        try:
            path = await asyncio.to_thread(private_pdf, report)
            report.content = {**report.content, "pdf": {"available": True, "bytes": path.stat().st_size,
                                                        "path": "/assistant/outcomes/" + report.id + "/pdf"}}
            creds = cipher_for(self.settings).decode(current.encrypted)
            uploaded = await upload_pdf_sdk(self.settings, creds, path, binding.owner_id)
            client_id = "agenote-pdf-" + hashlib.sha256((report.id + current.state["generation"]).encode()).hexdigest()
            receipt = await self.transport.send_item(creds, uploaded["item"], client_id)
            status = {"pdfStatus": "accepted", "pdfClientId": client_id,
                      "pdfReceiptCode": receipt.get("ret", receipt.get("errcode")),
                      "pdfAcceptedAt": now().isoformat()}
        except ChannelError as error:
            status = {"pdfStatus": "uncertain" if error.uncertain else "failed", "pdfError": error.code}
        except Exception as error:
            status = {"pdfStatus": "failed", "pdfError": type(error).__name__}
        report.content = {**report.content, "delivery": {**report.content["delivery"], **status}}
