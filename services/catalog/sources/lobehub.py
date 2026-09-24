"""LobeHub Market 源适配器（注册制 M2M 鉴权 + 市场 API）。

端点考证（2026-09-23，自 @lobehub/market-sdk 0.40.1 源码反查，官方文档只写了
CLI 用法）：
- 列表/搜索: GET {base}/api/v1/skills?q=&page=&pageSize=&sort=&order=
  （响应 {currentPage, items[], pageSize, totalCount, totalPages}）
- 详情:     GET {base}/api/v1/skills/{identifier}
- 下载:     GET {base}/api/v1/skills/{identifier}/download   → ZIP 包
- 注册:     POST {base}/api/v1/clients/register（**限 5 次/30 分钟/IP，只注册一次**）
- 令牌:     POST {base}/oauth/token（grant_type=client_credentials + JWT 断言
  urn:ietf:params:oauth:client-assertion-type:jwt-bearer；HS256，iss=sub=clientId，
  aud=令牌端点，签名密钥 = clientSecret）

凭证（client_id/client_secret）由注册产生、只存本地凭证仓（SqliteCredsStore，
见 store.py），日志严禁落凭证。详情/目录包见本文件 detail / fetch_pack。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import time
import uuid
import zipfile
from pathlib import PurePosixPath
from urllib.parse import quote

from ..schema import (
    AuditBadge,
    CatalogEntry,
    CatalogError,
    SkillDetail,
    SkillPack,
    peek_skill_meta,
)

BASE_URL = "https://market.lobehub.com"
PAGE_TIMEOUT = 20.0
CLIENT_NAME = "multiagent-catalog"  # 注册身份（只注册一次，凭证复用）


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign_client_assertion(client_id: str, client_secret: str, token_endpoint: str) -> str:
    """构造 M2M JWT 断言（HS256，纯标准库实现，零新依赖）。"""
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": client_id,
        "sub": client_id,
        "aud": token_endpoint,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + 300,
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    signature = hmac.new(
        client_secret.encode(), signing_input.encode(), hashlib.sha256
    ).digest()
    return f"{signing_input}.{_b64url(signature)}"


def _to_entry(raw: dict) -> CatalogEntry:
    identifier = str(raw.get("identifier", ""))
    github = raw.get("github") if isinstance(raw.get("github"), dict) else {}
    audits = tuple(
        AuditBadge.from_raw(item) for item in (raw.get("audits") or ()) if isinstance(item, dict)
    )
    return CatalogEntry.from_raw(
        {
            "id": identifier,
            "name": raw.get("name") or identifier,
            "description": raw.get("description"),
            "origin": github.get("url") or identifier,
            "installs": raw.get("installCount"),
            "stars": github.get("stars"),
            "tags": raw.get("tags"),
            "detail_url": f"{BASE_URL}/skills/{identifier}",
            "install_ref": identifier,
            "audits": audits,
            "validated": bool(raw.get("isValidated")),
        },
        source="lobehub",
    )


class LobeHubSource:
    """LobeHub 市场源（client 注入 HTTP 客户端；creds_store 注入凭证仓）。"""

    name = "lobehub"

    def __init__(self, settings=None, client=None, creds_store=None):
        self._client = client
        self._creds = creds_store if creds_store is not None else {}
        self._token: str | None = None
        self._token_expiry: float = 0.0    # ---- 鉴权 ----

    def register(self, client_name: str = CLIENT_NAME) -> dict:
        """注册 M2M 客户端（限 5 次/30 分钟/IP）；返回凭证，由调用方存凭证仓。"""
        response = self._request(
            "POST", f"{BASE_URL}/api/v1/clients/register", json={"name": client_name, "source": "multiagent"}
        )
        data = self._json(response, "LobeHub 注册响应")
        creds = {
            "client_id": str(data.get("clientId") or data.get("client_id") or ""),
            "client_secret": str(data.get("clientSecret") or data.get("client_secret") or ""),
        }
        if not creds["client_id"] or not creds["client_secret"]:
            raise CatalogError("LobeHub 注册响应缺凭证字段（响应结构可能已变更）", 502)
        self._creds.update(creds)
        return creds

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry:
            return self._token
        client_id = str(self._creds.get("client_id") or "")
        client_secret = str(self._creds.get("client_secret") or "")
        if not client_id or not client_secret:
            raise CatalogError(
                "LobeHub 尚未注册（缺 M2M 凭证）：请先注册一次（注册限 5 次/30 分钟/IP）", 401
            )
        token_endpoint = f"{BASE_URL}/oauth/token"
        response = self._request(
            "POST",
            token_endpoint,
            data={
                "grant_type": "client_credentials",
                "client_assertion_type": (
                    "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
                ),
                "client_assertion": sign_client_assertion(
                    client_id, client_secret, token_endpoint
                ),
            },
        )
        data = self._json(response, "LobeHub 令牌响应")
        token = data.get("access_token")
        if not token:
            raise CatalogError("LobeHub 令牌响应缺 access_token", 502)
        self._token = str(token)
        self._token_expiry = time.time() + float(data.get("expires_in") or 3600) - 60
        return self._token

    # ---- 目录 ----

    def crawl(self, max_pages: int = 3) -> list[CatalogEntry]:
        entries: list[CatalogEntry] = []
        for page in range(1, max(1, max_pages) + 1):
            response = self._request(
                "GET",
                f"{BASE_URL}/api/v1/skills",
                params={
                    "page": page,
                    "pageSize": 50,
                    "sort": "installCount",
                    "order": "desc",
                },
                headers={"Authorization": f"Bearer {self._access_token()}"},
            )
            data = self._json(response, "LobeHub 目录响应")
            items = data.get("items") or []
            for raw in items:
                if isinstance(raw, dict):
                    entries.append(_to_entry(raw))
            if page >= int(data.get("totalPages") or 1):
                break
        return entries

    def detail(self, ref: str) -> SkillDetail:
        """确认卡预览：从 ZIP 包取 SKILL.md 全文（审计以列表条目的 isValidated 为准）。"""
        pack = self.fetch_pack(ref)
        skill_md = next(text for path, text in pack.files if path.endswith("SKILL.md"))
        meta = peek_skill_meta(skill_md)
        entry = CatalogEntry.from_raw(
            {
                "id": ref,
                "name": meta["name"] or ref,
                "description": meta["description"],
                "origin": pack.origin or ref,
                "detail_url": f"{BASE_URL}/skills/{ref}",
                "install_ref": ref,
            },
            source="lobehub",
        )
        return SkillDetail(entry=entry, skill_md=skill_md)

    def fetch_pack(self, ref: str) -> SkillPack:
        """目录包获取：下载 ZIP 并解包为 SKILL.md 文件集（防 zip-slip）。"""
        response = self._request(
            "GET",
            f"{BASE_URL}/api/v1/skills/{quote(ref, safe='')}/download",
            headers={"Authorization": f"Bearer {self._access_token()}"},
        )
        if response.status_code != 200:
            raise CatalogError(f"LobeHub 包下载失败（HTTP {response.status_code}）", 502)
        return _zip_to_pack(response.content, origin=ref)

    # ---- 传输 ----

    def _request(self, method: str, url: str, **kwargs):
        client = self._client if self._client is not None else _default_client()
        kwargs.setdefault("timeout", PAGE_TIMEOUT)
        try:
            return client.request(method, url, **kwargs) if hasattr(client, "request") else (
                client.post(url, **kwargs) if method == "POST" else client.get(url, **kwargs)
            )
        except Exception as exc:
            raise CatalogError(f"LobeHub 请求失败: {exc}", 502) from exc

    @staticmethod
    def _json(response, what: str) -> dict:
        if response.status_code != 200:
            raise CatalogError(f"{what}失败（HTTP {response.status_code}）", 502)
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise CatalogError(f"{what}不是有效 JSON", 502) from exc
        return data if isinstance(data, dict) else {}


def _default_client():
    from ..stdlib_http import UrllibHttpClient

    return UrllibHttpClient()


def _zip_to_pack(blob: bytes, origin: str) -> SkillPack:
    """ZIP → 目录包：zip-slip 防护（绝对路径与 .. 一律拒绝）+ 顶层目录归一。"""
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise CatalogError("LobeHub 包不是有效 ZIP（下载可能被截断）", 502) from exc
    entries: list[tuple[str, PurePosixPath]] = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        path = PurePosixPath(info.filename)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise CatalogError(f"LobeHub 包含非法路径（zip-slip 防护拒绝）：{info.filename}", 502)
        entries.append((info.filename, path))
    files: list[tuple[str, str]] = []
    extras: list[str] = []
    for filename, path in _strip_root(entries):
        if path.name == "SKILL.md":
            files.append(
                (str(path), archive.read(filename).decode("utf-8", errors="replace"))
            )
        else:
            extras.append(str(path))
    if not files:
        raise CatalogError("LobeHub 包内未找到 SKILL.md", 502)
    return SkillPack(
        files=tuple(files), extra_files=tuple(extras), origin=origin, source="lobehub"
    )


def _strip_root(entries: list[tuple[str, PurePosixPath]]) -> list[tuple[str, PurePosixPath]]:
    """所有文件同属单个顶层目录时剥掉它（包路径归一到技能根）。"""
    paths = [path for _filename, path in entries]
    if paths and all(len(path.parts) > 1 for path in paths):
        roots = {path.parts[0] for path in paths}
        if len(roots) == 1:
            return [
                (filename, PurePosixPath(*path.parts[1:]))
                for filename, path in entries
            ]
    return entries
