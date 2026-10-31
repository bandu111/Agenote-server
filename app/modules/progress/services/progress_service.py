"""从已验证会议证据投影进展；人工更正独立保存，模型重跑不能覆盖。"""

import re
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from sqlalchemy import text
from app.modules.progress.models.progress_item import ProgressItem
from app.modules.progress.repositories.progress_repository import ProgressRepository
from app.modules.progress.services.goal_evidence import supported_goal, explicit_milestones, certain_clause
from sqlalchemy import select

SHANGHAI = timezone(timedelta(hours=8))
TRANSITIONS = {
    "pending": {"planned", "withdrawn"},
    "planned": {"active", "claimed", "withdrawn"},
    "active": {"claimed", "withdrawn"},
    "claimed": {"verified", "active", "withdrawn"},
    "verified": set(),
    "withdrawn": set(),
}


def deadline_from_text(text, started_at):
    from app.modules.meetings.services.action_deadline import resolve_action_deadline
    deadline = resolve_action_deadline(text, started_at)
    return deadline['at'] if deadline else None


def output(row):
    value = {
        **row.content,
        **row.overrides,
        "id": row.id,
        "version": row.version,
        "history": row.history,
        "sourceRevision": row.source_revision,
    }
    if row.kind == "goal":
        projection = value.pop("milestoneProjection", None)
        if projection and "milestones" not in row.overrides:
            value["milestones"] = projection["milestones"]
        if "status" not in row.overrides and value.get("status") in {"pending", "active"}:
            milestones = value.get("milestones", [])
            # 会议陈述和履约核验都能推进阶段；全部节点完成即目标完成，用户确认后不再停留在待确认。
            if milestones and all(m["status"] == "completed" for m in milestones):
                value["status"] = "completed"
            elif value.get("confirmed") or any(m["status"] != "pending" for m in milestones):
                value["status"] = "active"
    if row.kind == "commitment":
        if "status" in row.overrides:
            value["authorizationStatus"] = "owner_confirmed"
        elif row.source_key.startswith("commitment:"):
            # 兼容旧记录，但不能继续将模型声称冒充已确认业务状态。
            if value.get("status") != "pending":
                value["candidateStatus"] = value["status"]
            value["status"] = "pending"
            value["authorizationStatus"] = "pending"
        missing = list(value.get("missing", []))
        if value.get('authorizationStatus') == 'owner_confirmed':
            missing = [m for m in missing if m not in {'是否承诺待确认', '责任来源待确认'}]
        if value.get("side") in {"ours", "theirs"}:
            missing = [m for m in missing if m != "履约方待明确"]
        if value.get("deadline"):
            missing = [m for m in missing if m != "交付时间待明确"]
        value["missing"] = missing
        if row.overrides and ("side" in row.overrides or "deadline" in row.overrides):
            value["summary"] = (
                "；".join(missing) or "履约信息已补全，履约状态以实际核验为准"
            )
    deadline = value.get("deadline")
    try:
        end = datetime.fromisoformat(deadline.replace("Z", "+00:00")) if deadline else None
        expired = end is not None and end.tzinfo is not None and end <= datetime.now(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        expired = False
    value["effectiveStatus"] = value.get("status")
    if expired or value.get("expiredByClear"):
        if row.kind == "goal" and value.get("status") != "completed":
            value["effectiveStatus"] = "expired"
        elif row.kind == "commitment" and value.get("status") in {"pending", "planned", "active"}:
            value["effectiveStatus"] = "overdue"
    return value


class ProgressService:
    def __init__(self, session):
        self.session = session
        self.repository = ProgressRepository(session)
        self._source_meetings = {}

    async def source_current(self, row):
        from app.modules.meetings.models.meeting import Meeting
        from app.modules.meetings.services.business_guard import transcript_hashes as source_hashes
        expected = row.content.get('sourceEvidenceHashes')
        checks = [(row.meeting_id, expected)] if expected and row.meeting_id else []
        checks.extend((meeting_id, entry.get('hashes', {}))
            for meeting_id, entry in row.content.get('meetingSources', {}).items())
        # 人工目标没有起源会议，也必须检查后来附着的会议进度来源。
        for meeting_id, expected_hashes in checks:
            if meeting_id not in self._source_meetings:
                self._source_meetings[meeting_id] = await self.session.get(Meeting, meeting_id)
            meeting = self._source_meetings[meeting_id]
            if not meeting or meeting.deleted or meeting.owner_id != row.owner_id:
                return False
            current = source_hashes(meeting.content['utterances'])
            if not all(current.get(key) == value for key, value in expected_hashes.items()):
                return False
        return True

    async def _lock_owner(self, owner):
        await self.session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": "goal-progress:" + owner})

    async def project(self, meeting, report, *, preview=False, incremental=False):
        await self._lock_owner(meeting.owner_id)
        from app.modules.meetings.services.business_guard import guarded_report
        report = guarded_report(report, meeting.content["utterances"])
        self._meeting_goal_hints = report.get('goals', [])
        revision = (
            meeting.analysis_revision
            if meeting.analysis_revision >= 0 and not incremental
            else meeting.revision
        )
        sources = {u["id"]: u for u in meeting.content["utterances"]}
        from app.modules.meetings.services.business_guard import transcript_hashes as source_hashes
        hashes = source_hashes(meeting.content['utterances'])
        started = meeting.content["started_at"]
        goals = {}
        accepted_keys = set()
        for goal in report.get("goals", []):
            origin = sources.get(goal.get("origin_id"))
            ids = goal.get("evidence_ids", [])
            if not origin or not ids or any(key not in sources for key in ids):
                continue
            words = "\n".join(
                sources[key].get("correction") or sources[key]["raw"] for key in ids
            )
            if not supported_goal(goal, sources):
                continue
            condition = goal.get("condition", "")
            if not condition or condition not in words:
                continue
            acceptance = goal.get("acceptance") or "待明确"
            if acceptance != "待明确" and acceptance not in words:
                acceptance = "待明确"
            date = goal.get("deadline_text")
            date = deadline_from_text(date, started) if date and date in words else None
            milestones = [
                {"title": item, "weight": 1, "status": "pending"}
                for item in goal.get("milestones", [])
                if item and item in words
            ]
            if not milestones:
                from app.modules.progress.services.goal_continuity import explicit_steps
                milestones = [{'title': t, 'weight': 1, 'status': 'pending'} for t in explicit_steps([words])]
            content = {
                "title": goal["title"],
                "code": "",
                "phase": (
                    goal.get("phase")
                    if goal.get("phase") and goal["phase"] in words
                    else "待明确"
                ),
                "condition": condition,
                "acceptance": acceptance,
                "deadline": date,
                "deadlineText": goal.get("deadline_text"),
                "status": "pending",
                "milestones": milestones,
                "projectId": "personal-meetings",
                "evidenceIds": ids,
                "sessionId": meeting.client_id,
                'sourceEvidenceHashes': {i: hashes[i] for i in ids},
            }
            from app.modules.progress.services.goal_continuity import upsert_goal
            row = await upsert_goal(self, meeting, goal['origin_id'], content, revision, preview)
            accepted_keys.add(row.source_key)
            goals[goal["origin_id"]] = row.id
        from app.modules.progress.services.goal_continuity import continue_explicit_goals, sessions
        if not preview:
            await continue_explicit_goals(self, meeting)
        available = [r for r in await self.session.scalars(select(ProgressItem).where(
            ProgressItem.owner_id == meeting.owner_id, ProgressItem.kind == 'goal'))
            if meeting.client_id in sessions(r)]
        for g in available:
            for origin_id in g.content.get('meetingSources', {}).get(meeting.id, {}).get('originIds', []):
                goals.setdefault(origin_id, g.id)
        for item in report.get("commitments", []):
            origin = sources.get(item.get("origin_id"))
            if not origin:
                continue
            events = item.get("events", [])
            if not events or any(
                not e.get("evidence_ids")
                or any(k not in sources for k in e["evidence_ids"])
                for e in events
            ):
                continue
            goal_origin = item.get("goal_origin_id")
            if goal_origin and goal_origin not in goals:
                # 早期目标可能已离开近期分析窗口，沿用同场会议已有的明确关联。
                previous_goal = await self.repository.source(
                    meeting.owner_id, f"goal:{meeting.id}:{goal_origin}"
                )
                if previous_goal and previous_goal.kind == "goal":
                    goals[goal_origin] = previous_goal.id
            date = deadline_from_text(item.get("deadline_text"), started)
            evidence_words = [sources[k].get("correction") or sources[k]["raw"]
                for event in events for k in event["evidence_ids"]]
            linked_id = goals.get(goal_origin, "")
            link_basis = "reviewed_candidate" if linked_id else "none"
            if linked_id:
                target = next((g for g in available if g.id == linked_id), None)
                target_value = output(target) if target else {}
                grounded = bool(target) and (
                    any(certain_clause(w, target_value.get('condition', '')) for w in evidence_words
                        if len(target_value.get('condition', '')) >= 6)
                    or bool(explicit_milestones(target_value, evidence_words)))
                if target and not grounded:
                    from app.modules.progress.services.goal_continuity import mentions
                    grounded = mentions(target_value, evidence_words)
                if not grounded:
                    linked_id, link_basis = '', 'unconfirmed_candidate'
            if not linked_id:
                # 只接受本场会议唯一、逐字指向具体交付目标的引用，不以“测试”等短词猜关联。
                matches = [g for g in available if output(g).get("status") not in {"withdrawn", "paused", "completed"}
                    and len(output(g).get("condition", "")) >= 6
                    and any(certain_clause(w, output(g)["condition"]) for w in evidence_words)]
                if len(matches) == 1:
                    linked_id, link_basis = matches[0].id, "explicit_source_quote"
            linked = next((g for g in available if g.id == linked_id), None)
            milestone_titles = explicit_milestones(output(linked), evidence_words) if linked else []
            # 记录原话中的承诺并不等于验证了决策权限；状态变更使用带版本的人工接口。
            status = "pending"
            content = {
                "title": item["action"],
                'taskIntent': item.get('task_intent'),
                'taskDeliverable': item.get('deliverable_quote'),
                'taskSourceText': origin.get('correction') or origin['raw'],
                "person": item.get("speaker", "待确认发言人"),
                "side": item.get("side", "unknown"),
                "status": status,
                "candidateStatus": item.get("status", "proposed"),
                "authorizationStatus": "pending",
                "createdAt": started,
                "deadline": date,
                "deadlineText": item.get("deadline_text"),
                "summary": "；".join(item.get("missing", []))
                or "来自会议原话，身份尚未认证",
                "goalId": linked_id,
                "goalLinkBasis": link_basis,
                "milestoneTitles": milestone_titles,
                "progress": 0,
                "evidence": {
                    "id": origin["id"],
                    "sessionId": meeting.client_id,
                    "transcript": origin["raw"],
                    "start": origin["at"],
                    "end": origin["at"],
                    "recordedAt": started,
                    "timeZone": "Asia/Shanghai",
                    "speakerConfirmed": False,
                },
                "sourceEvents": events,
                "pendingChanges": item.get("pending_changes", []),
                "missing": item.get("missing", []),
                "attachment": None,
                'sourceEvidenceHashes': {i: hashes[i] for e in events for i in e['evidence_ids']},
                "withdrawalReason": (
                    events[-1].get("summary") if status == "withdrawn" else None
                ),
            }
            row = await self._upsert(
                meeting, "commitment", item["origin_id"], content, revision=revision, preview=preview
            )
            accepted_keys.add(row.source_key)
        if not preview and not incremental:
            provisional = list(await self.session.scalars(select(ProgressItem).where(
                ProgressItem.owner_id == meeting.owner_id, ProgressItem.meeting_id == meeting.id)))
            for row in provisional:
                if row.content.get('reviewPending') and row.source_revision <= revision and row.source_key not in accepted_keys:
                    # 只撤下未经核对的临时展示，不撤回已有有效决定。
                    row.content = {**row.content, 'reviewPending': False, 'reviewRejected': True}
        from app.modules.progress.services.goal_progress import refresh_goal_progress
        await refresh_goal_progress(self.session, meeting.owner_id)

    async def _upsert(self, meeting, kind, origin, content, *, revision=None, preview=False):
        revision = meeting.revision if revision is None else revision
        key = f"{kind}:{meeting.id}:{origin}"
        lock_key = int.from_bytes(
            hashlib.sha256(f"progress:{meeting.owner_id}:{key}".encode()).digest()[:8],
            "big",
            signed=True,
        )
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key}
        )
        row = await self.repository.source(meeting.owner_id, key)
        if preview and row is not None: return row
        if kind in {'goal', 'commitment'}:
            content = {**content, 'reviewPending': preview, 'reviewRejected': False}
        if row is None:
            row = ProgressItem(
                id=str(uuid.uuid4()),
                owner_id=meeting.owner_id,
                meeting_id=meeting.id,
                source_key=key,
                kind=kind,
                source_revision=revision,
                version=1,
                content=content,
                overrides={},
                history=[],
            )
            await self.repository.add(row)
        elif row.source_revision <= revision:
            if kind == "goal" and "milestoneProjection" in row.content:
                content = {**content, "milestoneProjection": row.content["milestoneProjection"]}
            if kind == "artifact" and row.content.get("versionLabel") != content.get(
                "versionLabel"
            ):
                row.overrides = {}
            if row.content != content:
                row.version += 1
            row.content = content
            row.source_revision = revision
        return row

    async def archive_artifact(self, meeting, archive):
        return await self._upsert(
            meeting,
            "artifact",
            "minutes",
            {
                "title": archive.title,
                "summary": "会议纪要已归档至知识库",
                "goalId": "",
                "versionLabel": f"r{archive.revision}",
                "createdAt": meeting.content["started_at"],
                "status": "prepared",
                "content": archive.markdown,
                "risk": "",
                "documentId": "meeting-" + meeting.client_id,
            },
        )

    async def snapshot(self, owner_id):
        # Reconcile clears from older app versions as well as new clear requests.
        from app.modules.knowledge.services.notice_inbox import cleared_ids, expire_progress
        await self._lock_owner(owner_id)
        await expire_progress(self.session, owner_id, await cleared_ids(self.session, owner_id))
        from app.modules.progress.services.goal_progress import refresh_goal_progress
        await refresh_goal_progress(self.session, owner_id)
        await self.session.flush()
        result = {"goals": [], "commitments": [], "artifacts": [], "pending": []}
        for row in await self.repository.list(owner_id):
            if row.content.get('reviewRejected'): continue
            item = output(row)
            if row.kind == 'goal' and item.get('removed'):
                continue
            if row.kind in {'goal', 'commitment'} and not await self.source_current(row):
                result['pending'].append({**item, 'kind': row.kind, 'reason': '原话已变更，等待重新核对'})
                continue
            if row.kind == "goal" and not item.get("deadline"):
                # 交期缺失只影响补全，不让整个目标及其拓扑从进展消失。
                result['goals'].append(item)
                result["pending"].append(
                    {**item, "kind": "goal", "reason": "交期待明确"}
                )
            elif row.kind == "commitment" and (
                not item.get("deadline") or item.get("side") not in {"ours", "theirs"}
            ):
                result["pending"].append(
                    {**item, "kind": "commitment", "reason": "履约方或交期待明确"}
                )
            else:
                result[
                    {
                        "goal": "goals",
                        "commitment": "commitments",
                        "artifact": "artifacts",
                    }[row.kind]
                ].append(item)
        from app.modules.progress.services.progress_relations import enrich_relations
        result = await enrich_relations(self, owner_id, result)
        # 自动投影产生的版本必须与响应一起持久化，防止客户端拿到不可用的新版本。
        await self.session.commit()
        return result

    async def create_goal(self, owner_id, data):
        row = ProgressItem(
            id=str(uuid.uuid4()),
            owner_id=owner_id,
            meeting_id=None,
            source_key="manual:" + str(uuid.uuid4()),
            kind="goal",
            source_revision=-1,
            version=1,
            content={"code": "", **data.model_dump(mode="json")},
            overrides={},
            history=[],
        )
        await self.repository.add(row)
        await self.session.commit()
        return output(row)

    async def complete(self, owner_id, item_id, data):
        await self._lock_owner(owner_id)
        row = await self.repository.get(owner_id, item_id, lock=True)
        if row is None or row.kind not in {"goal", "commitment"}:
            raise HTTPException(404, "progress_not_found")
        if row.version != data.version:
            raise HTTPException(409, "progress_version_conflict")
        current = output(row)
        values = {}
        if data.side is not None:
            if row.kind != "commitment" or (current.get("side") in {"ours", "theirs"}
                    and (not data.confirm or current["side"] != data.side)):
                raise HTTPException(409, "progress_version_conflict")
            if current.get("side") != data.side:
                values["side"] = data.side
        if data.deadline is not None:
            if current.get("deadline"):
                raise HTTPException(409, "progress_version_conflict")
            values["deadline"] = data.deadline.isoformat()
        if data.confirm:
            # 会议中的明确确认与补齐原子提交；不把单纯补信息当作批准。
            if row.kind != 'commitment' or current.get('status') in {'withdrawn', 'verified', 'claimed'}:
                raise HTTPException(409, 'progress_invalid_transition')
            if values.get('side', current.get('side')) not in {'ours', 'theirs'} or not values.get('deadline', current.get('deadline')):
                raise HTTPException(422, 'progress_completion_required')
            if current.get('status') == 'pending':
                values['status'] = 'planned'
        # 复用版本、原话有效性、审慎复核和事务检查；已确认不等于已履约。
        return await self.mutate(
            owner_id, item_id, row.kind, data.version, values, data.note
        )

    async def remove_goal(self, owner_id, item_id, version, note):
        await self._lock_owner(owner_id)
        if not note.strip():
            raise HTTPException(422, "invalid_request")
        row = await self.repository.get(owner_id, item_id, lock=True)
        if row is None or row.kind != 'goal':
            raise HTTPException(404, 'progress_not_found')
        if output(row).get('removed'):
            return output(row)
        if row.version != version:
            raise HTTPException(409, 'progress_version_conflict')
        at = datetime.now(timezone.utc).isoformat()
        changes = {'removed': True, 'removedAt': at, 'status': 'paused'}
        row.overrides = {**row.overrides, **changes}
        row.version += 1
        row.history = [*row.history, {'at': at, 'description': note.strip(),
            'actorUserId': owner_id, 'version': row.version, 'changes': changes}]
        await self.session.commit()
        return output(row)

    async def confirm_goal(self, owner_id, item_id, version, note):
        """用户确认会议提取的目标成立；只去掉待确认，不改节点，后续进度仍按里程碑推进。"""
        await self._lock_owner(owner_id)
        row = await self.repository.get(owner_id, item_id, lock=True)
        if row is None or row.kind != 'goal' or output(row).get('removed'):
            raise HTTPException(404, 'progress_not_found')
        if output(row).get('confirmed'):
            return output(row)
        if row.version != version:
            raise HTTPException(409, 'progress_version_conflict')
        at = datetime.now(timezone.utc).isoformat()
        changes = {'confirmed': True, 'confirmedAt': at}
        row.overrides = {**row.overrides, **changes}
        row.version += 1
        row.history = [*row.history, {'at': at, 'description': note.strip(),
            'actorUserId': owner_id, 'version': row.version, 'changes': changes}]
        await self.session.commit()
        return output(row)

    async def mutate(self, owner_id, item_id, kind, version, values, note):
        await self._lock_owner(owner_id)
        if not note.strip():
            raise HTTPException(422, "invalid_request")
        row = await self.repository.get(owner_id, item_id, lock=True)
        if row is None or row.kind != kind:
            raise HTTPException(404, "progress_not_found")
        if kind == 'goal' and output(row).get('removed'):
            raise HTTPException(409, 'progress_goal_removed')
        if row.version != version:
            raise HTTPException(409, "progress_version_conflict")
        if row.content.get('reviewPending') or row.content.get('reviewRejected'):
            raise HTTPException(409, 'progress_review_pending')
        if kind in {'goal', 'commitment'} and not await self.source_current(row):
            raise HTTPException(409, 'progress_source_changed')
        current = output(row)
        if kind == "commitment":
            status = values.get("status")
            if status == "planned":
                if current.get("side") not in {"ours", "theirs"} or not current.get("deadline"):
                    raise HTTPException(422, "progress_completion_required")
                if current.get("effectiveStatus") == "overdue" or current.get("expiredByClear"):
                    raise HTTPException(409, "progress_deadline_expired")
            if status and status not in TRANSITIONS.get(current["status"], set()):
                raise HTTPException(409, "progress_invalid_transition")
            if "deadline" in values and current["status"] in {"verified", "withdrawn"}:
                raise HTTPException(409, "progress_invalid_transition")
        if kind == "artifact":
            if (
                current["status"] != "confirmation"
                or values.pop("artifactVersion") != current["versionLabel"]
            ):
                raise HTTPException(409, "progress_version_conflict")
        row.overrides = {**row.overrides, **values}
        row.version += 1
        row.history = [
            *row.history,
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "description": note.strip(),
                "actorUserId": owner_id,
                "version": row.version,
                "changes": values,
            },
        ]
        from app.modules.progress.services.goal_progress import refresh_goal_progress
        await refresh_goal_progress(self.session, owner_id)
        await self.session.commit()
        return output(row)
