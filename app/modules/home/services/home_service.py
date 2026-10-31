"""账户首页只投影持久化业务结果，不在读取时调用模型或改变业务状态。"""

from sqlalchemy import select
from app.modules.actions.models import ActionRun
from app.modules.meetings.repositories.meeting_repository import MeetingRepository
from app.modules.progress.repositories.progress_repository import ProgressRepository
from app.modules.progress.services.progress_service import output
from app.modules.knowledge.repositories.knowledge_repository import KnowledgeRepository
from app.modules.home.services.task_checklist import checklist_items


def build_home(progress, actions, meeting=None, *, now=None):
    """保留来源、候选状态和版本；未经确认的安排不会伪装成已排期。"""
    active = [p for p in progress if p.get("status") not in {"withdrawn", "verified", "completed"}]
    goals = [p for p in active if p["kind"] == "goal" and p.get('evidenceCurrent', True)]
    schedules = []
    for item in progress:
        if not item.get('evidenceCurrent', True): continue
        if not item.get("deadline") or item.get("status") == "withdrawn":
            continue
        # 模型承诺候选不自动进入今天安排；手工目标及已授权履约项可进入。
        if item["kind"] == "commitment":
            allowed = item.get("authorizationStatus") == "owner_confirmed" and item.get("side") == "ours"
        elif item['kind'] == 'internal_task':
            allowed = True
        else:
            allowed = item["kind"] == "goal" and item.get("manual", False)
        if not allowed:
            continue
        schedules.append({
            "id": "progress:" + item["id"], "title": item["title"],
            "startsAt": item["deadline"], "endsAt": None, "kind": "task",
            "completed": item.get("status") in {"verified", "completed"},
            "notes": "进展中的交付截止时间；仅约定日期时按当日结束展示。请在进展中更新履约状态。",
            "source": "账户进展", "sourceId": item["id"], "version": item["version"],
        })
    for action in actions:
        # Pending/retryable commands remain approval notifications and never become schedules.
        if action.get("kind") == "tool_plan" or not action.get("authority"):
            continue
        schedule = {
            "id": "calendar:" + action["id"],
            "title": action["title"],
            "startsAt": action["start"],
            "endsAt": action["end"],
            "timeZone": action.get("timeZone"),
            "agenda": action.get("agenda"),
            "kind": "meeting",
            "completed": False,
            "reviewPending": False,
            "authority": action["authority"],
            "actionVersion": action["version"],
            "verifiedAt": action.get("verifiedAt"),
            "notes": "经用户批准，并由已认证设备回执字段一致的未来日程；不表示服务端读取了设备日历。",
            "source": "已核验日历行动",
            "sourceId": action["id"],
            "version": action["version"],
        }
        if action.get("preparationId"):
            schedule.update({
                "preparationId": action["preparationId"],
                "preparationStatus": action.get("preparationStatus"),
                "preparationSummary": action.get("preparationSummary"),
                "preparationRoute": "/outcomes/" + action["preparationId"],
            })
        schedules.append(schedule)
    rows, decisions = [], []
    if meeting:
        sources = {u["id"]: u for u in meeting["content"].get("utterances", [])}
        report = meeting["analysis"]
        for fact in report.get("state", []):
            ids = fact.get("evidence_ids", [])
            if not ids or any(i not in sources or sources[i].get("withdrawn") for i in ids):
                continue
            rows.append({"label": "待核对", "text": fact["value"], "evidenceIds": ids})
        for insight in report.get("insights", []):
            ids = insight.get("evidence_ids", [])
            if insight.get("kind") not in {"risk", "question"} or not ids or any(i not in sources or sources[i].get("withdrawn") for i in ids):
                continue
            decisions.append({
                "id": "insight:" + meeting["id"] + ":" + insight["id"],
                "label": "待核对 · 风险提示" if insight["kind"] == "risk" else "待补充信息",
                "title": insight["title"], "body": insight["body"],
                "evidence": "\n".join(f'{sources[i]["speaker"]}：{sources[i]["raw"]}' for i in ids),
                "evidenceCount": len(ids), "evidenceIds": ids,
                "sessionId": meeting["id"], "route": "/meeting?id=" + meeting["id"],
            })
    # 缺字段及待授权行动直接复用现有全局通知，不新建第二份审批状态。
    for item in active:
        if item.get('reviewPending'): continue
        missing = list(item.get("missing", []))
        if item["kind"] == "goal" and not item.get("deadline"):
            missing.append("交期待明确")
        if not missing:
            continue
        decisions.append({"id": "pending:" + item["id"], "label": "待补充信息",
                          "title": item["title"], "body": "；".join(dict.fromkeys(missing)),
                          "evidence": item.get("evidence", {}).get("transcript", ""),
                          "evidenceCount": 1 if item.get("evidence") else 0,
                          "route": "/notifications"})
    for item in progress:
        if item['kind'] == 'internal_task' and item.get('status') in {'claimed_completed', 'evidence_verified'} and (item.get('evidenceCurrent') or not item.get('attachments') and item.get('sourceCurrent')):
            decisions.append({'id': 'action:task:' + item['id'] + ':' + str(item['version']),
                'label': '待本人核对', 'title': '确认任务完成：' + item['title'],
                'body': '请本人确认实际完成情况；不代表客户验收。',
                'evidence': '', 'evidenceCount': len(item.get('attachments', [])), 'route': '/notifications'})
    for action in actions:
        if action["status"] not in {"pending", "awaiting_device", 'retryable'}:
            continue
        calendar_operation = action.get("operation", "create")
        calendar_label = {"create": "创建日程：", "reschedule": "改期日程：", "cancel": "取消日程："}.get(
            calendar_operation, "日历操作："
        )
        calendar_instruction = {
            "create": "允许后由设备创建日历项并回读核验。",
            "reschedule": "允许后由设备更新原日历项并回读核验。",
            "cancel": "允许后由设备删除原日历项并回读核验。",
        }.get(calendar_operation, "允许后由设备执行并回读核验。")
        decisions.append({"id": "action:" + action["id"], "label": "等待授权",
                          "title": action['title'] if action.get('kind') == 'tool_plan' else calendar_label + action["title"],
                          "body": action.get('instruction', calendar_instruction),
                          "evidence": "\n".join(action.get("evidence_ids", [])),
                          "evidenceCount": len(action.get("evidence_ids", [])), "route": "/notifications"})
    from app.modules.home.services.home_goal_state import situation
    current_situation = situation(progress, meeting, now)
    return {
        **current_situation, "decisions": decisions[:12], "schedules": schedules,
        "tasks": checklist_items(progress, now=now),
        "sessionId": meeting["id"] if meeting and meeting.get('analysis') else None,
        "source": "persisted_account_data", "modelCallsOnRead": 0,
    }


