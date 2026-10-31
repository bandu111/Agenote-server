import hashlib
import secrets
from datetime import timedelta
from fastapi import HTTPException
from sqlalchemy import select
from app.core.database.mixins import now
from app.modules.outcomes.wechat_models import WechatBinding
from app.modules.outcomes.weixin_transport import Cipher,BASE,allowed_base

def cipher_for(settings):
    key=getattr(settings,'wechat_binding_key',None)
    if not key: raise HTTPException(503,'wechat_binding_key_unconfigured')
    return Cipher(key.get_secret_value())

def binding_output(row,configured=True):
    state=row.state if row else {}
    status=row.status if row else 'unbound'
    expired=status in ['pending','scanned'] and state.get('expiresAt','') <= now().isoformat()
    if expired: status='expired'
    return {'channel':'weixin','bindingStatus':status,'canBind':configured,
        'canSend':bool(row and row.status=='bound' and state.get('contextReady')),
        'version':row.version if row else 0,
        'qr':state.get('qr') if status in ['pending','scanned'] else None,
        'expiresAt':state.get('expiresAt'), 'boundAt':state.get('boundAt'),
        'peerLabel':('已验证微信 · '+row.peer_hash[:8]) if row and row.peer_hash else None,
        'preferences':{'autoMeeting':True,'importantOnly':True,'aiDone':True,'paused':False,'sendPdf':True,'preMeeting':True,**state.get('preferences',{})},
        'message':state.get('message') or ('请先在微信助手中发送一条消息，以建立推送上下文。'
            if status=='bound' and not state.get('contextReady') else ''),
        'assistantName':'Agenote 工作助手'}

class WechatService:
    def __init__(self,session,settings,transport):
        self.session,self.settings,self.transport=session,settings,transport
    async def row(self,owner,lock=False):
        query=select(WechatBinding).where(WechatBinding.owner_id==owner)
        if lock:query=query.with_for_update()
        return await self.session.scalar(query.execution_options(populate_existing=True))
    async def create_qr(self,owner):
        cipher_for(self.settings)
        # Row creation is serialized even when the account has no binding yet.
        from sqlalchemy import text
        await self.session.execute(text('SELECT pg_advisory_xact_lock(hashtext(:key))'),
                                   {'key':'wechat-bind:'+owner})
        row=await self.row(owner,True)
        if row and row.status=='bound':raise HTTPException(409,'wechat_already_bound')
        if row and row.status in ['pending','scanned'] and row.state.get('expiresAt','')>now().isoformat():
            return binding_output(row)
        result=await self.transport.qr()
        if not row:
            row=WechatBinding(owner_id=owner,version=0,state={});self.session.add(row)
        row.version+=1;row.status='pending';row.encrypted=None;row.peer_hash=None
        row.state={'qr':str(result['qrcode_img_content']),'code':str(result['qrcode']),
            'generation':secrets.token_hex(16),'expiresAt':(now()+timedelta(seconds=120)).isoformat(),
            'base':BASE,'nextAt':now().isoformat(),'preferences':row.state.get('preferences',
                {'autoMeeting':True,'importantOnly':True,'aiDone':True,'paused':False,'sendPdf':True,'preMeeting':True})}
        await self.session.commit()
        return binding_output(row)
    async def unbind(self,owner,version):
        row=await self.row(owner,True)
        if row:
            if row.version!=version:raise HTTPException(409,'wechat_binding_changed')
            row.version+=1;row.status='unbound';row.peer_hash=None;row.encrypted=None
            row.state={'preferences':{**row.state.get('preferences',{}),'paused':True}}
            await self.session.commit()
        return binding_output(row)
    async def preferences(self,owner,version,preferences):
        row=await self.row(owner,True)
        if not row or row.status!='bound':raise HTTPException(409,'wechat_not_bound')
        if row.version!=version:raise HTTPException(409,'wechat_binding_changed')
        defaults={'autoMeeting':True,'importantOnly':True,'aiDone':True,
            'paused':False,'sendPdf':True,'preMeeting':True}
        row.version+=1;row.state={**row.state,'preferences':{
            **defaults,**row.state.get('preferences',{}),**preferences}}
        await self.session.commit()
        return binding_output(row)

def verified_credentials(value):
    # Only called with the real iLink authorization response, never client JSON.
    bot,token,peer=(str(value.get(k) or '').strip() for k in ['ilink_bot_id','bot_token','ilink_user_id'])
    if not all([bot,token,peer]):raise ValueError('incomplete_verified_identity')
    return {'bot':bot,'token':token,'peer':peer,'base':allowed_base(value.get('baseurl') or BASE),
            'cursor':'','context':None}

def peer_hash(peer):return hashlib.sha256(peer.encode()).hexdigest()
