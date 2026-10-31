import pytest
from app.modules.meetings.services.insight_clusters import cluster_insights


def hint(identifier, topic, issue, body='需要核对依据。', attention='normal', kind='question'):
    return dict(id=identifier, topic_key=topic, issue_key=issue, attention=attention,
        kind=kind, title='待核对', body=body, evidence_ids=[identifier])


def test_dynamic_objects_merge_without_scenario_table():
    for topic in ['仓储批次', '报销单据', '课程排期']:
        result = cluster_insights([hint('a', topic, '依据不齐'), hint('b', topic, '依据不齐', '先补依据再确认。')])
        assert len(result) == 1
        assert result[0]['id'] == 'a'
        assert result[0]['evidence_ids'] == ['a', 'b']
        assert result[0]['body'] == '先补依据再确认。'


def test_distinct_targets_concerns_and_constraints_survive():
    result = cluster_insights([hint('a', '甲', '凭据'), hint('b', '乙', '凭据'), hint('c', '甲', '金额'),
        hint('d', '甲', '交期', '需要3天。'), hint('e', '甲', '交期', '需要7天。')])
    assert len(result) == 5


def test_defer_is_not_published_and_memory_not_collapsed():
    result = cluster_insights([hint('routine', '事项', '细节', attention='defer'),
        hint('a', '事项', '话题接回', kind='stitch'), hint('b', '事项', '话题接回', kind='stitch'),
        hint('important', '事项', '实际冲突', attention='urgent', kind='risk')])
    assert [i['id'] for i in result] == ['a', 'b', 'important']


def test_defer_never_flashes_during_streaming_and_keys_survive():
    import json
    from app.modules.meetings.services.streaming_insights import streaming_insights
    item = hint('u', '对象', '问题', attention='defer')
    prefix = json.dumps({'insights': [item]}, ensure_ascii=False)
    assert streaming_insights(prefix, {'u'}) == []
    item['attention'] = 'normal'
    result = streaming_insights(json.dumps({'insights': [item]}, ensure_ascii=False), {'u'})
    assert result[0]['topic_key'] == '对象'
    assert result[0]['issue_key'] == '问题'


@pytest.mark.asyncio
async def test_merged_partial_keeps_streaming_with_union_of_evidence():
    from app.modules.meetings.services.progressive_review import ProgressiveReview
    sources = [dict(id=key, raw='有实际约束需要核对。', speaker='匿名A', at=0) for key in ['a', 'b']]
    reviewer = ProgressiveReview(None, sources, {})
    reviewer.previews = {'state': {'insights': [hint('a', '对象', '问题')]},
        'forecast': {'insights': [{**hint('b', '对象', '问题', '正在补充更完整的建议'), 'streaming': True}]}}
    result = reviewer.snapshot()['insights']
    assert len(result) == 1
    assert result[0]['streaming'] is True
    assert set(result[0]['evidence_ids']) == {'a', 'b'}


def test_near_duplicate_issue_keys_merge_and_keep_richer_assessment():
    a = {**hint('a', 'ERP接口', '确认超时影响', 'ERP接口偶尔超时，需确认是否影响同步结果及上线。'), 'title': '确认ERP接口超时影响'}
    b = {**hint('b', 'ERP接口', '业务影响待评估', 'ERP接口偶尔超时，尚未回答对业务的实际影响，需补充影响范围及是否阻塞上线。'), 'title': 'ERP接口超时影响待评估', 'evidence_ids': ['a']}
    result = cluster_insights([a, b])
    assert len(result) == 1
    assert result[0]['id'] == 'a'
    assert '实际影响' in result[0]['body']
