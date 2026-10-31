"""正常会议措辞能触发日程规划；触发不等于审批或执行。"""
import pytest
from app.modules.meetings.services.action_planner import has_calendar_cue, plan_actions
from app.modules.meetings.services.meeting_agents import analysis_triggers


@pytest.mark.parametrize('words', [
    '确定在2026年10月28日下午两点到两点半开上线评估会，请创建日历安排。',
    '我们周三下午两点举行上线评审，三点结束。',
    '请在日历中安排明天下午三点到四点的沟通。',
    '先考虑周三开评估会，还没有确定。',
    '不要创建日历安排。',
])
def test_calendar_wording_reaches_planner_without_deciding_approval(words):
    assert has_calendar_cue(words)
    assert 'planner' in analysis_triggers([{'id': 'a', 'raw': words, 'turn_status': 'complete'}])


def test_ordinary_progress_does_not_trigger_calendar():
    assert not has_calendar_cue('工单的图片上传功能已经开发完成，目前继续测试。')


@pytest.mark.asyncio
async def test_normal_calendar_request_and_human_correction_reach_existing_model_contract():
    class Provider:
        calls = []
        async def run(self, role, system, payload):
            self.calls.append((role, payload))
            # 模型判定为提议时，只保留候选语气，不升级成确认。
            return {'calendar': [{'title': '上线评估', 'start': '2026-10-28T14:00:00+08:00',
                'end': '2026-10-28T14:30:00+08:00', 'status': 'proposed', 'evidence_ids': ['a']}]}
    provider = Provider()
    result = await plan_actions(provider, [{'id': 'a', 'raw': '旧转写',
        'correction': '考虑周三开上线评估会，还没确定。'}], {'meeting_started_at': '2026-10-22T13:00:00+08:00'})
    assert len(provider.calls) == 1
    assert provider.calls[0][0] == 'planner'
    assert result[0]['status'] == 'proposed'
