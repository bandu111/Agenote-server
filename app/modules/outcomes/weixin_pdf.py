import asyncio,json,os,sys
from pathlib import Path
from app.core.config import ROOT
from app.modules.outcomes.weixin_transport import ChannelError

async def upload_pdf_sdk(settings,creds,path,owner):
    home=ROOT/'storage/wechat-file-runtime'/owner
    home.mkdir(parents=True,exist_ok=True,mode=0o700)
    env=os.environ.copy()
    env.update(HERMES_HOME=str(home),PYTHONPATH=str(settings.hermes_runtime_path),PYTHONIOENCODING='utf-8')
    for key in ['HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy']:
        env.pop(key,None)
    process=await asyncio.create_subprocess_exec(sys.executable,
        str(Path(__file__).with_name('weixin_file_worker.py')),stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,env=env)
    from app.integrations.hermes.provider import lower_worker_priority
    lower_worker_priority(process)
    try:
        out,_=await asyncio.wait_for(process.communicate(json.dumps({
            'root':str(ROOT),'path':str(path),'credentials':creds}).encode()),120)
        value=json.loads(out)
        if value.get('success') is not True:raise ChannelError(str(value.get('error','pdf_upload_failed')))
        return value
    except (asyncio.TimeoutError,ValueError,TypeError):
        raise ChannelError('pdf_upload_failed') from None
    finally:
        if process.returncode is None:process.kill();await process.wait()
