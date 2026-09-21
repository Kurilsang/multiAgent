"""交互式终端对话入口。

用法:
    python -m app.cli                    # 使用 .env 中的默认厂商
    python -m app.cli --provider glm     # 启动时指定厂商

会话内命令（大小写均可）:
    /model            查看可用厂商及当前目标
    /model <name>     切换厂商（如 /model deepseek）
    /reset            清空对话上下文
    /help             显示帮助
    /exit             退出
"""

import argparse
import sys

from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape

from .config import (
    PROVIDERS,
    ConfigError,
    ResolvedTarget,
    Settings,
    resolve_target,
)
from .conversation import Conversation
from .llm import LLMClient, LLMError

console = Console()

HELP_TEXT = """[bold]命令[/bold]
  /model            查看可用厂商及当前目标
  /model <name>     切换厂商（deepseek / glm / minimax）
  /reset            清空对话上下文
  /exit             退出"""


def describe_target(target: ResolvedTarget) -> str:
    return f"{target.provider} / {target.model}"


def print_providers(settings: Settings, current: str) -> None:
    for name, info in PROVIDERS.items():
        marker = "[bold green]→[/bold green] " if name == current else "  "
        configured = (
            "[green]✓[/green]" if settings.api_key_for(name) else "[red]✗ 缺少 Key[/red]"
        )
        console.print(f"{marker}[bold]{name}[/bold]  ({info.default_model})  {configured}")


def chat_once(
    llm: LLMClient,
    conversation: Conversation,
    user_input: str,
    provider: str,
    model: str | None,
) -> None:
    conversation.add("user", user_input)
    console.print("[bold magenta]assistant[/bold magenta] > ", end="")
    collected: list[str] = []
    try:
        for chunk in llm.chat_stream(conversation.messages_for_api(), provider, model):
            # 模型输出必须按纯文本打印：其中的 '[' 会被 rich 当作标记解析导致崩溃
            console.print(chunk, end="", markup=False, highlight=False)
            collected.append(chunk)
    except KeyboardInterrupt:
        conversation.pop_last()
        console.print()
        console.print("[yellow]已中断本次回复[/yellow]")
        return
    except (ConfigError, LLMError) as exc:
        conversation.pop_last()
        console.print()
        console.print(f"[red]出错了：{escape(str(exc))}[/red]")
        return
    conversation.add("assistant", "".join(collected))
    console.print()


def main() -> int:
    parser = argparse.ArgumentParser(description="多厂商 LLM 终端对话")
    parser.add_argument("--provider", help="启动时指定厂商（deepseek/glm/minimax）")
    parser.add_argument("--model", help="指定模型名（默认取厂商默认模型）")
    parser.add_argument("--system", help="system 提示词")
    args = parser.parse_args()

    try:
        settings = Settings()
        requested = (args.provider or "").strip().lower() or settings.llm_provider.strip().lower()
        target = resolve_target(settings, requested, args.model)
    except (ValidationError, ConfigError) as exc:
        console.print(f"[red]配置错误：{escape(str(exc))}[/red]")
        return 1

    # 运行时状态：current_model 为 None 表示使用厂商/配置默认模型
    current_provider = target.provider
    current_model = args.model

    conversation = Conversation(
        system_prompt=args.system, max_messages=settings.max_context_messages
    )
    llm = LLMClient(settings)

    console.print("[bold]multiagent 终端对话[/bold]")
    console.print(f"当前目标: [cyan]{escape(describe_target(target))}[/cyan]")
    console.print("输入 /help 查看命令，/exit 退出\n")

    while True:
        try:
            user_input = console.input("[bold cyan]你 >[/bold cyan] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n再见！")
            return 0

        if not user_input:
            continue

        parts = user_input.split()
        command = parts[0].lower()
        if command == "/exit":
            console.print("再见！")
            return 0
        if command == "/help":
            console.print(HELP_TEXT)
            continue
        if command == "/reset":
            conversation.reset()
            console.print("[yellow]对话上下文已清空[/yellow]")
            continue
        if command == "/model":
            if len(parts) == 1:
                print_providers(settings, current_provider)
                continue
            try:
                target = resolve_target(settings, parts[1], None)
            except ConfigError as exc:
                console.print(f"[red]{escape(str(exc))}[/red]")
                continue
            current_provider = target.provider
            current_model = None  # 切换厂商后回到该厂商默认模型
            console.print(f"[green]已切换到 {escape(describe_target(target))}[/green]")
            continue

        chat_once(llm, conversation, user_input, current_provider, current_model)


if __name__ == "__main__":
    sys.exit(main())
