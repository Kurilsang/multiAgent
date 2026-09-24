"""skills.sh 源适配器（HTML 降级：官方 API 为 Vercel OIDC 专属）。

鉴权探针结论（2026-09-23 实测）：`GET https://skills.sh/api/v1/skills` 匿名
返回 **401 authentication_required（OIDC-only）**，本地爬取服务无法使用官方
API → 走页面内嵌数据提取：skills.sh 是 Next.js 站点，榜单以
`self.__next_f.push([1,"…"])` 载荷内嵌完整 JSON（source/skillId/name/
installs/weeklyInstalls/isOfficial），解析它比啃 DOM 稳。注意：
- 请求需浏览器 UA 且跟随重定向（裸 curl 返回 15 字节 "Redirecting..."）
- 三档榜单各取一页（/、/trending、/hot），单页已含数百条目
- 详情/目录包：**git 坐标拉取**（install_ref = owner/repo/slug → 克隆 GitHub 仓库
  定位技能目录，SKILL.md 与其附带文件归一为目录包）；审计徽标解析自技能详情页
  的服务端渲染行（provider + Pass/Warn/Fail）
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from ..schema import (
    AuditBadge,
    CatalogEntry,
    CatalogError,
    SkillDetail,
    SkillPack,
    peek_skill_meta,
)

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
# 技能详情页的服务端渲染审计行：security/{slug}"…>Provider</span><span…>Status</span>
_AUDIT_RE = re.compile(
    r'href="[^"]*/security/(?P<slug>[^"/]+)"[^>]*>(?:(?!</a>).)*?'
    r'<span[^>]*>(?P<provider>[^<]+)</span><span[^>]*>(?P<status>[^<]+)</span>',
    re.S,
)
_CLONE_TIMEOUT = 60


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


def parse_audits(html: str) -> tuple[AuditBadge, ...]:
    """提取技能详情页的安全审计徽标（服务端渲染的 provider/status 行）。"""
    return tuple(
        AuditBadge.from_raw(
            {"provider": match.group("provider").strip(), "status": match.group("status").strip()}
        )
        for match in _AUDIT_RE.finditer(html)
    )


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

    def __init__(self, settings=None, client=None, creds_store=None, cloner=None):
        # creds_store：接口统一占位（本源匿名 HTML 抓取，无凭证）
        self._client = client
        self._cloner = cloner  # 测试注入点：默认 _git_clone

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
        """确认卡预览：SKILL.md 全文（git 拉取）+ 详情页审计徽标。"""
        pack = self.fetch_pack(ref)
        skill_md = next(text for path, text in pack.files if path.endswith("SKILL.md"))
        meta = peek_skill_meta(skill_md)
        entry = CatalogEntry.from_raw(
            {
                "id": ref,
                "name": meta["name"] or ref.rsplit("/", 1)[-1],
                "description": meta["description"],
                "origin": ref.split("/", 2)[0] + "/" + ref.split("/", 2)[1] if ref.count("/") == 2 else ref,
                "detail_url": f"{BASE_URL}/{ref}",
                "install_ref": ref,
            },
            source="skills-sh",
        )
        return SkillDetail(entry=entry, skill_md=skill_md, audits=parse_audits(self._fetch_page(f"/{ref}")))

    def fetch_pack(self, ref: str) -> SkillPack:
        """目录包获取：克隆 GitHub 仓库定位技能目录，归一 SKILL.md 文件集。

        well-known 来源（如 agent.qq.com/mail）无 git 坐标，暂不支持直装。
        """
        parts = ref.split("/")
        if len(parts) != 3 or not all(parts):
            raise CatalogError(
                f"skills.sh 仅支持 GitHub 来源技能直装（{ref!r} 为 well-known 来源，请手动安装）", 400
            )
        owner_repo, slug = "/".join(parts[:2]), parts[2]
        repo_url = f"https://github.com/{owner_repo}"
        with tempfile.TemporaryDirectory(prefix="skills-sh-pack-") as tmp:
            dest = Path(tmp) / "repo"
            try:
                (self._cloner or _git_clone)(repo_url, str(dest))
            except Exception as exc:
                raise CatalogError(f"仓库克隆失败: {exc}", 502) from exc
            pack_dir = _locate_pack_dir(dest, slug)
            skill_md = (pack_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")
            extras = tuple(
                str(item.relative_to(pack_dir)).replace("\\", "/")
                for item in sorted(pack_dir.rglob("*"))
                if item.is_file() and item.name != "SKILL.md"
            )
        return SkillPack(
            files=(("SKILL.md", skill_md),),
            extra_files=extras,
            origin=repo_url,
            source="skills-sh",
        )

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
    from ..stdlib_http import UrllibHttpClient

    return UrllibHttpClient()


def _git_clone(repo_url: str, dest: str) -> None:
    """浅克隆 GitHub 仓库（目录包获取的默认实现，60s 超时）。"""
    subprocess.run(
        ["git", "clone", "--depth", "1", repo_url, dest],
        check=True,
        capture_output=True,
        timeout=_CLONE_TIMEOUT,
    )


def _locate_pack_dir(repo_root: Path, slug: str) -> Path:
    """定位技能目录：常见布局先试，兜底扫描 SKILL.md 按目录名/技能名匹配。"""
    for candidate in (repo_root / slug, repo_root / "skills" / slug):
        if (candidate / "SKILL.md").is_file():
            return candidate
    for pack_file in sorted(repo_root.rglob("SKILL.md")):
        if pack_file.parent.name == slug:
            return pack_file.parent
        meta = peek_skill_meta(pack_file.read_text(encoding="utf-8", errors="replace"))
        if meta["name"] == slug:
            return pack_file.parent
    raise CatalogError(f"仓库中未找到技能 {slug!r} 的 SKILL.md", 404)
