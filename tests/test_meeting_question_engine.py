import unittest
from app.modules.outcomes.question_engine import checked, analyze_question

class EvidenceTests(unittest.TestCase):
    def test_rejects_fabricated_quote(self):
        with self.assertRaises(ValueError):
            checked({'coverage':'explicit','evidence':[{'id':'utterance:a','quote':'没有丢失'}]},
                {'utterance:a':{'text':'需要继续调查'}})
    def test_rejects_unowned_reference(self):
        with self.assertRaises(ValueError):
            checked({'coverage':'explicit','evidence':[{'id':'other','quote':'原话'}]}, {})
    def test_keeps_audio_anchor(self):
        _, citations = checked({'coverage':'explicit','evidence':[{'id':'utterance:a','quote':'十秒'}]},
            {'utterance:a':{'text':'重试间隔十秒','at':12000,'speaker':'B'}})
        self.assertEqual(citations[0]['id'],'a')
        self.assertEqual(citations[0]['atMs'],12000)
    def test_cannot_assert_answer_without_evidence(self):
        with self.assertRaises(ValueError): checked({'coverage':'explicit','text':'已解决'}, {})

class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_meeting_answer_needs_no_search(self):
        class Provider:
            async def run(self,*args):
                return {'question':'影响业务吗','text':'会延迟十秒','coverage':'explicit',
                    'evidence':[{'id':'utterance:a','quote':'延迟十秒'}]}
        result=await analyze_question(Provider(),None,'owner',{'evidence':{
            'utterance:a':{'text':'延迟十秒','at':12000}},
            'markedQuestion':{'text':'影响业务吗','sourceIds':['a']}})
        self.assertEqual(result['questionAnalysis']['state'],'answered')
    async def test_empty_question_stays_unconfirmed(self):
        class Provider:
            async def run(self,*args): return {'candidates':['是否有影响？'],'text':'请确认问题'}
        result=await analyze_question(Provider(),None,'owner',{'evidence':{},
            'markedQuestion':{'text':'','sourceIds':[]}})
        self.assertEqual(result['questionAnalysis']['state'],'awaiting_confirmation')

class WechatQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_questions_require_selection(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from app.modules.outcomes.wechat_jobs import WechatJobs
        session=SimpleNamespace(scalar=AsyncMock())
        binding=SimpleNamespace(state={'questionPointers':['a','b']}, owner_id='owner')
        worker=WechatJobs(None,None,None)
        await worker.receive(session,binding,{'peer':'peer','bot':'bot'}, {'msgs':[{
            'from_user_id':'peer','to_user_id':'bot','message_id':'m1',
            'item_list':[{'type':1,'text_item':{'text':'请继续分析'}}]}]})
        session.scalar.assert_not_awaited()
        self.assertIn('疑问编号',binding.state['reply']['text'])
    async def test_question_followup_reuses_question_record(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from app.modules.outcomes.wechat_jobs import WechatJobs
        record=SimpleNamespace(id='q',version=1,status='succeeded',content={
            'kind':'meeting_question','question':{'text':'原始问题','status':'pending'}})
        session=SimpleNamespace(scalar=AsyncMock(return_value=record))
        binding=SimpleNamespace(state={'questionPointers':['q']}, owner_id='owner')
        worker=WechatJobs(None,None,None)
        await worker.receive(session,binding,{'peer':'peer','bot':'bot'}, {'msgs':[{
            'from_user_id':'peer','to_user_id':'bot','message_id':'m2',
            'item_list':[{'type':1,'text_item':{'text':'如何验证？'}}]}]})
        self.assertEqual(record.status,'queued')
        self.assertEqual(record.content['request'],'如何验证？')
        self.assertEqual(record.content['question']['text'],'原始问题')
    async def test_other_wechat_sender_is_ignored(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from app.modules.outcomes.wechat_jobs import WechatJobs
        session=SimpleNamespace(scalar=AsyncMock())
        binding=SimpleNamespace(state={},owner_id='owner')
        await WechatJobs(None,None,None).receive(session,binding,{'peer':'allowed'},
            {'msgs':[{'from_user_id':'someone_else','message_id':'m3'}]})
        session.scalar.assert_not_awaited()
        self.assertEqual(binding.state,{})

    async def test_requeued_question_is_not_sent_as_completed(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from app.modules.outcomes.wechat_jobs import WechatJobs
        report=SimpleNamespace(status='queued',content={'result':None,'kind':'meeting_question'})
        session=SimpleNamespace(scalars=AsyncMock(return_value=[report]))
        binding=SimpleNamespace(owner_id='owner',state={'boundAt':'2026-01-01'})
        transport=SimpleNamespace(send=AsyncMock())
        await WechatJobs(None,None,transport).deliver(session,binding,{'context':'test'})
        transport.send.assert_not_awaited()

class QuestionRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_reference_repaired_without_weakening_validation(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        p=SimpleNamespace(run=AsyncMock(side_effect=[
            {'coverage':'explicit','evidence':[{'id':'other','quote':'invented'}]},
            {'question':'影响多久？','text':'延迟十秒','coverage':'explicit',
             'evidence':[{'id':'utterance:a','quote':'延迟十秒'}]}]))
        result=await analyze_question(p,None,'owner',{'evidence':{'utterance:a':{'text':'延迟十秒','at':12000}},
            'markedQuestion':{'text':'影响多久？','sourceIds':['a']}})
        self.assertEqual(p.run.await_count,2)
        self.assertEqual(result['questionAnalysis']['state'],'answered')
    async def test_repeated_invalid_reference_still_fails(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        p=SimpleNamespace(run=AsyncMock(return_value={'coverage':'explicit',
            'evidence':[{'id':'other','quote':'invented'}]}))
        with self.assertRaises(ValueError):
            await analyze_question(p,None,'owner',{'evidence':{},'markedQuestion':{'text':'问题？'}})
        self.assertEqual(p.run.await_count,2)
