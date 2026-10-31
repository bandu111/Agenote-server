"""Protocol pinned to installed Hermes f97608f / Weixin channel 2.2.0.

No administrator credentials/config are loaded. Each binding owns its bot token.
"""
import base64
import json
import secrets
from urllib.parse import urlparse
import httpx
from cryptography.fernet import Fernet

BASE='https://ilinkai.weixin.qq.com'
class ChannelError(Exception):
    def __init__(self,code,*,uncertain=False):
        self.code,self.uncertain=str(code),uncertain
        super().__init__(self.code)

def allowed_base(value):
    parsed=urlparse(value)
    host=parsed.hostname or ''
    if (parsed.scheme!='https' or parsed.username or parsed.password or parsed.port not in [None,443]
        or not (host=='ilinkai.weixin.qq.com' or host.endswith('.ilinkai.weixin.qq.com'))
        or parsed.path not in ['', '/'] or parsed.query or parsed.fragment):
        raise ChannelError('invalid_channel_host')
    return value.rstrip('/')

class Cipher:
    def __init__(self,key): self.fernet=Fernet(key.encode() if isinstance(key,str) else key)
    def encode(self,value): return self.fernet.encrypt(json.dumps(value,ensure_ascii=False).encode()).decode()
    def decode(self,value): return json.loads(self.fernet.decrypt(value.encode()))

class WeixinTransport:
    def __init__(self): self.http=httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=8)
    async def close(self): await self.http.aclose()
    async def request(self,method,endpoint,*,base=BASE,token=None,payload=None,params=None,sending=False):
        headers={'iLink-App-Id':'bot','iLink-App-ClientVersion':str((2<<16)|(2<<8)),
            'Content-Type':'application/json'}
        if token:
            headers.update({'Authorization':'Bearer '+token,'AuthorizationType':'ilink_bot_token',
                'X-WECHAT-UIN':base64.b64encode(str(secrets.randbits(32)).encode()).decode()})
        try:
            response=await self.http.request(method,allowed_base(base)+'/'+endpoint,headers=headers,
                json={**payload,'base_info':{'channel_version':'2.2.0'}} if payload is not None else None,
                params=params)
            if response.status_code>=500: raise ChannelError('upstream_5xx',uncertain=sending)
            if response.status_code!=200: raise ChannelError('http_'+str(response.status_code))
            value=response.json()
            if not isinstance(value,dict): raise ChannelError('invalid_response',uncertain=sending)
            codes=[value[k] for k in ['ret','errcode'] if value.get(k) is not None]
            if any(code!=0 for code in codes): raise ChannelError(next(str(c) for c in codes if c!=0))
            return value
        except (httpx.TimeoutException,httpx.NetworkError):
            raise ChannelError('transport_timeout',uncertain=sending) from None
        except (ValueError,TypeError):
            raise ChannelError('invalid_response',uncertain=sending) from None
    async def qr(self):
        result=await self.request('GET','ilink/bot/get_bot_qrcode',params={'bot_type':'3'})
        if not result.get('qrcode') or not result.get('qrcode_img_content'):
            raise ChannelError('missing_qr')
        return result
    async def poll_qr(self,code,base=BASE):
        return await self.request('GET','ilink/bot/get_qrcode_status',base=base,params={'qrcode':code})
    async def updates(self,creds):
        return await self.request('POST','ilink/bot/getupdates',base=creds['base'],token=creds['token'],
            payload={'get_updates_buf':creds.get('cursor','')})
    async def send(self,creds,text,client_id):
        if not creds.get('context'): raise ChannelError('context_required')
        return await self.request('POST','ilink/bot/sendmessage',base=creds['base'],token=creds['token'],
            payload={'msg':{'from_user_id':'','to_user_id':creds['peer'],'client_id':client_id,
                'message_type':2,'message_state':2,'context_token':creds['context'],
                'item_list':[{'type':1,'text_item':{'text':text[:1800]}}]}},sending=True)

    async def send_item(self,creds,item,client_id):
        if not creds.get('context'):raise ChannelError('context_required')
        return await self.request('POST','ilink/bot/sendmessage',base=creds['base'],token=creds['token'],
            payload={'msg':{'from_user_id':'','to_user_id':creds['peer'],'client_id':client_id,
                'message_type':2,'message_state':2,'context_token':creds['context'],'item_list':[item]}},sending=True)
