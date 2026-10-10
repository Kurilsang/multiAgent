"""任务审计日志：每任务一份 JSONL，全事件（含全部子 agent 轨迹）留痕。

落点 logs/tasks/<时间戳>-<任务id>.jsonl（logs/ 不入库）：失败 / 中断 /
叫停同样留痕，供回看与排障（不提供恢复语义，见 docs/specs/0004）。
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

    def __init__(self, task: str, directory: Path | None = None, now: datetime | None = None):
        self.task_id = new_task_id()
        stamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%S")
        self.directory = Path(directory) if directory is not None else DEFAULT_TASK_LOG_DIR
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / f"{stamp}-{self.task_id}.jsonl"
        header = {
            "type": HEADER_TYPE,
            "ts": (now or datetime.now()).isoformat(timespec="seconds"),
            "task_id": self.task_id,
            "task": task,
        }
        self._write(header)

    def record(self, event) -> None:
        """记录一个引擎事件（dataclass → JSON 行，含 agent 身份字段）。"""
        payload = {"type": type(event).__name__, **asdict(event)}
        self._write(payload)

    def _write(self, payload: dict) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def read_task_logs(directory: Path | None = None) -> list[dict]:
    """读回全部任务日志：[{"task_id", "task", "events": [...]}, ...]（时间序）。"""
    root = Path(directory) if directory is not None else DEFAULT_TASK_LOG_DIR
    if not root.is_dir():
        return []
    tasks: list[dict] = []
    # 时间序（同秒落盘时文件名前缀相同，按 mtime 兜底排序）
    paths = sorted(
        root.glob("*.jsonl"), key=lambda p: (p.stat().st_mtime_ns, p.name)
    )
    for path in paths:
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
                    "events": [],
                }
                tasks.append(current)
            elif current is not None:
                current["events"].append(payload)
    return tasks
