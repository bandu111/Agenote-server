"""在应用授权过滤之后调用 LlamaIndex，不向外部解析服务发送文档。"""

import base64
import hashlib
import re
import tempfile
import zipfile
from pathlib import Path
from llama_index.core import Document
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.schema import TextNode
from llama_index.retrievers.bm25 import BM25Retriever


def fingerprint(document, version):
    if document.get('fileData') and not version['content']:
        return hashlib.sha256((str(document.get('fileHash')) + ':document-runtime-v2').encode()).hexdigest()
    return (
        document.get("fileHash")
        if document.get("fileData") and not version["content"]
        else version["hash"]
    )


def parse_nodes(owner, document, version):
    """版本文本不可改写；二进制只在原文为空时解析，人工文字版本独立索引。"""
    content = version["content"]
    documents = [Document(text=content)] if content.strip() else []
    if not documents and document.get("fileData"):
        raw = base64.b64decode(document["fileData"], validate=True)
        if len(raw) > 20 * 1024 * 1024 or hashlib.sha256(
            raw
        ).hexdigest() != document.get("fileHash"):
            raise ValueError("file_integrity_failed")
        suffix = Path(document.get("filename") or "").suffix.lower()
        from llama_index.readers.file import (
            PDFReader,
            DocxReader,
            PandasExcelReader,
            PptxReader,
            RTFReader,
        )

        readers = {
            ".pdf": PDFReader,
            ".docx": DocxReader,
            ".xlsx": PandasExcelReader,
            ".pptx": PptxReader,
            ".rtf": RTFReader,
        }
        from app.integrations.llama_index.document_runtime import preview_pdf, pdf_documents
        if suffix == '.pdf':
            documents = pdf_documents(raw)
        elif suffix in {'.doc', '.xls', '.ppt', '.odt', '.ods'}:
            documents = pdf_documents(preview_pdf(document))
        elif suffix not in readers:
            raise ValueError("unsupported_binary_format")
        # Office 文档是压缩包；只解析正常体量，避免解压炸弹耗尽进程。
        if suffix in {".docx", ".xlsx", ".pptx"}:
            import io

            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if (
                    len(archive.infolist()) > 10000
                    or sum(v.file_size for v in archive.infolist()) > 100 * 1024 * 1024
                ):
                    raise ValueError("document_expansion_limit")
        if not documents:
            with tempfile.TemporaryDirectory(prefix="yanshu-parse-") as directory:
                file = Path(directory) / ("source" + suffix)
                file.write_bytes(raw)
                reader = readers[suffix]()
                documents = reader.load_data(file=file)
    if not documents:
        return []
    if sum(len(d.text) for d in documents) > 2200000:
        raise ValueError("parsed_text_limit")
    # 不加载默认远程 LLM/Embedding；中文字符及英文词作为保守词法切分单位。
    splitter = SentenceSplitter(
        chunk_size=600,
        chunk_overlap=80,
        tokenizer=lambda text: re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+|[^\s]", text),
    )
    result = []
    for index, node in enumerate(splitter.get_nodes_from_documents(documents)):
        body = node.get_content()
        node.id_ = hashlib.sha256(
            f"{owner}:{document['id']}:{version['id']}:{index}:{body}".encode()
        ).hexdigest()
        node.metadata = {
            "document_id": document["id"],
            "version_id": version["id"],
            "hash": version["hash"],
            "file_hash": document.get("fileHash"),
            "project_id": document["projectId"],
            "title": version["title"],
            "page": node.metadata.get("page_label"),
            "source": document["source"],
            "chunk": index,
            "extraction": node.metadata.get('extraction', 'text'),
        }
        node.excluded_embed_metadata_keys = list(node.metadata)
        node.excluded_llm_metadata_keys = list(node.metadata)
        result.append(node)
    return result


def retrieve_nodes(nodes, query, limit):
    if not nodes or not re.search(r"[\w\u4e00-\u9fff]", query):
        return []
    retriever = BM25Retriever.from_defaults(
        nodes=nodes,
        similarity_top_k=min(limit, len(nodes)),
        skip_stemming=True,
        language="en",
        token_pattern=r"(?u)[\u4e00-\u9fff]|[a-zA-Z0-9_]+",
    )
    return [v for v in retriever.retrieve(query) if v.score is not None and v.score > 0]
