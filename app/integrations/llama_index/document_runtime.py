"""受限本机文档转换及逐页 OCR；不上传第三方服务、不执行文档宏。"""
import base64
import hashlib
import io
import os
import shutil
import subprocess
import tempfile
import threading
import zipfile
from pathlib import Path
from pypdf import PdfReader
from llama_index.core import Document

OFFICE = {'.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.odt', '.ods', '.rtf'}
_capacity = threading.BoundedSemaphore(2)


def file_bytes(document):
    try:
        raw = base64.b64decode(document.get('fileData') or '', validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError('file_integrity_failed') from error
    if not raw or len(raw) > 20 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != document.get('fileHash'):
        raise ValueError('file_integrity_failed')
    return raw


def check_container(raw, suffix):
    if suffix in {'.docx', '.xlsx', '.pptx', '.odt', '.ods'}:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            rows = archive.infolist()
            if len(rows) > 10000 or sum(r.file_size for r in rows) > 100 * 1024 * 1024:
                raise ValueError('document_expansion_limit')


def executable(name):
    found = shutil.which(name)
    if not found and name == 'soffice' and os.name == 'nt':
        candidate = Path('C:/Program Files/LibreOffice/program/soffice.exe')
        if candidate.is_file(): found = str(candidate)
    if not found: raise ValueError('document_runtime_unavailable')
    return found


def run(args, directory, timeout=70):
    if os.name != 'nt':
        # Linux 转换器没有网络，也看不到应用目录、密钥或用户主目录。
        sandbox = [executable('bwrap'), '--unshare-net', '--unshare-pid', '--die-with-parent',
            '--new-session', '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp']
        for system_path in ['/usr', '/bin', '/lib', '/lib64', '/etc']:
            if Path(system_path).exists(): sandbox += ['--ro-bind', system_path, system_path]
        args = [*sandbox, '--bind', str(directory), str(directory), '--chdir', str(directory), *args]
    process = subprocess.Popen(args, cwd=directory, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=os.name != 'nt',
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        env={'PATH': os.environ.get('PATH', ''), 'LANG': 'C.UTF-8', 'HOME': str(directory),
             'SystemRoot': os.environ.get('SystemRoot', ''), 'OMP_THREAD_LIMIT': '1', 'SAL_USE_VCLPLUGIN': 'svp'})
    try:
        if process.wait(timeout=timeout): raise ValueError('document_conversion_failed')
    except subprocess.TimeoutExpired as error:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            import signal
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        raise ValueError('document_conversion_timeout') from error


def convert_pdf(raw, suffix, directory):
    if suffix not in OFFICE: raise ValueError('unsupported_binary_format')
    check_container(raw, suffix)
    source = directory / ('source' + suffix)
    source.write_bytes(raw)
    profile = directory / 'profile' / 'user'
    profile.mkdir(parents=True)
    (profile / 'registrymodifications.xcu').write_text('''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item><item oor:path="/org.openoffice.Office.Common/Load"><prop oor:name="UpdateDocMode" oor:op="fuse"><value>0</value></prop></item></oor:items>''', encoding='utf-8')
    run([executable('soffice'), '-env:UserInstallation=' + profile.parent.as_uri(),
        '--headless', '--nologo', '--nodefault', '--norestore', '--convert-to', 'pdf', '--outdir', str(directory), str(source)], directory)
    output = directory / 'source.pdf'
    if not output.is_file() or output.stat().st_size > 30 * 1024 * 1024:
        raise ValueError('document_conversion_failed')
    result = output.read_bytes()
    if not result.startswith(b'%PDF-'): raise ValueError('invalid_preview_pdf')
    return result


def preview_pdf(document):
    raw = file_bytes(document)
    suffix = Path(document.get('filename') or '').suffix.lower()
    if suffix == '.pdf': return raw
    if not _capacity.acquire(timeout=5): raise ValueError('document_runtime_busy')
    try:
        with tempfile.TemporaryDirectory(prefix='yanshu-document-') as temp:
            return convert_pdf(raw, suffix, Path(temp))
    finally: _capacity.release()


def pdf_documents(raw):
    """只对缺少文本层的页 OCR，保留原页码，混合 PDF 不丢掉扫描页。"""
    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted: raise ValueError('encrypted_document')
    if len(reader.pages) > 200: raise ValueError('document_page_limit')
    documents, missing = [], []
    for index, page in enumerate(reader.pages):
        text = page.extract_text() or ''
        if len(''.join(text.split())) < 8:
            missing.append(index)
        documents.append(Document(text=text, metadata={'page_label': str(index + 1), 'extraction': 'text'}))
    if len(missing) > 40: raise ValueError('document_ocr_page_limit')
    if missing:
        if not _capacity.acquire(timeout=5): raise ValueError('document_runtime_busy')
        try:
            with tempfile.TemporaryDirectory(prefix='yanshu-ocr-') as temp:
                directory = Path(temp)
                source = directory / 'source.pdf'
                source.write_bytes(raw)
                for index in missing:
                    target = directory / ('page-' + str(index + 1))
                    run([executable('pdftoppm'), '-f', str(index+1), '-l', str(index+1), '-singlefile', '-scale-to', '2400', '-png', str(source), str(target)], directory, 25)
                    run([executable('tesseract'), str(target) + '.png', str(target), '-l', 'chi_sim+eng', '--psm', '3'], directory, 35)
                    text_file = target.with_suffix('.txt')
                    text = text_file.read_text(encoding='utf-8') if text_file.exists() else ''
                    documents[index] = Document(text=text, metadata={'page_label': str(index+1), 'extraction': 'ocr'})
        finally: _capacity.release()
    return documents
