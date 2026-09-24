"""工具注册表:function calling 通道的原子可调用工具。

工具是暴露给模型的原子能力（见 CONTEXT.md「工具」），走 function calling
通道；提示词注入型能力（技能，见 skills.py）经 use_skill 元工具激活，
元工具也注册在这里。注册表是唯一扩展点：新增工具只需注册
（名称、描述、参数 schema、实现），引擎与入口层不感知。
"""

import datetime
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 避免与 skills.py 形成运行时环
    from .skills import SkillRegistry


@dataclass(frozen=True)
class Tool:
    """单个工具：模型可见的名称/描述/参数 schema + 本地实现。

    full_result=True 的结果完整回灌不截断（如 use_skill 的技能正文——
    它是刻意加载的上下文，不是普通工具输出）。
    """

    name: str
    description: str
    parameters: dict  # JSON Schema 对象
    func: Callable[..., str]
    full_result: bool = False
    max_observation_chars: int | None = None  # 工具级观察值视图大小覆盖；None=全局默认，0=全量直灌

    def openai_schema(self) -> dict:
        """OpenAI function calling 的 tools 数组条目。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def run(self, arguments: dict) -> str:
        """执行工具；异常由调用方（引擎）转为错误观察值回灌模型。"""
        return str(self.func(**arguments))


class ToolRegistry:
    """按名称持有工具。finish 等协议工具由引擎自带，不进注册表。"""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"未注册的工具: {name!r}") from None

    def names(self) -> list[str]:
        return sorted(self._tools)

    def openai_schemas(self) -> list[dict]:
        return [tool.openai_schema() for tool in self._tools.values()]


# calculator 的白名单：仅数字与四则运算符，先于 eval 拦截任意代码
_CALC_ALLOWED = re.compile(r"^[0-9+\-*/().%\s]+$")


def calculator(expression: str) -> str:
    """四则运算求值；只接受数字与 + - * / ( ) . % 和空白。"""
    expr = expression.strip()
    if not expr:
        raise ValueError("表达式为空")
    if not _CALC_ALLOWED.fullmatch(expr):
        raise ValueError("表达式包含不允许的字符（仅支持数字与 + - * / ( ) % 运算）")
    try:
        value = eval(expr, {"__builtins__": {}}, {})  # noqa: S307 白名单已拦截注入
    except ZeroDivisionError as exc:
        raise ValueError("除数为零") from exc
    except Exception as exc:
        raise ValueError(f"表达式无法计算: {exc}") from exc
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def get_current_time() -> str:
    """当前本地日期时间与星期。"""
    now = datetime.datetime.now()
    weekdays = "星期一 星期二 星期三 星期四 星期五 星期六 星期日".split()
    return now.strftime("%Y-%m-%d %H:%M:%S") + " " + weekdays[now.weekday()]


SKILL_TOOL_NAMES = ("use_skill", "search_skills", "create_skill")


def skill_tools(skills: "SkillRegistry") -> list[Tool]:
    """技能元工具：use_skill / search_skills / create_skill。

    激活技能 = 一次普通工具调用（use_skill 的结果 = 技能正文，经观察值
    回灌进轨迹），引擎循环零改动。create_skill 校验失败即拒绝写入，
    错误作为观察值回灌，模型自愈重试。
    """

    def use_skill(name: str) -> str:
        entry = skills.get((name or "").strip())
        if entry is None:
            raise ValueError(f"未找到技能: {name!r}，可用 search_skills 检索")
        if not entry.enabled:
            raise ValueError(f"技能 {entry.skill.name} 已禁用，无法激活")
        skill = entry.skill
        deps = "、".join(skill.tools) or "无"
        return f"# 技能：{skill.name}\n{skill.guide}\n\n（建议使用的工具：{deps}）"

    def search_skills(query: str) -> str:
        hits = skills.search(query or "")
        if not hits:
            return f"没有匹配 {query!r} 的技能"
        return "\n".join(f"- {entry.skill.name}：{entry.skill.description}" for entry in hits)

    def create_skill(
        name: str, description: str, guide: str, tools: list | None = None
    ) -> str:
        skill = skills.create(
            name, description, guide, tuple(tools or ()), source="agent"
        )
        return f"技能 {skill.name} 已创建并生效，可直接 use_skill 激活"

    return [
        Tool(
            name="use_skill",
            description="激活技能：传入技能名，返回其使用指引（正文），随后按指引执行",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "技能名（见技能清单）"}
                },
                "required": ["name"],
            },
            func=use_skill,
            full_result=True,
        ),
        Tool(
            name="search_skills",
            description="按关键词检索技能清单（名称/描述模糊匹配），用于清单未列全时找技能",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词"}
                },
                "required": ["query"],
            },
            func=search_skills,
        ),
        Tool(
            name="create_skill",
            description="把可复用的流程沉淀为新技能（写入 SKILL.md 并立即生效）",
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "技能名（中英文/数字/下划线/连字符，1-32 字符）",
                    },
                    "description": {
                        "type": "string",
                        "description": "一句话描述（≤100 字符，进技能清单）",
                    },
                    "guide": {"type": "string", "description": "技能指引正文（≤8000 字符）"},
                    "tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "依赖的已注册工具名列表，可省略",
                    },
                },
                "required": ["name", "description", "guide"],
            },
            func=create_skill,
        ),
    ]


def default_registry() -> ToolRegistry:
    """占位最小工具集：纯函数、零网络/文件副作用，供测试与演示。"""
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="get_current_time",
            description="获取当前本地日期时间与星期",
            parameters={"type": "object", "properties": {}, "required": []},
            func=get_current_time,
        )
    )
    registry.register(
        Tool(
            name="calculator",
            description="四则运算计算器，输入算术表达式，如 (3+7)*2",
            parameters={
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "算术表达式"}
                },
                "required": ["expression"],
            },
            func=calculator,
        )
    )
    return registry
