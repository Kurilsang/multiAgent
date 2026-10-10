"""任务运行器：任务生命周期独立于 HTTP 连接（SPEC-0005 / issue #55）。

任务在后台线程执行，逐事件写入带单调 seq 的内存缓冲，并同时落审计 JSONL；
SSE 端点只是**订阅者**——客户端断连即退订，不置取消标志、不持有会话锁，
因此任务照常跑完，服务也不会被一次刷新拖垮。

单会话并发闸门：同一会话同时最多一个在跑任务；重复发起由调用方转成明确
拒绝（不排队、不阻塞）。停止按 task_id 定位，不再依赖「单会话单任务」前提。
"""

import queue
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator

from .agent import CancelToken, TaskFailed, TaskFinished

# 单个订阅者的收件箱上限：慢客户端 / 已断连客户端积压超限即退订，
# 凭 from_seq 重连续流（见 SPEC-0005 T5），避免无人消费的队列无限增长。
DEFAULT_SUBSCRIBER_QUEUE_MAX = 4096
# 已完成任务在内存里保留的条数（供重连续流回放历史事件）
DEFAULT_RETAINED_TASKS = 50
# 订阅者等待新事件的轮询间隔：只用于察觉退订/溢出，不影响事件时延
_SUBSCRIBER_POLL_SECONDS = 0.2

_DONE = object()


def _timestamp() -> str:
    """事件入缓冲的时间戳（秒级 ISO）：与 seq 一起构成可回放的时间线。"""
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def new_task_id() -> str:
    """任务标识：8 位十六进制随机串（与审计日志同风格）。"""
    return uuid.uuid4().hex[:8]


class TaskBusy(Exception):
    """同一会话已有在跑任务（调用方据此给出明确拒绝）。"""

    def __init__(self, task_id: str):
        super().__init__(
            f"当前会话已有任务在跑（{task_id}）：请先停止它或等它结束"
        )
        self.task_id = task_id


class SubscriberOverflow(Exception):
    """订阅者消费太慢（或已断连）导致积压超限：流被截断，需重连续流。

    带上 last_seq，让调用方能如实告知客户端「从哪个序号接着收」——
    静默结束会让人以为任务就这么点事件（见 SPEC-0005 T5）。
    """

    def __init__(self, last_seq: int, reason: str):
        super().__init__(reason)
        self.last_seq = last_seq
        self.reason = reason


class _Subscriber:
    """一个订阅者的收件箱；溢出即标记失效（停止接收，交由重连补发）。"""

    def __init__(self, maxsize: int):
        self.queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self.closed = threading.Event()
        self.reason = ""

    def push(self, item) -> bool:
        if self.closed.is_set():
            return False
        try:
            self.queue.put_nowait(item)
            return True
        except queue.Full:
            self.reason = "客户端消费过慢或已断连"
            self.closed.set()
            return False

    def close(self, reason: str = "") -> None:
        if reason:
            self.reason = reason
        self.closed.set()


class TaskRecord:
    """一次任务的运行时记录：事件缓冲、订阅者、取消令牌、终态。"""

    def __init__(
        self,
        task_id: str,
        session_id: str,
        task: str,
        subscriber_max: int = DEFAULT_SUBSCRIBER_QUEUE_MAX,
        provider: str = "",
        model: str = "",
    ):
        self.task_id = task_id
        self.session_id = session_id
        self.task = task
        # 该任务实际使用的厂商/模型：终态回写时一并落库（归属可追溯）
        self.provider = provider
        self.model = model
        self.status = "running"
        self.answer = ""
        self.reason = ""
        self.iterations = 0
        self.cancel = CancelToken()
        self._subscriber_max = subscriber_max
        self._lock = threading.Lock()
        self._events: list[tuple[int, object]] = []
        self._subscribers: set[_Subscriber] = set()
        self._seq = 0
        self._finished = False

    # ---- 生产者（任务线程） ----

    @property
    def finished(self) -> bool:
        with self._lock:
            return self._finished

    @property
    def event_count(self) -> int:
        with self._lock:
            return self._seq

    def publish(self, event) -> int:
        """登记一个引擎事件并推给全部订阅者，返回其单调序号。

        事件是 frozen dataclass（纯数据形状，见 agent.py），所以时间戳与
        seq 一起作为信封存在缓冲里，而不是塞进事件本身——审计日志仍是
        引擎事件的原始形状。
        """
        with self._lock:
            self._seq += 1
            seq = self._seq
            item = (seq, _timestamp(), event)
            self._events.append(item)
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.push(item)
        return seq

    def mark_terminal(
        self, status: str, answer: str = "", reason: str = "", iterations: int = 0
    ) -> None:
        """只记录终态字段（不关流）：让回写先于终态帧对订阅者可见。"""
        with self._lock:
            self.status = status
            self.answer = answer
            self.reason = reason
            self.iterations = iterations

    def finish(self) -> None:
        """标记结束并收掉全部订阅者（各自排空后自行结束）。"""
        with self._lock:
            self._finished = True
            subscribers = list(self._subscribers)
            self._subscribers.clear()
        for subscriber in subscribers:
            subscriber.push(_DONE)
            subscriber.close()

    # ---- 消费者（SSE 生成器） ----

    def subscribe(self, from_seq: int = 0) -> Iterator[tuple[int, str, object]]:
        """订阅事件流：先补发 seq > from_seq 的历史，再转实时。

        产出 (seq, ts, event)。断连时生成器被遗弃也不会泄漏：订阅者被移出
        集合后不再收事件，其收件箱有上限（溢出即抛 SubscriberOverflow，
        调用方据此告知客户端从 last_seq 重连，而不是静默截断事件流）。
        """
        subscriber = _Subscriber(self._subscriber_max)
        with self._lock:
            self._subscribers.add(subscriber)
            backlog = [item for item in self._events if item[0] > from_seq]
            finished = self._finished
        if finished:  # 已完成：补发历史即可，无需注册订阅者
            with self._lock:
                self._subscribers.discard(subscriber)
        last = from_seq
        try:
            for seq, ts, event in backlog:
                if seq <= last:
                    continue
                last = seq
                yield seq, ts, event
            if finished:
                return
            while True:
                try:
                    item = subscriber.queue.get(timeout=_SUBSCRIBER_POLL_SECONDS)
                except queue.Empty:
                    if subscriber.closed.is_set():
                        raise SubscriberOverflow(last, subscriber.reason or "订阅已关闭")
                    continue
                if item is _DONE:
                    return
                seq, ts, event = item
                if seq <= last:  # 与补发段重叠（订阅与快照之间的竞态）
                    continue
                last = seq
                yield seq, ts, event
        finally:
            with self._lock:
                self._subscribers.discard(subscriber)


