"""WebUI 任务流重连逻辑（#59）的回归测试。

前端逻辑用 node 直接跑：从 app/static/index.html 抽出真实实现（不是副本），
打桩 fetch 与 SSE 响应，断言「断连后从缺口重连、不重复渲染」等行为。

无头环境跑不了真浏览器，但这段是纯逻辑 + fetch，node 能忠实验证；
先例：tests/test_catalog_http.py 用真实本地服务锚住传输层。
"""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "app" / "static" / "index.html"
NODE = shutil.which("node")

HARNESS = """
// ---- 最小环境桩：只提供被抽出的函数所需的东西 ----
let runningTask = null;
const timeline = {{
  steps: [],
  addStep(kind, text) {{ this.steps.push(kind + ': ' + text); }},
  handle(evt) {{ this.steps.push('frame:' + evt.type + ':' + (evt.seq ?? '')); }},
}};
function handleTaskFrame(evt) {{
  if (evt.type === 'task_meta') {{
    if (runningTask) runningTask.taskId = evt.task_id;
    return;
  }}
  if (evt.type === 'truncated') {{
    if (runningTask) {{
      runningTask.lastSeq = evt.from_seq || runningTask.lastSeq;
      runningTask.needsReconnect = true;
    }}
    return;
  }}
  if (typeof evt.seq === 'number') {{
    if (runningTask) {{
      if (evt.seq <= runningTask.lastSeq) return;
      runningTask.lastSeq = evt.seq;
    }}
  }}
  if (runningTask) runningTask.timeline.handle(evt);
}}
{read_sse}
{consume}
{drain}

function sseResponse(frames, {{ dropAfter = null }} = {{}}) {{
  const enc = new TextEncoder();
  let i = 0;
  return {{
    ok: true,
    body: {{
      getReader() {{
        return {{
          async read() {{
            if (dropAfter !== null && i >= dropAfter) throw new Error('socket closed');
            if (i >= frames.length) return {{ done: true, value: undefined }};
            return {{
              done: false,
              value: enc.encode('data: ' + JSON.stringify(frames[i++]) + '\\n\\n'),
            }};
          }},
        }};
      }},
    }},
  }};
}}

async function run() {{
  const results = [];

  // 1) 中途断开 -> 从缺口重连，不重复渲染
  {{
    runningTask = {{ taskId: null, lastSeq: 0, timeline }};
    timeline.steps.length = 0;
    const calls = [];
    const all = [
      {{ type: 'task_started', seq: 1 }},
      {{ type: 'thought_delta', text: 'a', seq: 2 }},
      {{ type: 'thought_delta', text: 'b', seq: 3 }},
      {{ type: 'final', status: 'completed', answer: 'x', iterations: 1, seq: 4 }},
    ];
    global.fetch = async (url) => {{
      calls.push(url);
      if (calls.length === 1) {{
        return sseResponse(
          [{{ type: 'task_meta', task_id: 'T1' }}, ...all],
          {{ dropAfter: 2 }},
        );
      }}
      const from = Number((url.match(/from_seq=(\\d+)/) || [])[1] || 0);
      return sseResponse([
        {{ type: 'task_meta', task_id: 'T1' }},
        ...all.filter((e) => e.seq > from),
      ]);
    }};
    const ok = await drainTaskStream({{ url: '/agent/stream', options: {{}} }}, timeline);
    const seqs = timeline.steps
      .filter((s) => s.startsWith('frame:'))
      .map((s) => s.split(':')[2]);
    results.push(['中途断开后从缺口重连且不重复', ok === true && calls.length === 2
      && calls[1].includes('from_seq=1')
      && JSON.stringify(seqs) === JSON.stringify(['1', '2', '3', '4'])]);
  }}

  // 2) 服务端报 truncated -> 按 from_seq 重连
  {{
    runningTask = {{ taskId: null, lastSeq: 0, timeline }};
    timeline.steps.length = 0;
    const calls = [];
    global.fetch = async (url) => {{
      calls.push(url);
      if (calls.length === 1) {{
        return sseResponse([
          {{ type: 'task_meta', task_id: 'T2' }},
          {{ type: 'task_started', seq: 1 }},
          {{ type: 'truncated', task_id: 'T2', from_seq: 1 }},
        ]);
      }}
      return sseResponse([
        {{ type: 'task_meta', task_id: 'T2' }},
        {{ type: 'final', status: 'completed', answer: 'y', iterations: 1, seq: 2 }},
      ]);
    }};
    const ok = await drainTaskStream({{ url: '/agent/stream', options: {{}} }}, timeline);
    results.push(['truncated 后按 from_seq 重连', ok === true && calls.length === 2
      && calls[1].includes('from_seq=1')]);
  }}

  // 3) 反复断开 -> 有限次后放弃并如实标注（不谎报完成）
  {{
    runningTask = {{ taskId: null, lastSeq: 0, timeline }};
    timeline.steps.length = 0;
    let calls = 0;
    global.fetch = async () => {{
      calls += 1;
      return sseResponse([
        {{ type: 'task_meta', task_id: 'T3' }},
        {{ type: 'task_started', seq: 1 }},
      ], {{ dropAfter: 2 }});
    }};
    const ok = await drainTaskStream({{ url: '/agent/stream', options: {{}} }}, timeline);
    const gaveUp = timeline.steps.some((s) => s.includes('放弃自动重连'));
    results.push(['反复断开后放弃并标注', ok === false && gaveUp && calls === 6]);
  }}

  // 4) 正常结束不重连
  {{
    runningTask = {{ taskId: null, lastSeq: 0, timeline }};
    timeline.steps.length = 0;
    let calls = 0;
    global.fetch = async () => {{
      calls += 1;
      return sseResponse([
        {{ type: 'task_meta', task_id: 'T4' }},
        {{ type: 'final', status: 'completed', answer: 'z', iterations: 1, seq: 1 }},
      ]);
    }};
    const ok = await drainTaskStream({{ url: '/agent/stream', options: {{}} }}, timeline);
    results.push(['正常结束不重连', ok === true && calls === 1]);
  }}

  let bad = 0;
  for (const [name, pass] of results) {{
    console.log((pass ? 'PASS ' : 'FAIL ') + name);
    if (!pass) bad++;
  }}
  process.exit(bad === 0 ? 0 : 1);
}}
run();
"""


def _extract(name: str) -> str:
    """从页面里抽出真实函数实现（含 async function* 生成器）。"""
    text = HTML.read_text(encoding="utf-8")
    match = re.search(rf"((?:async )?function\*? {name}\(.*?\n\}})", text, re.S)
    if not match:
        raise AssertionError(f"index.html 里找不到 {name}")
    return match.group(1)


@unittest.skipIf(NODE is None, "未安装 node，跳过前端逻辑测试")
class ReconnectLogicTest(unittest.TestCase):
    """断连续流的前端逻辑：真实实现 + 打桩 fetch，node 执行。"""

    def test_reconnect_behaviour(self):
        script = HARNESS.format(
            read_sse=_extract("readSse"),
            consume=_extract("consumeTaskStream"),
            drain=_extract("drainTaskStream"),
        )
        path = Path(self._tmp_dir()) / "reconnect.mjs"
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
            f"重连逻辑断言失败：\n{result.stdout}\n{result.stderr[-1200:]}",
        )

    def _tmp_dir(self) -> str:
        import tempfile

        if not hasattr(self, "_tmp"):
            self._tmp = tempfile.TemporaryDirectory()
            self.addCleanup(self._tmp.cleanup)
        return self._tmp.name


if __name__ == "__main__":
    unittest.main()
