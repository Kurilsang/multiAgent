"""交互式终端对话入口。

用法:
    python -m app.cli                    # 使用 .env 中的默认厂商
    python -m app.cli --provider glm     # 启动时指定厂商

会话内命令（大小写均可）:
    /model            查看可用厂商及当前目标
    /model <name>     切换厂商（如 /model deepseek）
    /agent <任务>     发起自主多步任务（思考→行动→观察→判断）
    /reset            清空对话上下文
    /help             显示帮助
    /exit             退出
"""

import argparse
import json
import sys

from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape

from .agent import (
    OBSERVATION_PREVIEW_CHARS,
    ActionObserved,
    ActionStarted,
    AgentEngine,
    TaskFailed,
    TaskFinished,
    ThoughtDelta,
)
from .config import (
    PROVIDERS,
    ConfigError,
    ResolvedTarget,
    Settings,
    resolve_target,
)
from .conversation import Conversation
from .llm import LLMClient, LLMError
from .tools import default_registry

console = Console()

HELP_TEXT = """[bold]命令[/bold]
  /model            查看可用厂商及当前目标
  /model <name>     切换厂商（deepseek / glm / minimax）
  /agent <任务>     发起自主多步任务（ReAct 循环）
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


def _observation_preview(text: str) -> str:
    if len(text) <= OBSERVATION_PREVIEW_CHARS:
        return text
    return text[: OBSERVATION_PREVIEW_CHARS - 1] + "…"


def run_agent_task(
    agent: AgentEngine,
    conversation: Conversation,
    task: str,
    provider: str,
    model: str | None,
) -> None:
    """执行一次任务并实时渲染事件流；完成后把「请求 + 答案」回写主对话。

    回写约定（见 CONTEXT.md「主对话」）：completed / partial 回写答案，
    FAILED 与中断不回写，主对话保持一问一答的连贯性。
    """
    console.print(f"[bold magenta]任务[/bold magenta] > {escape(task)}")
    in_thought = False
    answer: str | None = None
    try:
        for event in agent.run(task, provider, model):
            if isinstance(event, ThoughtDelta):
                if not in_thought:
                    console.print("[dim]思考[/dim] > ", end="")
                    in_thought = True
                # 模型输出按纯文本打印，避免 rich 把 '[' 当标记解析
                console.print(event.text, end="", markup=False, highlight=False)
            elif isinstance(event, ActionStarted):
                if in_thought:
                    console.print()
                    in_thought = False
                args_text = json.dumps(event.arguments, ensure_ascii=False)
                console.print(
                    f"[bold blue]行动[/bold blue] "
                    f"{escape(event.tool_name)}({escape(args_text)})"
                )
            elif isinstance(event, ActionObserved):
                if in_thought:
                    console.print()
                    in_thought = False
                style = "red" if event.is_error else "blue"
                console.print(
                    f"[{style}]观察[/{style}] "
                    f"{escape(_observation_preview(event.result))}"
                )
            elif isinstance(event, TaskFinished):
                if in_thought:
                    console.print()
                    in_thought = False
                label = "完成" if event.status == "completed" else "部分完成（预算耗尽）"
                console.print(
                    f"\n[bold green]assistant（{label}，{event.iterations} 轮）"
                    f"[/bold green] > {escape(event.answer)}"
                )
                answer = event.answer
            elif isinstance(event, TaskFailed):
                if in_thought:
                    console.print()
                    in_thought = False
                console.print(
                    f"\n[red]任务失败（第 {event.iterations} 轮）："
                    f"{escape(event.reason)}[/red]"
                )
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断任务（轨迹未回写主对话）[/yellow]")
        return
    if answer is not None:
        conversation.add("user", task)
        conversation.add("assistant", answer)


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
    agent = AgentEngine(
        llm,
        default_registry(),
        max_iterations=settings.agent_max_iterations,
    )

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
        if command == "/agent":
            task = user_input[len("/agent") :].strip()
            if not task:
                console.print("[yellow]用法: /agent <任务描述>[/yellow]")
                continue
            run_agent_task(agent, conversation, task, current_provider, current_model)
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
