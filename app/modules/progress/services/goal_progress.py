"""从已授权履约状态投影里程碑；不让模型自行批准验收。"""
import re
from copy import deepcopy
from sqlalchemy import select
from app.modules.progress.models.progress_item import ProgressItem


def related(milestone, action):
    def normalized(value):
        value = re.sub(r"[^\w\u4e00-\u9fff+#.\-/]", "", value).lower()
        # 只去掉操作性前缀和通用后缀，不用子串相似把“提交测试报告”当成“完成测试”。
        for _ in range(2):
            value = re.sub(r"^(?:完成|执行|推进|开展|进行|实施|交付|提交|提供|编写)", "", value)
        return re.sub(r"(?:工作|事项|任务)$", "", value)
    left, right = normalized(milestone), normalized(action)
    return len(left) >= 4 and len(right) >= 4 and left == right


def milestone_projection(milestones, commitments):
    result = deepcopy(milestones)
    basis = []
    assignments = {i: [] for i in range(len(result))}
    for commitment in commitments:
        explicit = [i for i, m in enumerate(result) if m["title"] in commitment.get("milestoneTitles", [])]
        candidates = explicit or [i for i, m in enumerate(result) if related(m["title"], commitment["title"])]
        if len(candidates) > 1 and not explicit:
            # “测试报告”不能同时把更宽泛的“测试”节点也推成完成，优先唯一的更具体节点。
            longest = max(len(result[i]["title"]) for i in candidates)
            candidates = [i for i in candidates if len(result[i]["title"]) == longest]
        if len(candidates) == 1:
            assignments[candidates[0]].append(commitment)
    for index, milestone in enumerate(result):
        matched = assignments[index]
        # 同一节点有多个履约项时，必须全部核对完成才投影为完成。
        if not matched:
            continue
        milestone["status"] = (
            "completed" if all(c["status"] == "verified" and related(milestone["title"], c["title"]) for c in matched)
            else "active" if any(c["status"] in {"active", "claimed", "verified"} for c in matched)
            else "pending"
        )
        milestone['statusBasis'] = 'owner_verified' if milestone['status'] == 'completed' else 'authorized_task'
        basis.extend({"id": c["id"], "version": c["version"], "status": c["status"]} for c in matched)
    return {"milestones": result, "basis": sorted(basis, key=lambda c: c["id"])}


