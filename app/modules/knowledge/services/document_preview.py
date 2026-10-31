"""预览只读取认证账户已保存的指定版本，不接受任意路径或外部 URL。"""
import asyncio
import base64
import hashlib
from fastapi import HTTPException
from app.integrations.llama_index.document_runtime import preview_pdf
from app.modules.knowledge.repositories.knowledge_repository import KnowledgeRepository


class DocumentPreviewService:
    def __init__(self, session):
        self.repository = KnowledgeRepository(session)

    async def preview(self, owner, request):
        row = await self.repository.library(owner)
        document = next((d for d in (row.snapshot['documents'] if row else []) if d['id'] == request.documentId), None)
        version = next((v for v in document['versions'] if v['id'] == request.versionId), None) if document else None
        if not document or not version: raise HTTPException(404, 'document_not_found')
        if version['hash'] != request.hash or document.get('fileHash') != request.fileHash:
            raise HTTPException(409, 'document_version_changed')
        try:
            result = await asyncio.to_thread(preview_pdf, document)
        except ValueError as error:
            raise HTTPException(422, 'document_preview_unavailable') from error
        # 转换期间切换或删除版本，不能把旧结果当当前文件返回。
        await self.repository.session.refresh(row)
        latest = next((d for d in row.snapshot['documents'] if d['id'] == request.documentId), None)
        if not latest or latest.get('fileHash') != request.fileHash or not any(v['id'] == request.versionId and v['hash'] == request.hash for v in latest['versions']):
            raise HTTPException(409, 'document_version_changed')
        return {'pdfData': base64.b64encode(result).decode(), 'pdfHash': hashlib.sha256(result).hexdigest(),
                'documentId': request.documentId, 'versionId': request.versionId, 'sourceHash': request.fileHash}
