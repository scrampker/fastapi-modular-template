"""Task engine — async task lifecycle management."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    WAITING = "waiting"  # Waiting for user input


@dataclass
class Task:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = ""
    task_type: str = "generic"
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0  # 0-100
    status_text: str = ""
    result: Any = None
    error: str = ""
    output_lines: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: str | None = None
    completed_at: str | None = None
    metadata: dict = field(default_factory=dict)
    pending_question: dict | None = None  # {"question": str, "options": list[str] | None}


class TaskEngine:
    """Manages background tasks with status tracking and output streaming."""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._running: dict[str, asyncio.Task] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._answer_futures: dict[str, asyncio.Future] = {}

    # ── Public query API ───────────────────────────────────────────────────

    def list_tasks(self, include_completed: bool = True) -> list[Task]:
        """Return all tasks sorted newest-first."""
        tasks = list(self._tasks.values())
        if not include_completed:
            tasks = [t for t in tasks if t.status in (TaskStatus.PENDING, TaskStatus.RUNNING)]
        return sorted(tasks, key=lambda t: t.created_at, reverse=True)

    def get_task(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def get_summary(self) -> dict[str, int]:
        """Return task counts by status for nav badges."""
        counts: dict[str, int] = {
            "running": 0,
            "pending": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
            "waiting": 0,
        }
        for t in self._tasks.values():
            key = t.status.value
            counts[key] = counts.get(key, 0) + 1
        counts["total"] = sum(counts.values())
        return counts

    # ── Task lifecycle ─────────────────────────────────────────────────────

    def submit(
        self,
        name: str,
        coro_factory: Callable[[Task, "TaskEngine"], Coroutine],
        task_type: str = "generic",
        metadata: dict | None = None,
    ) -> Task:
        """Create and start a background task.

        ``coro_factory`` receives ``(task, engine)`` so it can call
        ``emit_output``, ``update_progress``, ``ask_question``, etc.
        """
        task = Task(name=name, task_type=task_type, metadata=metadata or {})
        self._tasks[task.id] = task

        async def _run() -> None:
            task.status = TaskStatus.RUNNING
            task.started_at = datetime.now(timezone.utc).isoformat()
            try:
                result = await coro_factory(task, self)
                task.result = result
                task.status = TaskStatus.COMPLETED
                task.progress = 100
            except asyncio.CancelledError:
                task.status = TaskStatus.CANCELLED
            except Exception as exc:
                task.error = str(exc)
                task.status = TaskStatus.FAILED
            finally:
                task.completed_at = datetime.now(timezone.utc).isoformat()
                await self._notify(task.id, {
                    "type": "done",
                    "status": task.status.value,
                    "result": task.result,
                    "error": task.error,
                })

        self._running[task.id] = asyncio.create_task(_run())
        return task

    def cancel(self, task_id: str) -> bool:
        """Cancel a running task. Returns True if cancellation was requested."""
        running = self._running.get(task_id)
        if running and not running.done():
            running.cancel()
            return True
        return False

    # ── In-task helpers (called from within task coroutines) ───────────────

    def emit_output(self, task_id: str, line: str) -> None:
        """Append an output line and push it to all subscribers."""
        task = self._tasks.get(task_id)
        if task:
            task.output_lines.append(line)
            asyncio.ensure_future(self._notify(task_id, {"type": "output", "line": line}))

    def update_progress(self, task_id: str, progress: int, status_text: str = "") -> None:
        """Update task progress (0-100) and optional status label."""
        task = self._tasks.get(task_id)
        if task:
            task.progress = min(100, max(0, progress))
            if status_text:
                task.status_text = status_text
            asyncio.ensure_future(self._notify(task_id, {
                "type": "progress",
                "value": task.progress,
                "status_text": task.status_text,
            }))

    async def ask_question(
        self,
        task_id: str,
        question: str,
        options: list[str] | None = None,
        timeout: float = 300.0,
    ) -> str:
        """Pause a task and wait for a user answer.

        Sets task status to WAITING and blocks until ``submit_answer`` is called
        or ``timeout`` seconds elapse (default 5 minutes).  Returns the answer
        string, or an empty string on timeout.
        """
        task = self._tasks.get(task_id)
        if not task:
            raise ValueError(f"Task {task_id!r} not found")

        task.status = TaskStatus.WAITING
        task.pending_question = {"question": question, "options": options}

        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._answer_futures[task_id] = future

        await self._notify(task_id, {
            "type": "question",
            "question": question,
            "options": options,
        })

        try:
            answer = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            answer = ""
        finally:
            self._answer_futures.pop(task_id, None)
            task.status = TaskStatus.RUNNING
            task.pending_question = None

        return answer

    def submit_answer(self, task_id: str, answer: str) -> bool:
        """Deliver an answer to a WAITING task.

        Returns True if the answer was accepted, False if no question was pending.
        """
        future = self._answer_futures.get(task_id)
        if not future or future.done():
            return False
        future.set_result(answer)
        return True

    # ── Subscription ───────────────────────────────────────────────────────

    def subscribe(self, task_id: str) -> asyncio.Queue:
        """Subscribe to live events for a task. Returns a Queue that receives dicts."""
        if task_id not in self._subscribers:
            self._subscribers[task_id] = []
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers[task_id].append(q)
        return q

    def unsubscribe(self, task_id: str, queue: asyncio.Queue) -> None:
        """Remove a subscriber queue."""
        if task_id in self._subscribers:
            self._subscribers[task_id] = [
                q for q in self._subscribers[task_id] if q is not queue
            ]

    # ── Maintenance ────────────────────────────────────────────────────────

    def cleanup_old(self, max_completed: int = 50) -> int:
        """Trim completed/failed/cancelled tasks, keeping the most recent N.

        Returns the number of tasks removed.
        """
        terminal = [
            t for t in self._tasks.values()
            if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
        ]
        terminal.sort(key=lambda t: t.completed_at or "", reverse=True)
        to_remove = terminal[max_completed:]
        for task in to_remove:
            self._tasks.pop(task.id, None)
            self._running.pop(task.id, None)
            self._subscribers.pop(task.id, None)
        return len(to_remove)

    # ── Internal ───────────────────────────────────────────────────────────

    async def _notify(self, task_id: str, event: dict) -> None:
        """Broadcast an event to all subscribers of a task."""
        for q in self._subscribers.get(task_id, []):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # Drop if subscriber is slow


# Module-level singleton — import this in routers and services
engine = TaskEngine()
