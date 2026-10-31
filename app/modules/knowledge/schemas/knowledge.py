"""沿用 Flutter 的版本与文件格式，只开放已定义字段。"""

from datetime import datetime
from typing import Literal
from pydantic import Field, model_validator
from app.modules.meetings.schemas.meeting import StrictModel


class Project(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=120)
    allowed: bool = True  # Legacy wire field; no retrieval permission gate.


class Folder(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=120)
    parentId: str | None = Field(default=None, max_length=100)


class SourceLink(StrictModel):
    kind: Literal["document", "voice"]
    targetId: str = Field(min_length=1, max_length=160)
    versionId: str = Field(min_length=1, max_length=160)
    hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class Version(StrictModel):
    id: str = Field(min_length=1, max_length=160)
    number: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=240)
    content: str = Field(max_length=2200000)
    hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    createdAt: datetime
    state: Literal["current", "review", "stale", "conflicted", "retracted", "unknown"]
    reason: str = Field(default="", max_length=2000)
    origin: str = Field(default="", max_length=500)
    sources: list[SourceLink] = Field(default_factory=list, max_length=100)
    validUntil: datetime | None = None


class AudioReference(StrictModel):
    id: str = Field(max_length=36)
    hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    duration: int = Field(gt=0)
    size: int = Field(gt=0)


class Document(StrictModel):
    audioRefs: list[AudioReference] = Field(default_factory=list, max_length=100)
    id: str = Field(min_length=1, max_length=160)
    folderId: str = Field(max_length=100)
    projectId: str = Field(max_length=100)
    kind: Literal[
        "markdown",
        "text",
        "pdf",
        "word",
        "spreadsheet",
        "presentation",
        "office",
        "note",
        "link",
        "audio",
    ]
    source: str = Field(max_length=500)
    createdAt: datetime
    versions: list[Version] = Field(min_length=1, max_length=100)
    selectedVersionId: str = Field(max_length=160)
    fileData: str | None = Field(default=None, max_length=28000000)
    fileHash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    filename: str | None = Field(default=None, max_length=255)
    url: str | None = Field(default=None, max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=50)
    allowed: bool = True  # Legacy wire field; no retrieval permission gate.
    archived: bool = False
    demo: bool = False
    ownerUserId: str | None = Field(default=None, max_length=36)


class Audit(StrictModel):
    documentId: str = Field(max_length=160)
    action: str = Field(max_length=1000)
    at: datetime


class LibrarySnapshot(StrictModel):
    schema_version: Literal[1] = Field(alias="schema")
    folders: list[Folder] = Field(max_length=1000)
    projects: list[Project] = Field(min_length=1, max_length=1000)
    documents: list[Document] = Field(max_length=1000)
    audit: list[Audit] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def validate_structure(self):
        folders = {v.id: v for v in self.folders}
        projects = {v.id for v in self.projects}
        if (
            len(folders) != len(self.folders)
            or "inbox" not in folders
            or len(projects) != len(self.projects)
        ):
            raise ValueError("目录或项目结构无效")
        for folder in self.folders:
            seen = {folder.id}
            parent = folder.parentId
            while parent is not None:
                if parent not in folders or parent in seen:
                    raise ValueError("目录不可循环引用")
                seen.add(parent)
                parent = folders[parent].parentId
        if len({d.id for d in self.documents}) != len(self.documents):
            raise ValueError("资料 ID 重复")
        for doc in self.documents:
            ids = {v.id for v in doc.versions}
            if (
                doc.folderId not in folders
                or doc.projectId not in projects
                or len(ids) != len(doc.versions)
                or doc.selectedVersionId not in ids
            ):
                raise ValueError("资料引用结构无效")
            if (
                len({v.number for v in doc.versions}) != len(doc.versions)
                or sum(v.state == "current" for v in doc.versions) > 1
            ):
                raise ValueError("版本号或有效状态冲突")
            if any(
                v.createdAt.tzinfo is None
                or (v.validUntil is not None and v.validUntil.tzinfo is None)
                for v in doc.versions
            ):
                raise ValueError("版本时间必须带时区")
            if doc.demo:
                raise ValueError("演示资料不进入真实知识库")
        if (
            sum(
                len(d.fileData or "") + sum(len(v.content) for v in d.versions)
                for d in self.documents
            )
            > 42000000
        ):
            raise ValueError("单次同步资料量过大")
        return self


class LibrarySync(StrictModel):
    revision: int = Field(ge=0)
    snapshot: LibrarySnapshot


class SearchInput(StrictModel):
    query: str = Field(min_length=1, max_length=4000)
    projectId: str | None = Field(default=None, max_length=100)
    limit: int = Field(default=6, ge=1, le=12)


class CitationInput(StrictModel):
    documentId: str = Field(max_length=160)
    versionId: str = Field(max_length=160)
    nodeId: str = Field(max_length=64)
    hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class AssistanceDecision(StrictModel):
    id: str = Field(min_length=1, max_length=260)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    allow: bool
