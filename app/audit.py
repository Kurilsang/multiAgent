"""任务审计日志：每任务一份 JSONL，全事件（含全部子 agent 轨迹）留痕。

落点 logs/tasks/<时间戳>-<任务id>.jsonl（logs/ 不入库）：失败 / 中断 /
叫停同样留痕，头部带会话归属。它是任务事件的**唯一权威副本**（可回看、
可重放渲染成轨迹），但不提供续跑语义（见 docs/specs/0005）。
导出（GET /export?traces=true）把日志渲染成协作树 markdown 附在主对话后。
"""

import json
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .config import PROJECT_ROOT

# 审计日志根目录（logs/ 不入库，见 .gitignore）
DEFAULT_TASK_LOG_DIR = PROJECT_ROOT / "logs" / "tasks"

HEADER_TYPE = "TaskAuditHeader"


def new_task_id() -> str:
    return uuid.uuid4().hex[:8]


class TaskAuditLog:
    """一次任务的审计日志写入器：首行任务头，随后逐行一个引擎事件。"""

    def __init__(
        self,
        task: str,
        directory: Path | None = None,
        now: datetime | None = None,
        session_id: str = "",
    ):
        self.task_id = new_task_id()
        stamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%S")
        self.directory = Path(directory) if directory is not None else DEFAULT_TASK_LOG_DIR
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / f"{stamp}-{self.task_id}.jsonl"
        self.session_id = session_id
        header = {
            "type": HEADER_TYPE,
            "ts": (now or datetime.now()).isoformat(timespec="seconds"),
            "task_id": self.task_id,
            "task": task,
            "session_id": session_id,
        }
        self._write(header)

    def record(self, event) -> None:
        """记录一个引擎事件（dataclass → JSON 行，含 agent 身份字段）。"""
        payload = {"type": type(event).__name__, **asdict(event)}
        self._write(payload)

    def _write(self, payload: dict) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _parse_log(path: Path) -> dict | None:
    """解析单份任务日志：首个 TaskAuditHeader 建条目，其后逐行收事件。

    容忍半行（崩溃时可能截断在行中）与缺头部的文件（返回 None）。
    """
    current: dict | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if payload.get("type") == HEADER_TYPE:
            current = {
                "task_id": payload.get("task_id", ""),
                "task": payload.get("task", ""),
                "session_id": payload.get("session_id", ""),
                "path": str(path),
                "events": [],
            }
        elif current is not None:
            current["events"].append(payload)
    return current


def read_task_logs(directory: Path | None = None) -> list[dict]:
    """读回全部任务日志：[{"task_id", "task", "session_id", "path", "events"}, ...]。

    顺序按 (mtime, 文件名)：同一秒内的多份日志在部分文件系统上 mtime 完全相同，
    此时退化为 uuid 随机序——需要权威顺序时请用任务索引（app/sessions.py 的
    tasks 表，按 started_at 排）。
    """
    root = Path(directory) if directory is not None else DEFAULT_TASK_LOG_DIR
    if not root.is_dir():
        return []
    tasks: list[dict] = []
    for path in sorted(root.glob("*.jsonl"), key=lambda p: (p.stat().st_mtime_ns, p.name)):
        entry = _parse_log(path)
        if entry is not None:
            tasks.append(entry)
    return tasks


def read_task_log(path: Path | str) -> dict | None:
    """按路径读回单份任务日志（任务索引指向的那一份）。"""
    target = Path(path)
    return _parse_log(target) if target.is_file() else None


def has_terminal(events: list[dict]) -> bool:
    """事件流里是否有**编排器**终态（没有 = 进程中断，轨迹只到一半）。

    子 agent 也会产出终态事件，所以必须限定 role=orchestrator——
    与 app/tasks.py 判定「编排器终态」的口径保持一致。
    """
    return any(
        event.get("type") in ("TaskFinished", "TaskFailed")
        and event.get("role", "orchestrator") == "orchestrator"
        for event in events
    )
