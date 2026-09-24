"""统一条目 schema 与清洗。

目录元数据是**不可信输入**（来自外网平台，可能含恶意文本）：
入库/出库一律经 clean_text 清洗（去控制字符、截断上限、纯文本化），
UI 渲染约定只走 textContent。缺关键字段的条目直接拒绝（CatalogError）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_NAME_CHARS = 120
MAX_DESCRIPTION_CHARS = 400
MAX_TAG_CHARS = 32
MAX_TAGS = 8
MAX_URL_CHARS = 500
MAX_AUDIT_SUMMARY_CHARS = 200

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
AUDIT_STATUSES = ("pass", "warn", "fail", "unknown")


class CatalogError(Exception):
    """目录数据非法或平台请求失败，携带面向用户的可读信息。

    status 用于 HTTP 映射（默认 400；找不到类用 404）。
    """

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def clean_text(value, limit: int) -> str:
    """去控制字符、压空白、截断上限——外网文本进内部世界的唯一通道。"""
    text = _CONTROL_CHARS.sub(" ", str(value or ""))
    text = " ".join(text.split())
    return text[:limit]


_FM_NAME_RE = re.compile(r"^name:\s*(.+)$", re.M)
_FM_DESC_RE = re.compile(r"^description:\s*(.+)$", re.M)


def peek_manifest_meta(manifest: str) -> dict:
    """从 manifest frontmatter 宽松取 name/description（详情预览用）。

    当前清单形态为 SKILL.md（frontmatter + 正文）；其他形态清单由适配器自理解。
    """
    text = manifest or ""
    head = ""
    if text.lstrip().startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            head = parts[1]
    name = _FM_NAME_RE.search(head)
    description = _FM_DESC_RE.search(head)
    return {
        "name": clean_text(name.group(1), MAX_NAME_CHARS) if name else "",
        "description": (
            clean_text(description.group(1), MAX_DESCRIPTION_CHARS) if description else ""
        ),
    }


def _clean_tags(raw) -> tuple[str, ...]:
    tags = []
    for item in list(raw or ())[:MAX_TAGS]:
        tag = clean_text(item, MAX_TAG_CHARS)
        if tag:
            tags.append(tag)
    return tuple(tags)


@dataclass(frozen=True)
class AuditBadge:
    """一条第三方安全审计结论（如 skills.sh 聚合的 Socket/Snyk 结果）。"""

    provider: str
    status: str  # pass | warn | fail | unknown
    summary: str = ""
    risk_level: str = ""

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "status": self.status,
            "summary": self.summary,
            "risk_level": self.risk_level,
        }

    @classmethod
    def from_raw(cls, raw: dict) -> "AuditBadge":
        status = clean_text(raw.get("status"), 16).lower()
        return cls(
            provider=clean_text(raw.get("provider"), 60),
            status=status if status in AUDIT_STATUSES else "unknown",
            summary=clean_text(raw.get("summary"), MAX_AUDIT_SUMMARY_CHARS),
            risk_level=clean_text(raw.get("risk_level"), 16).upper(),
        )


@dataclass(frozen=True)
class CatalogEntry:
    """统一目录条目：各平台适配器归一后的唯一形态。"""

    id: str
    name: str
    description: str
    source: str  # 平台标识：skills-sh | lobehub
    origin: str  # owner/repo 或市场标识
    kind: str = "skill"  # 资产类型：skill | mcp
    installs: int = 0
    stars: int = 0
    tags: tuple[str, ...] = field(default_factory=tuple)
    detail_url: str = ""
    install_ref: str = ""  # 目录包引用（fetch_pack/detail 的入参）
    audits: tuple[AuditBadge, ...] = field(default_factory=tuple)
    validated: bool = False
    is_duplicate: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "origin": self.origin,
            "kind": self.kind,
            "installs": self.installs,
            "stars": self.stars,
            "tags": list(self.tags),
            "detail_url": self.detail_url,
            "install_ref": self.install_ref,
            "audits": [badge.to_dict() for badge in self.audits],
            "validated": self.validated,
            "is_duplicate": self.is_duplicate,
        }

    @classmethod
    def from_raw(cls, raw: dict, *, source: str) -> "CatalogEntry":
        """从平台原始数据构造条目；缺关键字段即拒绝。"""
        entry_id = clean_text(raw.get("id"), 200)
        name = clean_text(raw.get("name"), MAX_NAME_CHARS)
        if not entry_id or not name:
            raise CatalogError(f"目录条目缺 id/name，已拒绝：{clean_text(raw, 80)!r}")
        return cls(
            id=entry_id,
            name=name,
            description=clean_text(raw.get("description"), MAX_DESCRIPTION_CHARS),
            source=source,
            origin=clean_text(raw.get("origin"), 200),
            kind=clean_text(raw.get("kind"), 16) or "skill",
            installs=_to_int(raw.get("installs")),
            stars=_to_int(raw.get("stars")),
            tags=_clean_tags(raw.get("tags")),
            detail_url=clean_text(raw.get("detail_url"), MAX_URL_CHARS),
            install_ref=clean_text(raw.get("install_ref"), 200),
            audits=tuple(
                AuditBadge.from_raw(item)
                for item in list(raw.get("audits") or ())[:10]
                if isinstance(item, dict)
            ),
            validated=bool(raw.get("validated")),
            is_duplicate=bool(raw.get("is_duplicate")),
        )


@dataclass(frozen=True)
class CatalogDetail:
    """确认卡预览载荷：条目信息 + manifest 全文 + 审计明细（通用 manifest 字段）。"""

    entry: CatalogEntry
    manifest_text: str
    manifest_path: str = ""
    audits: tuple[AuditBadge, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "entry": self.entry.to_dict(),
            "manifest_path": self.manifest_path,
            "manifest_text": self.manifest_text,
            "audits": [badge.to_dict() for badge in self.audits],
        }


@dataclass(frozen=True)
class CatalogPack:
    """目录包：安装所需的 manifest 文件集（平台分发差异在适配器内归一）。

    manifest_path 指定清单文件（当前形态 SKILL.md）；kind 为资产类型（skill | mcp）。
    """

    files: tuple[tuple[str, str], ...]  # (相对路径, 内容)；至少含 manifest_path 指定文件
    extra_files: tuple[str, ...] = ()  # 附带脚本/资源（主服务丢弃并警告）
    origin: str = ""
    source: str = ""
    manifest_path: str = ""
    kind: str = "skill"

    def to_dict(self) -> dict:
        return {
            "files": [{"path": path, "contents": contents} for path, contents in self.files],
            "extra_files": list(self.extra_files),
            "origin": self.origin,
            "source": self.source,
            "manifest_path": self.manifest_path,
            "kind": self.kind,
        }


def _to_int(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
