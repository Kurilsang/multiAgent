"""技能注册表：SKILL.md 技能包的加载、校验与自产。

技能是提示词形式注入上下文的能力包（见 CONTEXT.md「技能」「技能包」），
文件态是唯一事实源：`skills/<名称>/SKILL.md` = YAML frontmatter + Markdown 正文。
frontmatter 仅允许 name / description / tools 三个平面字段，超出一律拒绝——
技能包可能来自第三方安装，格式校验是信任边界的第一道闸。

启停状态另存 `skills/.installed.json`（不碰技能包本身）；增删改后热重载。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 避免与 tools.py 形成运行时环
    from .tools import ToolRegistry

# 技能名：中英文/数字/下划线/连字符，1-32 字符；须与目录名一致
NAME_PATTERN = re.compile(r"^[\w\u4e00-\u9fff-]{1,32}$")
MAX_DESCRIPTION_CHARS = 100
MAX_GUIDE_CHARS = 8000
PACK_FILENAME = "SKILL.md"
STATE_FILENAME = ".installed.json"
ALLOWED_FIELDS = ("name", "description", "tools")


class SkillError(Exception):
    """技能包非法或操作失败，携带面向用户的可读信息。"""


@dataclass(frozen=True)
class Skill:
    """技能：提示词级能力包，声明依赖的工具（见 CONTEXT.md「技能」）。"""

    name: str
    description: str
    guide: str
    tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillEntry:
    """注册表中的一项：技能本体 + 启停状态与来源。"""

    skill: Skill
    enabled: bool
    source: str


def parse_skill_md(text: str) -> tuple[dict, str]:
    """解析 SKILL.md 为 (frontmatter 字段, 正文)。

    frontmatter 手写解析（仅三个平面字段，不引入 YAML 依赖）；
    未知字段、游离列表项、格式非法一律拒绝。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillError("技能包必须以 --- frontmatter 开头")
    end = next(
        (i for i in range(1, len(lines)) if lines[i].strip() == "---"), None
    )
    if end is None:
        raise SkillError("技能包 frontmatter 未以 --- 结束")

    fields: dict = {"name": "", "description": "", "tools": []}
    current_list: str | None = None
    for raw in lines[1:end]:
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith("- "):
            if current_list != "tools":
                raise SkillError(f"frontmatter 出现游离列表项: {raw.strip()!r}")
            fields["tools"].append(line.lstrip()[2:].strip().strip("'\""))
            continue
        if ":" not in line:
            raise SkillError(f"frontmatter 行格式非法: {raw.strip()!r}")
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip().strip("'\"")
        if key not in ALLOWED_FIELDS:
            raise SkillError(
                f"frontmatter 含未知字段: {key!r}（仅允许 name / description / tools）"
            )
        if key == "tools":
            current_list = "tools"
            if value and value != "[]":
                fields["tools"] = [item.strip() for item in value.split(",") if item.strip()]
                current_list = None
        else:
            current_list = None
            fields[key] = value

    body = "\n".join(lines[end + 1 :]).strip("\n")
    return fields, body


def validate_pack(
    name: str,
    description: str,
    guide: str,
    tools: tuple[str, ...],
    known_tools,
    *,
    dir_name: str | None = None,
) -> None:
    """技能包字段校验；known_tools 为已注册工具名集合/可枚举对象。"""
    if not name:
        raise SkillError("frontmatter 缺少 name")
    if not NAME_PATTERN.fullmatch(name):
        raise SkillError(
            f"技能名非法: {name!r}（仅允许中英文/数字/下划线/连字符，1-32 字符）"
        )
    if dir_name is not None and dir_name != name:
        raise SkillError(f"目录名 {dir_name!r} 与技能名 {name!r} 不一致")
    if not description:
        raise SkillError(f"技能 {name} 的 frontmatter 缺少 description")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise SkillError(
            f"技能 {name} 的 description 超长（{len(description)} > {MAX_DESCRIPTION_CHARS} 字符）"
        )
    if not guide.strip():
        raise SkillError(f"技能 {name} 的正文（guide）为空")
    if len(guide) > MAX_GUIDE_CHARS:
        raise SkillError(
            f"技能 {name} 的正文超长（{len(guide)} > {MAX_GUIDE_CHARS} 字符）"
        )
    registered = set(known_tools)
    missing = [tool for tool in tools if tool not in registered]
    if missing:
        raise SkillError(f"技能 {name} 依赖未注册的工具: {'、'.join(missing)}")


