"""模型识别的是候选，服务端证据检查及人工版本授权才产生业务状态。"""

import copy
import hashlib
import json
import re


def fingerprint(value):
    if isinstance(value, dict) and "origin_id" in value:
        value = {
            k: v
            for k, v in value.items()
            if k not in {"business_status", "authorization_status", "evidence_digest"}
        }
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def normalized(text):
    return re.sub(r"[\s，,。；;：:、]", "", text).lower()


def transcript_hashes(sources):
    """稳定发言依据摘要；纠错、撤回、声源修订与未完成状态均参与失效判断。"""
    from app.modules.meetings.services.commitment_agent import source_speaker
    return {u['id']: fingerprint([u['raw'], u.get('correction'), u.get('withdrawn'),
        u.get('turn_status', 'complete'), source_speaker(u), u.get('speaker_revision', 0),
        u.get('speaker_separated', u.get('speakerSeparated', False))]) for u in sources}


def supported_action(action, evidence):
    """只接纳可逐字定位的行动，不用同义词相似度冒充语义已证实。"""
    needle = normalized(action)
    return len(needle) >= 2 and any(
        needle in normalized(u.get("correction") or u["raw"]) for u in evidence
    )


def guarded_report(report, utterances):
    """写入分析、进展前统一防线；不信任上游 JSON 中的状态或授权声明。"""
    value = copy.deepcopy(report)
    # 中断/未说完的发言允许参与记忆修复，但不能单独产生承诺、目标或日程。
    utterances = [u for u in utterances if u.get('turn_status', 'complete') == 'complete']
    sources = {u["id"]: u for u in utterances}
    for key in ['state', 'action_intents', 'tool_plans']:
        value[key] = [item for item in value.get(key, [])
                      if item.get('evidence_ids') and all(i in sources for i in item['evidence_ids'])]
    withdrawals = value.get("withdrawals", [])
    # 目前尚无经过服务端认证的声纹/会议决策权限协议。原话最多生成待核对候选。
    value["withdrawal_candidates"] = value.get("withdrawal_candidates", []) + [
        {
            **w,
            "authorization_status": "pending",
            "reason_code": "speaker_authority_unverified",
        }
        for w in withdrawals
        if w.get("target_id") in sources
    ]
    value["withdrawals"] = []
    value["business_guard_version"] = 3
    for fact in value.get("state", []):
        fact["reported_status"] = fact.get("reported_status", fact.get("status"))
        fact["authorization_status"] = "pending"
        if fact.get("status") in {"confirmed", "withdrawn"}:
            fact["status"] = "proposed"
    guarded = []
    for item in value.get("commitments", []):
        origin = sources.get(item.get("origin_id"))
        if not origin:
            continue
        events = item.get("events", [])
        evidence_ids = {
            key for event in events for key in event.get("evidence_ids", [])
        }
        if (
            not events
            or not evidence_ids
            or any(key not in sources for key in evidence_ids)
        ):
            continue
        evidence = [sources[key] for key in evidence_ids]
        if not supported_action(item.get("action", ""), evidence):
            value.setdefault("rejected_candidates", []).append(
                {
                    "kind": "commitment",
                    "origin_id": item.get("origin_id"),
                    "reason": "action_not_grounded_in_source",
                }
            )
            continue
        item = copy.deepcopy(item)
        from app.modules.meetings.services.commitment_agent import (
            CommitmentDraft,
            validate_commitments,
            source_speaker,
        )
        from pydantic import ValidationError

        candidate = {
            "origin_id": item["origin_id"],
            "speaker_id": item.get("speaker_id") or source_speaker(origin),
            "action": item["action"],
            "action_quote": item.get("action_quote") or item["action"],
            "goal_origin_id": item.get("goal_origin_id"),
            "side": item.get("side", "unknown"),
            "side_evidence_ids": item.get("side_evidence_ids") or list(evidence_ids),
            "events": [
                {
                    "kind": e["kind"],
                    "evidence_ids": e["evidence_ids"],
                    "summary": e.get("summary", "原话候选"),
                    "deadline_text": e.get("deadline_text", item.get("deadline_text")),
                }
                for e in [*events, *item.get("pending_changes", [])]
            ],
            'task_intent': item.get('task_intent'),
            'deliverable_quote': item.get('deliverable_quote'),
        }
        try:
            checked = validate_commitments(
                CommitmentDraft.model_validate({"commitments": [candidate]}), utterances
            )
        except (ValidationError, KeyError):
            continue
        if not checked:
            continue
        item = checked[0]
        # 提取可以保留匿名承诺原话，但不把来源聚类当作认证决策权限。
        item["business_status"] = "candidate"
        item["authorization_status"] = "pending"
        item["verification_status"] = "not_verified"
        item["evidence_digest"] = fingerprint(
            {key: sources[key]["raw"] for key in sorted(evidence_ids)}
        )
        guarded.append(item)
    value["commitments"] = guarded
    goals = []
    from app.modules.progress.services.goal_evidence import supported_goal
    for goal in value.get("goals", []):
        ids = goal.get("evidence_ids", [])
        condition = goal.get("condition", "")
        if (
            goal.get("origin_id") not in sources
            or not ids
            or any(i not in sources for i in ids)
            or not condition
            or not supported_goal(goal, sources)
            or not any(
                condition in (sources[i].get("correction") or sources[i]["raw"])
                for i in ids
            )
        ):
            continue
        goals.append(
            {**goal, "business_status": "candidate", "authorization_status": "pending"}
        )
    value["goals"] = goals
    questionnaire = value.get("questionnaire")
    if questionnaire:
        subjects = {i["origin_id"] for i in [*goals, *guarded]}
        questionnaire["questions"] = [
            q
            for q in questionnaire.get("questions", [])
            if q.get("origin_id") in subjects
            and q.get("evidence_ids")
            and all(
                i in sources and not sources[i].get("withdrawn")
                for i in q["evidence_ids"]
            )
        ]
    return value
