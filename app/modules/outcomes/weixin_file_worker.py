"""One-shot SDK document transfer; no Agent, workspace tools or shared memory."""
import asyncio,contextlib,io,json,os,sys
from pathlib import Path

async def run(value):
    # Must stay within the private PDF root; no client-provided arbitrary file reads.
    root=Path(value['root']).resolve();path=Path(value['path']).resolve()
    if not path.is_relative_to((root/'storage/outcome-pdfs').resolve()) or path.suffix!='.pdf':
        raise ValueError('invalid_pdf_path')
    creds=value['credentials']
    if not creds.get('context'):raise ValueError('context_required')
    # Reuse encryption/CDN upload only. Final delivery stays in our strict
    # context-token transport; SDK's tokenless retry path is never invoked.
    import base64,hashlib,secrets
    from urllib.parse import quote,urlparse
    from gateway.platforms.weixin import (_new_session,_get_upload_url,_aes128_ecb_encrypt,
        _upload_ciphertext,_file_item,MEDIA_FILE,WEIXIN_CDN_BASE_URL)
    data=path.read_bytes();aes=secrets.token_bytes(16);filekey=secrets.token_hex(16)
    md5=hashlib.md5(data).hexdigest()
    async with _new_session() as session:
        response=await _get_upload_url(session,base_url=creds['base'],token=creds['token'],
            to_user_id=creds['peer'],media_type=MEDIA_FILE,filekey=filekey,rawsize=len(data),
            rawfilemd5=md5,filesize=((len(data)+16)//16)*16,aeskey_hex=aes.hex())
        encrypted=_aes128_ecb_encrypt(data,aes)
        parameter=str(response.get('upload_param') or '')
        url=str(response.get('upload_full_url') or '') or (WEIXIN_CDN_BASE_URL+'/upload?encrypted_query_param='+
            quote(parameter,safe='')+'&filekey='+quote(filekey,safe='') if parameter else '')
        parsed=urlparse(url)
        if (parsed.scheme!='https' or parsed.username or parsed.password or
            not (parsed.hostname or '').endswith('.cdn.weixin.qq.com')):
            raise ValueError('untrusted_upload_host')
        query=await _upload_ciphertext(session,ciphertext=encrypted,upload_url=url)
        item=_file_item(encrypt_query_param=query,aes_key_for_api=base64.b64encode(aes.hex().encode()).decode(),
            ciphertext_size=len(encrypted),plaintext_size=len(data),filename=path.name,rawfilemd5=md5)
        return {'success':True,'item':item}

if __name__=='__main__':
    target=sys.stdout
    try:
        value=json.loads(sys.stdin.read())
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            result=asyncio.run(run(value))
        target.write(json.dumps(result))
    except Exception as error:
        target.write(json.dumps({'success':False,'error':str(error) if str(error) in {'invalid_pdf_path','context_required','untrusted_upload_host'} else type(error).__name__}))