def render_skill_md(skill: Skill) -> str:
    """把技能序列化回 SKILL.md 文本（与 parse_skill_md 互逆）。"""
    lines = ["---", f"name: {skill.name}", f"description: {skill.description}"]
    if skill.tools:
        lines.append("tools:")
        lines.extend(f"  - {tool}" for tool in skill.tools)
    else:
        lines.append("tools: []")
    lines.extend(["---", "", skill.guide])
    return "\n".join(lines) + "\n"


def resolve_skills_dir(directory: str, project_root: Path) -> Path:
    """技能目录解析：相对路径锚定项目根。"""
    path = Path(directory)
    return path if path.is_absolute() else project_root / path


def catalog_section(skills: SkillRegistry, limit: int) -> str:
    """动态技能清单（紧凑注入 system prompt）。

    每次调用现场生成，安装/卸载/启停即时反映；条数上限外的技能
    不进清单，提示模型用 search_skills 检索。
    """
    entries = skills.enabled_skills()
    if not entries:
        return ""
    shown = entries[:limit]
    hidden = len(entries) - len(shown)
    lines = [
        "## 技能清单",
        "以下技能可在需要时调用 use_skill(名称) 激活，激活后按其指引执行：",
    ]
    lines.extend(f"- {skill.name}：{skill.description}" for skill in shown)
    if hidden > 0:
        lines.append(f"（另有 {hidden} 个技能未列出，可用 search_skills(关键词) 检索）")
    return "\n".join(lines)


