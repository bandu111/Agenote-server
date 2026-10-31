"""同一行动的问题合并展示，不合并业务字段和其他行动。"""
from copy import deepcopy
from app.modules.meetings.services.meeting_agents import append_questions


def question(identifier, origin, subject, field, wording):
    return {'id': identifier, 'origin_id': origin, 'subject': subject, 'field': field,
        'question': wording, 'evidence_ids': [origin]}


def test_one_action_missing_owner_and_deadline_has_one_card_but_two_fields():
    questions = [question('one', 'u1', '提交报告', 'deadline', '交付日期是什么？'),
        question('two', 'u1', '提交报告', 'side', '由我方还是对方履约？')]
    result = {'questionnaire': {'questions': deepcopy(questions)}, 'insights': []}
    append_questions(result)
    assert len(result['insights']) == 1
    assert '交付日期' in result['insights'][0]['body']
    assert '我方还是对方' in result['insights'][0]['body']
    assert result['insights'][0]['evidence_ids'] == ['u1']
    assert result['questionnaire']['questions'] == questions
    identifier = result['insights'][0]['id']
    append_questions(result)
    assert len(result['insights']) == 1 and result['insights'][0]['id'] == identifier


def test_distinct_actions_or_origins_are_not_merged_and_existing_risk_remains():
    risk = {'id': 'risk', 'kind': 'risk', 'body': '独立风险'}
    result = {'insights': [risk], 'questionnaire': {'questions': [
        question('one', 'u1', '提交报告', 'deadline', '交付日期是什么？'),
        question('two', 'u1', '修复上传', 'side', '由谁履约？'),
        question('three', 'u2', '提交报告', 'deadline', '交付日期是什么？')]}}
    append_questions(result)
    assert len(result['insights']) == 4
    assert result['insights'][0] == risk
