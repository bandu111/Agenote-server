"""把已定位的信息缺口整理为最少问题，答案仍须来自原话或用户的业务更正。"""

import hashlib
import json
from pydantic import Field, ValidationError
from app.modules.meetings.schemas.meeting import StrictModel
from app.integrations.hermes.skill_catalog import questionnaire_skill


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


class Question(StrictModel):
    candidate_id: str = Field(max_length=120)
    question: str = Field(min_length=2, max_length=160)
    why: str = Field(default="", max_length=180)


class QuestionnaireDraft(StrictModel):
    questions: list[Question] = Field(default_factory=list, max_length=2)


class DecisionQuestionnaire:
    def __init__(self, provider):
        self.provider = provider

    async def build(self, gaps, context=None, previous=None, *, allow_model=True):
        context = context or {}

        # 只用同一原始依据上的显式业务补全消除缺口，不拿相似历史任务猜答案。
        def answered(gap):
            client_id = context.get("client_id")
            if not client_id:
                return False
            for item in context.get("progress", []):
                content = item.get("content", {})
                evidence = content.get("evidence", {})
                if (content.get("sessionId") or evidence.get("sessionId")) != client_id:
                    continue
                same_goal = (
                    item.get("kind") == "goal"
                    and content.get("title") == gap.get("subject")
                    and gap.get("origin_id") in content.get("evidenceIds", [])
                )
                if evidence.get("id") != gap.get("origin_id") and not same_goal:
                    continue
                value = content.get(gap["field"])
                if gap["field"] == "side" and value in {"ours", "theirs"}:
                    return True
                if (
                    gap["field"] in {"deadline", "acceptance"}
                    and value
                    and value != "待明确"
                ):
                    return True
            return False

        gaps = [g for g in gaps if not answered(g)]
        # 不因无关闲聊重新发问；缓存包含实际缺口和授权检索/有效业务记录的版本。
        key = digest(
            {
                "gaps": gaps,
                "client_id": context.get("client_id"),
                "knowledge": context.get("knowledge", []),
                "progress": context.get("progress", []),
            }
        )
        if previous and previous.get("input_digest") == key and not previous.get('deferred'):
            return {**previous, "cached": True}
        meta = questionnaire_skill()[1]
        if not gaps:
            return {
                "input_digest": key,
                "questions": [],
                "skill": meta,
                "applied": False,
                "cached": False,
            }
        if not allow_model:
            return {'input_digest': key, 'questions': [
                {**g, 'question': g['fallback'], 'why': ''} for g in gaps[:2]],
                'skill': meta, 'applied': False, 'cached': False, 'deferred': True}
        prompt = (
            questionnaire_skill()[0]
            + "\n只返回 JSON，严格遵守 Schema："
            + json.dumps(QuestionnaireDraft.model_json_schema(), ensure_ascii=False)
        )
        prompt += "\n候选 ID 必须来自 gaps；若历史依据已明确回答就不要再次询问。不得把身份分离当作用户可口头补全的声纹认证。不改写业务字段。"
        try:
            value = await self.provider.run(
                "questionnaire",
                prompt,
                {"gaps": gaps[:8], "historical_context": {
                    k: context[k] for k in ['client_id', 'progress', 'knowledge'] if k in context}},
            )
            draft = QuestionnaireDraft.model_validate(value)
            valid = {g["id"]: g for g in gaps}
            questions, seen = [], set()
            for q in draft.questions:
                if q.candidate_id not in valid or q.candidate_id in seen:
                    continue
                # 一个缺口最多一个问题，来源由服务端绑定，不由模型自行提供。
                g = valid[q.candidate_id]
                questions.append({**g, "question": q.question, "why": q.why})
                seen.add(q.candidate_id)
            if not questions:
                raise ValueError("unanswered_gaps_omitted")
            return {
                "input_digest": key,
                "questions": questions,
                "skill": meta,
                "applied": True,
                "cached": False,
            }
        except Exception as error:
            # 辅助格式化失败不抹掉已发现的缺口，也不重试整场会议。
            return {
                "input_digest": key,
                "questions": [
                    {**g, "question": g["fallback"], "why": ""} for g in gaps[:2]
                ],
                "skill": meta,
                "applied": False,
                "cached": False,
                "error": type(error).__name__,
            }


def meeting_gaps(report):
    gaps = []
    for item in report.get("commitments", []):
        for field, missing, question in [
            ("deadline", "交付时间待明确", "这项工作的交付日期是什么？"),
            ("side", "履约方待明确", "这项工作由我方还是对方履约？"),
        ]:
            if missing in item.get("missing", []):
                gaps.append(
                    {
                        "id": digest([item["origin_id"], field]),
                        "origin_id": item["origin_id"],
                        "field": field,
                        "subject": item["action"],
                        "evidence_ids": [item["origin_id"]],
                        "fallback": question,
                    }
                )
    for goal in report.get("goals", []):
        if not goal.get("acceptance") or goal["acceptance"] == "待明确":
            gaps.append(
                {
                    "id": digest([goal["origin_id"], "acceptance"]),
                    "origin_id": goal["origin_id"],
                    "field": "acceptance",
                    "subject": goal["title"],
                    "evidence_ids": goal["evidence_ids"],
                    "fallback": "这个目标的验收条件是什么？",
                }
            )
    return gaps[:8]
