"""单元测试：任务审计日志（JSONL 留痕）与任务轨迹导出渲染。

只断言落盘内容与导出文本的外部形态，不测内部实现。
"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app.agent import ActionObserved, ActionStarted, TaskFinished, TaskStarted, ThoughtDelta
from app.audit import TaskAuditLog, read_task_logs
from app.export import traces_to_markdown


class TaskAuditLogTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_records_header_and_events_as_jsonl(self):
        now = datetime(2026, 10, 10, 12, 0, 0)
        audit = TaskAuditLog("测试任务", directory=self.dir, now=now)
        audit.record(TaskStarted(task="测试任务"))
        audit.record(
            TaskFinished(status="completed", answer="答案", iterations=1)
        )
        lines = audit.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("TaskAuditHeader", lines[0])
        self.assertIn("测试任务", lines[0])
        self.assertIn("TaskFinished", lines[2])
        self.assertTrue(audit.path.name.startswith("20261010T120000-"))

    def test_record_carries_agent_identity_fields(self):
        audit = TaskAuditLog("任务", directory=self.dir)
        audit.record(
            ThoughtDelta(
                text="想",
                agent_id="sub-1",
                agent_name="子任务1",
                role="worker",
                parent_id="main",
            )
        )
        payload = read_task_logs(self.dir)[0]["events"][0]
        self.assertEqual(payload["agent_id"], "sub-1")
        self.assertEqual(payload["role"], "worker")
        self.assertEqual(payload["parent_id"], "main")

    def test_read_task_logs_roundtrip(self):
        # 注入不同时间戳：同一秒内的两份日志按 mtime 兜底排序时，Windows 上
        # mtime_ns 可能完全相同，退化成 uuid 随机序（已记录，待 #58 的任务索引
        # 提供权威顺序）。这里断言的是「按时间序读回」这一行为本身。
        TaskAuditLog("任务一", directory=self.dir, now=datetime(2026, 10, 10, 12, 0, 0))
        audit2 = TaskAuditLog(
            "任务二", directory=self.dir, now=datetime(2026, 10, 10, 12, 0, 1)
        )
        audit2.record(TaskStarted(task="任务二"))
        tasks = read_task_logs(self.dir)
        self.assertEqual([t["task"] for t in tasks], ["任务一", "任务二"])
        self.assertEqual(len(tasks[1]["events"]), 1)


class TracesExportTest(unittest.TestCase):
    def test_renders_collaboration_tree(self):
        events = [
            {
                "type": "TaskStarted",
                "task": "主任务",
                "role": "orchestrator",
                "agent_id": "main",
                "agent_name": "编排器",
                "parent_id": None,
            },
            {
                "type": "ThoughtDelta",
                "text": "我先",
                "role": "orchestrator",
                "agent_id": "main",
                "agent_name": "编排器",
                "parent_id": None,
            },
            {
                "type": "ThoughtDelta",
                "text": "想想",
                "role": "orchestrator",
                "agent_id": "main",
                "agent_name": "编排器",
                "parent_id": None,
            },
            {
                "type": "ActionStarted",
                "tool_name": "dispatch_task",
                "arguments": {},
                "role": "orchestrator",
                "agent_id": "main",
                "agent_name": "编排器",
                "parent_id": None,
            },
            {
                "type": "TaskFinished",
                "status": "completed",
                "answer": "子结果",
                "iterations": 1,
                "role": "worker",
                "agent_id": "sub-1",
                "agent_name": "子任务1",
                "parent_id": "main",
            },
        ]
        text = traces_to_markdown([{"task_id": "t1", "task": "主任务", "events": events}])
        self.assertIn("## 任务：主任务", text)
        self.assertIn("- 开始：主任务", text)
        self.assertIn("- 思考：我先想想", text)  # 连续思考增量合并
        self.assertIn("- 行动：dispatch_task({})", text)
        self.assertIn('  - [子任务1] 结果（completed）：子结果', text)  # 子 agent 缩进一层

    def test_empty_logs_render_no_tasks(self):
        text = traces_to_markdown([])
        self.assertIn("任务轨迹导出", text)
        self.assertNotIn("## 任务：", text)


if __name__ == "__main__":
    unittest.main()