class TaskHub:
    """任务登记处：后台执行、事件缓冲、按 task_id 停止。"""

    def __init__(
        self,
        retain: int = DEFAULT_RETAINED_TASKS,
        subscriber_max: int = DEFAULT_SUBSCRIBER_QUEUE_MAX,
    ):
        self._lock = threading.Lock()
        self._tasks: dict[str, TaskRecord] = {}
        self._finished_order: list[str] = []
        self._running: dict[str, str] = {}
        self._retain = retain
        self._subscriber_max = subscriber_max

    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            return self._tasks.get(task_id)

    def running_task_id(self, session_id: str) -> str | None:
        """该会话当前在跑的任务标识（None = 空闲）。"""
        with self._lock:
            return self._running.get(session_id)

    def running_tasks(self) -> list[TaskRecord]:
        with self._lock:
            return [
                self._tasks[task_id]
                for task_id in self._running.values()
                if task_id in self._tasks
            ]

    def start(
        self,
        *,
        session_id: str,
        task: str,
        runner: Callable[[CancelToken], Iterator],
        audit=None,
        on_terminal: Callable[[TaskRecord], None] | None = None,
        provider: str = "",
        model: str = "",
    ) -> TaskRecord:
        """登记并启动一次后台任务；同一会话已有在跑任务时抛 TaskBusy。"""
        with self._lock:
            busy = self._running.get(session_id)
            if busy is not None:
                raise TaskBusy(busy)
            task_id = new_task_id()
            record = TaskRecord(
                task_id,
                session_id,
                task,
                subscriber_max=self._subscriber_max,
                provider=provider,
                model=model,
            )
            self._tasks[task_id] = record
            self._running[session_id] = task_id
        threading.Thread(
            target=self._run,
            args=(record, runner, audit, on_terminal),
            daemon=True,
            name=f"task-{task_id}",
        ).start()
        return record

    def stop(self, task_id: str) -> bool:
        """按标识置取消标志；任务在迭代边界或厂商流内退出。"""
        record = self.get(task_id)
        if record is None or record.finished:
            return False
        record.cancel.cancel()
        return True

    def stop_all(self) -> None:
        """停机钩子：置全部在跑任务的取消标志（让它们尽快收尾）。"""
        for record in self.running_tasks():
            record.cancel.cancel()

    # ---- 任务线程 ----

    def _run(self, record, runner, audit, on_terminal) -> None:
        """执行任务：逐事件落审计 + 广播；回写完成才关闭订阅者。

        顺序刻意是「回写 → 关闭订阅者」：客户端看到流结束时，历史必定已经
        落库，因此刷新页面看到的内容与刚刚收到的终态一致（否则会出现
        「刚看到答案、刷新就没了」）。回炉场景有多个编排器终态，只回写最后一个。
        """
        terminal = None
        try:
            for event in runner(record.cancel):
                if audit is not None:
                    audit.record(event)
                if isinstance(event, (TaskFinished, TaskFailed)) and getattr(
                    event, "role", "orchestrator"
                ) == "orchestrator":
                    terminal = event
                record.publish(event)
        except Exception as exc:  # 引擎异常不该让任务无声消失
            terminal = TaskFailed(reason=f"任务执行异常：{exc}", iterations=0)
            if audit is not None:
                try:
                    audit.record(terminal)
                except Exception:
                    pass
            record.publish(terminal)
        finally:
            self._settle(record, terminal, on_terminal)
            record.finish()
            with self._lock:
                if self._running.get(record.session_id) == record.task_id:
                    del self._running[record.session_id]
                self._finished_order.append(record.task_id)
                while len(self._finished_order) > self._retain:
                    self._tasks.pop(self._finished_order.pop(0), None)

    @staticmethod
    def _settle(record, terminal, on_terminal) -> None:
        """记录终态字段并回写会话历史（无终态 = 如实标注中断）。"""
        if isinstance(terminal, TaskFinished):
            status, answer, reason, iterations = (
                terminal.status,
                terminal.answer,
                "",
                terminal.iterations,
            )
        elif isinstance(terminal, TaskFailed):
            status, answer, reason, iterations = (
                "failed",
                "",
                terminal.reason,
                terminal.iterations,
            )
        else:
            status, answer, reason, iterations = (
                "failed",
                "",
                "任务未产出终态（进程可能已中断）",
                0,
            )
        record.mark_terminal(status, answer, reason, iterations)
        if on_terminal is None:
            return
        try:
            on_terminal(record)
        except Exception as exc:  # 回写失败不影响任务终态
            print(f"[task] 终态回写失败：{exc}", file=sys.stderr)