class HomeService:
    def __init__(self, session):
        self.session = session

    async def snapshot(self, owner):
        from app.modules.progress.services.goal_progress import refresh_goal_progress
        from app.modules.progress.services.progress_service import ProgressService
        progress_service = ProgressService(self.session)
        await progress_service._lock_owner(owner)
        await refresh_goal_progress(self.session, owner)
        await self.session.flush()
        rows = await ProgressRepository(self.session).list(owner)
        progress = []
        for row in rows:
            if row.content.get('reviewRejected'): continue
            if row.kind not in {'goal', 'commitment'}: continue
            if row.kind == 'goal' and output(row).get('removed'): continue
            item = {**output(row), 'kind': row.kind, 'manual': row.source_key.startswith('manual:')}
            item['evidenceCurrent'] = await progress_service.source_current(row)
            if not item['evidenceCurrent']: item['missing'] = ['原话已变更，等待重新核对']
            progress.append(item)
        from app.modules.execution.task_service import TaskService
        tasks = await TaskService(self.session).list(owner, include_organized=True)
        from app.modules.meetings.models.meeting import Meeting
        from app.modules.execution.models import InternalTask
        task_sessions = dict((await self.session.execute(select(InternalTask.id, Meeting.client_id)
            .join(Meeting, Meeting.id == InternalTask.meeting_id)
            .where(InternalTask.owner_id == owner, Meeting.owner_id == owner))).all()) if tasks else {}
        tasks = [{**t, 'sessionId': task_sessions.get(t['id'])} for t in tasks]
        progress.extend({**t, 'kind': 'internal_task'} for t in tasks)
        actions = list(await self.session.scalars(select(ActionRun).where(ActionRun.owner_id == owner)))
        action_values = [{**a.content, "id": a.id, "version": a.version, "status": a.status,
                          "kind": "tool_plan" if a.content.get("kind") == "tool_plan" else "calendar"}
                         for a in actions]
        from app.modules.actions.calendar_authority import authority_output, list_authoritative_actions
        authoritative = await list_authoritative_actions(self.session, owner)
        authority_values = {row.id: authority_output(row) for row in authoritative}
        if authoritative:
            from app.modules.outcomes.models import OutcomeRun
            preparation_rows = list(await self.session.scalars(select(OutcomeRun).where(
                OutcomeRun.owner_id == owner,
                OutcomeRun.status == "succeeded",
                OutcomeRun.content["kind"].astext == "meeting_preparation",
                OutcomeRun.content["actionId"].astext.in_([row.id for row in authoritative]),
            ).order_by(OutcomeRun.content["finishedAt"].astext.desc()).limit(200)))
            for preparation in preparation_rows:
                action = authority_values.get(preparation.content.get("actionId"))
                if not action or action.get("preparationId"):
                    continue
                if (preparation.content.get("actionVersion") != action["version"]
                        or preparation.content.get("actionFingerprint") != action.get("fingerprint")):
                    continue
                result = preparation.content.get("result") or {}
                action.update(
                    preparationId=preparation.id,
                    preparationStatus=preparation.status,
                    preparationSummary=result.get("summary"),
                )
        action_values = [authority_values.get(row.id, value) for row, value in zip(actions, action_values)]
        meetings = await MeetingRepository(self.session).list(owner)
        # 最新会议若仍在分析则不展示旧版本报告；禁止把过时预测当作当前状态。
        current = meetings[0] if meetings else None
        meeting = {"id": current.client_id, "content": current.content, "analysis": {}} if current else None
        from app.modules.meetings.services.realtime_business import additive_sources
        if current and current.analysis and (current.analysis_revision == current.revision or
                current.analysis.get('realtime') and additive_sources(current.analysis, current.content['utterances'])):
            report = current.analysis
            library = await KnowledgeRepository(self.session).library(owner)
            library_revision = library.revision if library else 0
            # 引用的资料撤权或换版后，旧风险不能继续充当当前可信依据。
            if not report.get("knowledge_sources") or report.get("knowledge_revision") == library_revision:
                meeting = {"id": current.client_id, "content": current.content, "analysis": report}
        result = build_home(progress, action_values, meeting)
        from app.modules.meetings.services.minutes_actions import list_minutes_tasks
        result['tasks'] = await list_minutes_tasks(self.session, owner, include_expired=True)
        from datetime import datetime, timedelta, timezone
        from app.modules.meetings.models.archive import MeetingArchive
        from app.modules.home.services.weekly_rhythm import weekly_rhythm
        today = datetime.now(timezone(timedelta(hours=8))).date()
        recent = list((await self.session.scalars(select(Meeting).where(
            Meeting.owner_id == owner, Meeting.deleted.is_(False),
            Meeting.content['started_at'].astext >= (today-timedelta(days=6)).isoformat(),
            Meeting.content['started_at'].astext < (today+timedelta(days=1)).isoformat()))).all())
        saved = list((await self.session.scalars(select(MeetingArchive).where(
            MeetingArchive.owner_id == owner, MeetingArchive.meeting_id.in_([m.id for m in recent])))).all()) if recent else []
        result['weeklyRhythm'] = weekly_rhythm(recent, {a.meeting_id:a for a in saved}, progress, result['tasks'])

        await self.session.commit()
        return result
