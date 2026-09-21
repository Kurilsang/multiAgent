"""工具注册表:function calling 通道的原子可调用工具。

工具是暴露给模型的原子能力（见 CONTEXT.md「工具」），走 function calling
通道；提示词注入型能力（技能）见 agent.py。注册表是唯一扩展点：
新增工具只需注册（名称、描述、参数 schema、实现），引擎与入口层不感知。
"""

import datetime
import re
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Tool:
    """单个工具：模型可见的名称/描述/参数 schema + 本地实现。"""

    name: str
    description: str
    parameters: dict  # JSON Schema 对象
    func: Callable[..., str]

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
