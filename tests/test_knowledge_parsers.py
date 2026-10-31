"""合成文件验证真实 LlamaIndex 解析，无客户文件、无远程解析服务。"""

import base64
import hashlib
import io
import zipfile
import pytest
from app.integrations.llama_index.knowledge_index import parse_nodes


def binary_fixture(suffix):
    text = "Android iOS contract Windows requires approval"
    stream = io.BytesIO()
    if suffix == ".docx":
        with zipfile.ZipFile(stream, "w") as z:
            z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>' + text + '</w:t></w:r></w:p></w:body></w:document>')
        return stream.getvalue()
    if suffix == ".xlsx":
        from openpyxl import Workbook
        workbook = Workbook()
        workbook.active.append(["scope"])
        workbook.active.append([text])
        workbook.save(stream)
        return stream.getvalue()
    if suffix == ".pptx":
        from pptx import Presentation
        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = text
        presentation.save(stream)
        return stream.getvalue()
    if suffix == ".pdf":
        objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
            b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
            b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
        body = f'BT /F1 12 Tf 40 700 Td ({text}) Tj ET'.encode()
        objects.append(b'<< /Length ' + str(len(body)).encode() + b' >>\nstream\n' + body + b'\nendstream')
        pdf = bytearray(b'%PDF-1.4\n')
        offsets = [0]
        for i, obj in enumerate(objects, 1):
            offsets.append(len(pdf))
            pdf += f'{i} 0 obj\n'.encode() + obj + b'\nendobj\n'
        xref = len(pdf)
        pdf += b'xref\n0 6\n0000000000 65535 f \n'
        pdf += b''.join(f'{v:010d} 00000 n \n'.encode() for v in offsets[1:])
        pdf += f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode()
        return bytes(pdf)
    return b'unsupported legacy file'


@pytest.mark.parametrize("suffix", [".pdf", ".docx", ".xlsx", ".pptx"])
def test_binary_reader_extracts_real_content_and_version_bound_nodes(suffix):
    raw = binary_fixture(suffix)
    doc = {"id": "synthetic", "filename": "synthetic" + suffix, "fileData": base64.b64encode(raw).decode(),
        "fileHash": hashlib.sha256(raw).hexdigest(), "projectId": "test", "source": "synthetic-file"}
    version = {"id": "v1", "title": "Synthetic contract", "content": "", "hash": hashlib.sha256(b'').hexdigest()}
    nodes = parse_nodes("synthetic-owner", doc, version)
    assert nodes and "Android" in " ".join(n.text for n in nodes)
    assert nodes[0].metadata["version_id"] == "v1"
    assert nodes[0].metadata["file_hash"] == doc["fileHash"]


def test_invalid_legacy_doc_fails_instead_of_fake_index():
    raw = binary_fixture(".doc")
    with pytest.raises(ValueError, match="document_conversion_failed|document_runtime_unavailable"):
        parse_nodes("synthetic-owner", {"id": "d", "filename": "legacy.doc", "fileData": base64.b64encode(raw).decode(),
            "fileHash": hashlib.sha256(raw).hexdigest(), "projectId": "p", "source": "fixture"},
            {"id": "v", "title": "legacy", "content": "", "hash": hashlib.sha256(b'').hexdigest()})
