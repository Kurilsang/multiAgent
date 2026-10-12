"""前端逻辑测试：上下文账目角标（SPEC-0006 T1，真实实现抽取 + node 执行）。"""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "app" / "static" / "index.html"
NODE = shutil.which("node")


HARNESS = """
{badge}

const results = [];

// 空 meta（旧服务端/任务通道）不渲染角标
results.push(['空 meta 不渲染', formatContextBadge(null) === ''
  && formatContextBadge(undefined) === ''
  && formatContextBadge({{}}) === '']);

// 正常 meta 渲染估算与预算
results.push([
  'meta 渲染估算与预算',
  formatContextBadge({{ estimated_tokens: 1234, budget_tokens: 27904, degraded: false }})
    === '≈1.2k / 27.9k token',
]);

// 降级时标注
results.push([
  '降级标注',
  formatContextBadge({{ estimated_tokens: 900, budget_tokens: 900, degraded: true }}).includes('已降级'),
]);

let bad = 0;
for (const [name, pass] of results) {{
  console.log((pass ? 'PASS ' : 'FAIL ') + name);
  if (!pass) bad++;
}}
process.exit(bad === 0 ? 0 : 1);
"""


def _extract(name: str) -> str:
    text = HTML.read_text(encoding="utf-8")
    match = re.search(rf"((?:async )?function\*? {name}\(.*?\n\}})", text, re.S)
    if not match:
        raise AssertionError(f"index.html 里找不到 {name}")
    return match.group(1)


@unittest.skipIf(NODE is None, "未安装 node，跳过前端逻辑测试")
class ContextBadgeTest(unittest.TestCase):
    def test_context_badge_formats_usage(self):
        script = HARNESS.format(badge=_extract("formatContextBadge"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "badge.mjs"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run(
                [NODE, str(path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
            )
        self.assertEqual(
            result.returncode,
            0,
            f"角标断言失败：\n{result.stdout}\n{result.stderr[-1200:]}",
        )


if __name__ == "__main__":
    unittest.main()
