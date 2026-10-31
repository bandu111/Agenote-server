"""格式路由、OCR 页码、预览账户隔离与文件完整性。外部引擎实测独立记录。"""
import base64
import hashlib
import io
from unittest.mock import patch
import pytest
from pypdf import PdfWriter
from app.integrations.llama_index import document_runtime as runtime
from app.integrations.hermes.skill_catalog import document_processing_skills
from tests.test_meetings import account
from tests.test_knowledge import snapshot


def test_document_skills_are_pinned_and_checked():
    values = document_processing_skills()
    assert [m['name'] for m in values] == ['pdf', 'docx', 'xlsx', 'powerpoint']
    assert all(len(m['sha256']) == 64 for m in values)


def test_file_integrity_and_expansion_limits():
    with pytest.raises(ValueError, match='integrity'):
        runtime.file_bytes({'fileData': base64.b64encode(b'fake').decode(), 'fileHash': '0'*64})


def test_scanned_pdf_runs_ocr_per_page_and_keeps_page_number(monkeypatch):
    writer = PdfWriter()
    writer.add_blank_page(200, 300)
    out = io.BytesIO(); writer.write(out)
    calls = []
    monkeypatch.setattr(runtime, 'executable', lambda name: name)
    def run(args, directory, timeout):
        calls.append(args)
        if args[0] == 'tesseract':
            from pathlib import Path
            Path(args[2] + '.txt').write_text('合同要求双端交付', encoding='utf-8')
    monkeypatch.setattr(runtime, 'run', run)
    documents = runtime.pdf_documents(out.getvalue())
    assert documents[0].text == '合同要求双端交付'
    assert documents[0].metadata == {'page_label': '1', 'extraction': 'ocr'}
    assert len(calls) == 2 and 'chi_sim+eng' in calls[1]


async def test_preview_is_account_and_version_bound(client):
    first = await account(client)
    second = await account(client, 'preview-other@example.com')
    data = snapshot()
    raw = b'%PDF-1.7\nsynthetic-test'
    doc = data['documents'][0]
    doc.update(kind='pdf', filename='test.pdf', fileData=base64.b64encode(raw).decode(), fileHash=hashlib.sha256(raw).hexdigest())
    # 预览权限不依赖检索是否允许，私有资料仍能由主人查看。
    doc['allowed'] = False
    response = await client.post('/api/v1/knowledge/sync', headers=first, json={'revision': 0, 'snapshot': data})
    assert response.status_code == 200
    request = {'documentId': doc['id'], 'versionId': 'v1', 'hash': doc['versions'][0]['hash'], 'fileHash': doc['fileHash']}
    assert (await client.post('/api/v1/knowledge/preview', headers=second, json=request)).status_code == 404
    assert (await client.post('/api/v1/knowledge/preview', headers=first, json={**request, 'fileHash': '0'*64})).status_code == 409
    response = await client.post('/api/v1/knowledge/preview', headers=first, json=request)
    assert response.status_code == 200, response.text
    assert base64.b64decode(response.json()['data']['pdfData']) == raw


async def test_binary_indexing_is_queued_then_becomes_searchable(client, app):
    from tests.test_knowledge_parsers import binary_fixture
    from app.modules.knowledge.services.index_jobs import KnowledgeIndexJobs
    headers = await account(client, 'index-queue@example.com')
    data = snapshot()
    raw = binary_fixture('.pdf')
    doc = data['documents'][0]
    doc.update(kind='pdf', filename='contract.pdf', fileData=base64.b64encode(raw).decode(), fileHash=hashlib.sha256(raw).hexdigest())
    doc['versions'][0].update(content='', hash=hashlib.sha256(b'').hexdigest())
    response = await client.post('/api/v1/knowledge/sync', headers=headers, json={'revision': 0, 'snapshot': data})
    assert response.status_code == 200, response.text
    assert response.json()['data']['indexStates'][0]['status'] == 'queued'
    assert await KnowledgeIndexJobs(app.state.database).process_one()
    response = await client.post('/api/v1/knowledge/search', headers=headers, json={'query': 'Android contract'})
    assert response.status_code == 200 and response.json()['data']
