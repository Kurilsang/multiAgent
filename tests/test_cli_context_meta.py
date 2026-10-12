"""CLI 测试：回复后小字显示上下文账目（SPEC-0006 T1）。"""

import io
import unittest

from rich.console import Console

from app import cli
from app.config import Settings
from app.context_budget import estimate_messages
from app.llm import TextDelta


class FakeLLM:
    def __init__(self):
        self.calls: list[list[dict]] = []

    def chat_events(self, messages, provider=None, model=None, tools=None):
        self.calls.append([dict(m) for m in messages])
        yield TextDelta("答复")


class CliContextMetaTest(unittest.TestCase):
    def test_reply_shows_context_usage_line(self):
        settings = Settings(_env_file=None)
        fake = FakeLLM()
        conversation = cli.Conversation(system_prompt="sys", max_messages=4)
        buffer = io.StringIO()
        orig_console = cli.console
        cli.console = Console(file=buffer, width=120)
        try:
            cli.chat_once(
                fake,
                conversation,
                "上下文预算的账目",
                "glm",
                None,
                settings=settings,
            )
        finally:
            cli.console = orig_console
        output = buffer.getvalue()
        meta_tokens = estimate_messages(fake.calls[0])
        self.assertIn(f"约 {meta_tokens} token", output)
        self.assertIn("预算", output)


if __name__ == "__main__":
    unittest.main()