class SkillRegistry:
    """按名称持有技能包；文件态是唯一事实源，内存态随热重载刷新。"""

    def __init__(self, skills_dir: Path, tools: ToolRegistry):
        self._dir = Path(skills_dir)
        self._tools = tools
        self._skills: dict[str, SkillEntry] = {}
        self._state_file = self._dir / STATE_FILENAME

    @property
    def skills_dir(self) -> Path:
        return self._dir

    def pack_file(self, name: str) -> Path:
        return self._dir / name / PACK_FILENAME

    # ---- 读取 ----

    def entries(self) -> tuple[SkillEntry, ...]:
        return tuple(self._skills[name] for name in sorted(self._skills))

    def enabled_skills(self) -> tuple[Skill, ...]:
        return tuple(e.skill for e in self.entries() if e.enabled)

    def get(self, name: str) -> SkillEntry | None:
        return self._skills.get(name)

    def search(self, query: str, limit: int = 10) -> tuple[SkillEntry, ...]:
        needle = query.strip().casefold()
        if not needle:
            return self.entries()[:limit]
        hits = [
            e
            for e in self.entries()
            if needle in e.skill.name.casefold() or needle in e.skill.description.casefold()
        ]
        return tuple(hits[:limit])

    def match_prefix(self, message: str) -> SkillEntry | None:
        """/技能名 … 前缀显式点名：命中已注册且启用的技能，否则 None。"""
        if not message.startswith("/"):
            return None
        rest = message[1:].strip()
        if not rest:
            return None
        entry = self._skills.get(rest.split(maxsplit=1)[0])
        return entry if entry is not None and entry.enabled else None

    def reload(self) -> list[str]:
        """全量重扫技能目录；非法包跳过并以中文原因返回（不中断其余加载）。"""
        errors: list[str] = []
        entries: dict[str, SkillEntry] = {}
        state = self._load_state()
        if self._dir.is_dir():
            for pack_dir in sorted(p for p in self._dir.iterdir() if p.is_dir()):
                pack_file = pack_dir / PACK_FILENAME
                if not pack_file.is_file():
                    errors.append(f"{pack_dir.name}: 缺少 {PACK_FILENAME}，跳过")
                    continue
                try:
                    skill = self._load_pack(pack_file, dir_name=pack_dir.name)
                except SkillError as exc:
                    errors.append(str(exc))
                    continue
                record = state.get(skill.name, {})
                entries[skill.name] = SkillEntry(
                    skill=skill,
                    enabled=bool(record.get("enabled", True)),
                    source=str(record.get("source", "local")),
                )
        self._skills = entries
        return errors

    # ---- 变更 ----

    def create(
        self,
        name: str,
        description: str,
        guide: str,
        tools: tuple[str, ...] = (),
        source: str = "agent",
    ) -> Skill:
        """自产技能：校验通过即落盘生效；失败拒绝写入（调用方自愈重试）。"""
        skill = Skill(
            name=name.strip(),
            description=description.strip(),
            guide=guide,
            tools=tuple(tools),
        )
        validate_pack(
            skill.name,
            skill.description,
            skill.guide,
            skill.tools,
            self._tools.names(),
        )
        if skill.name in self._skills or self._dir.joinpath(skill.name).exists():
            raise SkillError(f"技能已存在: {skill.name}")
        self._write_pack(skill, source)
        state = self._load_state()
        state[skill.name] = {"enabled": True, "source": source}
        self._save_state(state)
        return skill

    def set_enabled(self, name: str, enabled: bool) -> SkillEntry:
        entry = self._require(name)
        state = self._load_state()
        state[entry.skill.name] = {"enabled": enabled, "source": entry.source}
        self._save_state(state)
        updated = SkillEntry(skill=entry.skill, enabled=enabled, source=entry.source)
        self._skills[entry.skill.name] = updated
        return updated

    def enable(self, name: str) -> SkillEntry:
        return self.set_enabled(name, True)

    def disable(self, name: str) -> SkillEntry:
        return self.set_enabled(name, False)

    def remove(self, name: str) -> None:
        entry = self._require(name)
        pack_dir = self._dir / entry.skill.name
        if pack_dir.is_dir():
            shutil.rmtree(pack_dir)
        state = self._load_state()
        state.pop(entry.skill.name, None)
        self._save_state(state)
        del self._skills[entry.skill.name]

    def install_packs(self, packs: list[dict], source: str) -> list[dict]:
        """批量落盘（安装入口）；逐包报告 installed / skipped / invalid。"""
        results: list[dict] = []
        state = self._load_state()
        for pack in packs:
            name = str(pack.get("name", ""))
            if "error" in pack:  # 扫描阶段解析失败，透传真实原因
                results.append({"name": name or "(未命名)", "status": "invalid", "detail": pack["error"]})
                continue
            try:
                skill = Skill(
                    name=name.strip(),
                    description=str(pack.get("description", "")).strip(),
                    guide=str(pack.get("guide", "")),
                    tools=tuple(pack.get("tools", ())),
                )
                validate_pack(
                    skill.name,
                    skill.description,
                    skill.guide,
                    skill.tools,
                    self._tools.names(),
                )
            except SkillError as exc:
                results.append({"name": name or "(未命名)", "status": "invalid", "detail": str(exc)})
                continue
            if skill.name in self._skills or self._dir.joinpath(skill.name).exists():
                results.append(
                    {"name": skill.name, "status": "skipped", "detail": "已存在同名技能"}
                )
                continue
            self._write_pack(skill, source)
            state[skill.name] = {"enabled": True, "source": source}
            results.append({"name": skill.name, "status": "installed", "detail": ""})
        self._save_state(state)
        return results

    # ---- 内部 ----

    def _require(self, name: str) -> SkillEntry:
        entry = self._skills.get(name)
        if entry is None:
            raise SkillError(f"未找到技能: {name}")
        return entry

    def _load_pack(self, pack_file: Path, *, dir_name: str | None = None) -> Skill:
        try:
            text = pack_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillError(f"{pack_file}: 读取失败（{exc}）") from exc
        fields, body = parse_skill_md(text)
        tools = tuple(str(item) for item in fields["tools"])
        validate_pack(
            fields["name"],
            fields["description"],
            body,
            tools,
            self._tools.names(),
            dir_name=dir_name,
        )
        return Skill(
            name=fields["name"],
            description=fields["description"],
            guide=body,
            tools=tools,
        )

    def _write_pack(self, skill: Skill, source: str) -> None:
        """落盘技能包并登记内存态；.installed.json 由调用方统一写。"""
        pack_dir = self._dir / skill.name
        pack_dir.mkdir(parents=True, exist_ok=True)
        self.pack_file(skill.name).write_text(render_skill_md(skill), encoding="utf-8")
        self._skills[skill.name] = SkillEntry(skill=skill, enabled=True, source=source)

    def _load_state(self) -> dict:
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self, state: dict) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