async def refresh_goal_progress(session, owner):
    from app.modules.progress.services.progress_service import output, ProgressService
    await session.flush()
    rows = (await session.scalars(select(ProgressItem).where(
        ProgressItem.owner_id == owner,
        ProgressItem.kind.in_(["goal", "commitment"]),
    ))).all()
    commitments = []
    goals_by_id = {r.id: output(r) for r in rows if r.kind == 'goal'}
    for row in rows:
        if row.kind != "commitment":
            continue
        value = output(row)
        if value.get('goalLinkBasis') == 'reviewed_candidate':
            from app.modules.meetings.models.meeting import Meeting
            from app.modules.progress.services.goal_evidence import explicit_milestones
            original = await session.get(Meeting, row.meeting_id) if row.meeting_id else None
            goal_value = goals_by_id.get(value.get('goalId'), {})
            sources = {s['id']:s for s in original.content['utterances']} if original else {}
            words = [sources[k].get('correction') or sources[k]['raw']
                for event in value.get('sourceEvents', []) for k in event.get('evidence_ids', []) if k in sources]
            from app.modules.progress.services.goal_continuity import mentions
            if not mentions(goal_value, words) and not explicit_milestones(goal_value, words):
                continue
        # 只有用户明确记录过的履约状态才参与；模型候选和未知归属不能推进目标。
        if value.get("authorizationStatus") == "owner_confirmed" and value.get("side") in {"ours", "theirs"}:
            commitments.append(value)
    from app.modules.execution.models import InternalTask
    from app.modules.execution.task_service import evidence_current
    tasks = (await session.scalars(select(InternalTask).where(InternalTask.owner_id == owner))).all()
    for task in tasks:
        value = task.content
        if value.get('bindingAuthorization') != 'owner_confirmed':
            continue
        valid = (value.get('status') == 'verified' and value.get('completionAuthorization') == 'owner_confirmed'
            and await evidence_current(session, owner, task))
        commitments.append({**value, 'id': 'task:' + task.id, 'version': task.version,
            'title': value['milestoneTitle'], 'milestoneTitles': [value['milestoneTitle']],
            'status': 'withdrawn' if value.get('status') == 'cancelled' else 'verified' if valid else 'active' if value.get('status') != 'pending' else 'pending',
            'basisKind': 'internal_task'})
    for goal in rows:
        if goal.kind != "goal" or "milestones" in goal.overrides:
            continue
        value = output(goal)
        if value.get("status") in {"withdrawn", "completed", "paused"}:
            continue
        # 旧会议同样能回收明确里程碑及陈述进度，不需要重新调用模型。
        if goal.meeting_id and await ProgressService(session).source_current(goal):
            from app.modules.meetings.models.meeting import Meeting
            from app.modules.progress.services.goal_continuity import explicit_steps, reported_milestones, sessions
            baseline = deepcopy(goal.content.get('milestones', []))
            entries = deepcopy(goal.content.get('meetingSources', {}))
            meeting_ids = goal.content.get('meetingOrder') or [goal.meeting_id]
            for meeting_id in meeting_ids:
                source_meeting = await session.get(Meeting, meeting_id)
                if not source_meeting or source_meeting.owner_id != owner:
                    continue
                sources = source_meeting.content['utterances']
                own_sources, own_ids, contested = sources, None, set()
                shared = [g for g in rows if g.kind == 'goal' and source_meeting.client_id in sessions(g)]
                if len(shared) > 1:
                    own_ids = set(goal.content.get('evidenceIds', [])) | set(
                        goal.content.get('meetingSources', {}).get(meeting_id, {}).get('hashes', {}))
                    own_sources = [u for u in sources if u['id'] in own_ids]
                    # 同场多个目标时，只有与其他目标重名的节点才限定在本目标来源；独有节点的会中进度陈述照常回收。
                    contested = {m['title'] for g in shared if g.id != goal.id
                        for m in g.content.get('milestones', [])}
                if not baseline:
                    words = [u.get('correction') or u['raw'] for u in own_sources if not u.get('withdrawn')
                        and u.get('turn_status', 'complete') == 'complete']
                    baseline = [{'title': t, 'weight': 1, 'status': 'pending'} for t in explicit_steps(words)]
                baseline = reported_milestones(baseline, sources, source_meeting.client_id,
                    own_ids=own_ids, contested=contested)
                claimed_ids = {i for m in baseline if m.get('statusBasis') == 'meeting_reported'
                    and m.get('evidenceSessionId') == source_meeting.client_id for i in m.get('evidenceIds', [])}
                if claimed_ids:
                    from app.modules.progress.services.goal_continuity import contribution
                    entry = entries.get(meeting_id, {})
                    claimed_ids.update(entry.get('hashes', {}))
                    entries[meeting_id] = contribution(source_meeting, entry.get('originIds', []), claimed_ids)
            if baseline != goal.content.get('milestones', []):
                content = {**goal.content, 'milestones': baseline, 'meetingSources': entries}
                content.pop('milestoneProjection', None)
                goal.content = content
                goal.version += 1
        projection = milestone_projection(goal.content.get("milestones", []),
            [c for c in commitments if c.get("goalId") == goal.id])
        if not projection["basis"] and "milestoneProjection" not in goal.content:
            continue
        if projection == goal.content.get("milestoneProjection"):
            continue
        goal.content = {**goal.content, "milestoneProjection": projection}
        goal.version += 1
        from datetime import datetime, timezone
        goal.history = [*goal.history, {"at": datetime.now(timezone.utc).isoformat(),
            "description": "根据已授权履约状态更新里程碑；最终验收仍需确认",
            "version": goal.version, "basis": projection["basis"]}]
