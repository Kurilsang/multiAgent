"""skills.sh 源适配器（HTML 降级：官方 API 为 Vercel OIDC 专属）。

鉴权探针结论（2026-09-23 实测）：`GET https://skills.sh/api/v1/skills` 匿名
返回 **401 authentication_required（OIDC-only）**，本地爬取服务无法使用官方
API → 走页面内嵌数据提取：skills.sh 是 Next.js 站点，榜单以
`self.__next_f.push([1,"…"])` 载荷内嵌完整 JSON（source/skillId/name/
installs/weeklyInstalls/isOfficial），解析它比啃 DOM 稳。注意：
- 请求需浏览器 UA 且跟随重定向（裸 curl 返回 15 字节 "Redirecting..."）
- 三档榜单各取一页（/、/trending、/hot），单页已含数百条目
- 详情/目录包（SKILL.md 预览与安装）见 20 号工单（git 坐标拉取）
"""

from __future__ import annotations

import json
import re

from ..schema import CatalogEntry, CatalogError, SkillDetail, SkillPack

BASE_URL = "https://skills.sh"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) multiagent-catalog/1.0"
)
PAGE_TIMEOUT = 20.0
VIEWS = ("/", "/trending", "/hot")

# self.__next_f.push([1,"<JS 字符串转义的载荷>"])
_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', re.S)
_OBJECT_RE = re.compile(r'\{"source":')


def extract_embedded_payloads(html: str) -> list[str]:
    """收集 RSC 推送载荷并解 JS 字符串转义（转义坏块跳过不致命）。"""
    chunks: list[str] = []
    for match in _PUSH_RE.finditer(html):
        try:
            chunks.append(json.loads('"' + match.group(1) + '"'))
        except json.JSONDecodeError:
            continue
    return chunks


def extract_entry_objects(html: str) -> list[dict]:
    """从载荷里抠出技能条目对象（字段顺序无关，靠 json 解析不靠正则拼字段）。"""
    decoder = json.JSONDecoder()
    objects: list[dict] = []
    for chunk in extract_embedded_payloads(html):
        for match in _OBJECT_RE.finditer(chunk):
            try:
                obj, _ = decoder.raw_decode(chunk[match.start() :])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "skillId" in obj and "installs" in obj:
                objects.append(obj)
    return objects


def _to_entry(raw: dict) -> CatalogEntry:
    source_id = str(raw.get("source", ""))
    skill_id = str(raw.get("skillId", ""))
    tags = ("official",) if raw.get("isOfficial") else ()
    return CatalogEntry.from_raw(
        {
            "id": f"{source_id}/{skill_id}",
            "name": raw.get("name") or skill_id,
            "description": "",
            "origin": source_id,
            "installs": raw.get("installs"),
            "tags": tags,
            "detail_url": f"{BASE_URL}/{source_id}/{skill_id}",
            "install_ref": f"{source_id}/{skill_id}",
            "is_duplicate": bool(raw.get("isDuplicate")),
        },
        source="skills-sh",
    )


class SkillsShSource:
    """skills.sh 目录源（client 注入 HTTP 客户端，测试零网络）。"""

    name = "skills-sh"

    def __init__(self, settings=None, client=None, creds_store=None):
        # creds_store：接口统一占位（本源匿名 HTML 抓取，无凭证）
        self._client = client

    def crawl(self, max_pages: int = 3) -> list[CatalogEntry]:
        seen: dict[str, CatalogEntry] = {}
        for path in VIEWS[: max(1, max_pages)]:
            html = self._fetch_page(path)
            for raw in extract_entry_objects(html):
                entry = _to_entry(raw)
                seen.setdefault(entry.id, entry)  # 三档榜单重叠：保序去重
        if not seen:
            raise CatalogError(
                "skills.sh 页面解析为空（页面结构可能已改版），请检查适配器", 502
            )
        return list(seen.values())

    def detail(self, ref: str) -> SkillDetail:
        raise CatalogError("skills.sh 详情预览随目录包获取落地（20 号工单）", 501)

    def fetch_pack(self, ref: str) -> SkillPack:
        raise CatalogError("skills.sh 目录包获取随 20 号工单落地", 501)

    def _fetch_page(self, path: str) -> str:
        client = self._client if self._client is not None else _default_client()
        try:
            response = client.get(
                BASE_URL + path,
                headers={"User-Agent": USER_AGENT},
                timeout=PAGE_TIMEOUT,
            )
        except Exception as exc:
            raise CatalogError(f"skills.sh 请求失败: {exc}", 502) from exc
        if response.status_code != 200:
            raise CatalogError(
                f"skills.sh 返回错误（HTTP {response.status_code}）", 502
            )
        return response.text


def _default_client():
    import httpx

    return httpx.Client(follow_redirects=True)
