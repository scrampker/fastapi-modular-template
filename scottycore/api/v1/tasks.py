"""Task management REST API endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from scottycore.core.task_engine import Task, TaskStatus, engine

router = APIRouter(prefix="/tasks", tags=["tasks"])


# ── Response schemas ──────────────────────────────────────────────────────


class TaskResponse(BaseModel):
    id: str
    name: str
    task_type: str
    status: str
    progress: int
    status_text: str
    result: Any
    error: str
    output_lines: list[str]
    created_at: str
    started_at: str | None
    completed_at: str | None
    metadata: dict
    pending_question: dict | None


class TaskListResponse(BaseModel):
    tasks: list[TaskResponse]


class SummaryResponse(BaseModel):
    total: int
    running: int
    pending: int
    waiting: int
    completed: int
    failed: int
    cancelled: int


class RespondBody(BaseModel):
    answer: str


# ── Internal helpers ──────────────────────────────────────────────────────


def _to_response(task: Task) -> TaskResponse:
    return TaskResponse(
        id=task.id,
        name=task.name,
        task_type=task.task_type,
        status=task.status.value,
        progress=task.progress,
        status_text=task.status_text,
        result=task.result,
        error=task.error,
        output_lines=task.output_lines,
        created_at=task.created_at,
        started_at=task.started_at,
        completed_at=task.completed_at,
        metadata=task.metadata,
        pending_question=task.pending_question,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.get("/summary", response_model=SummaryResponse)
def task_summary() -> SummaryResponse:
    """Lightweight task counts by status — suitable for nav badges."""
    summary = engine.get_summary()
    return SummaryResponse(
        total=summary.get("total", 0),
        running=summary.get("running", 0),
        pending=summary.get("pending", 0),
        waiting=summary.get("waiting", 0),
        completed=summary.get("completed", 0),
        failed=summary.get("failed", 0),
        cancelled=summary.get("cancelled", 0),
    )


@router.get("", response_model=TaskListResponse)
def list_tasks(include_completed: bool = True) -> TaskListResponse:
    """List all tasks with status and progress."""
    tasks = engine.list_tasks(include_completed=include_completed)
    return TaskListResponse(tasks=[_to_response(t) for t in tasks])


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(task_id: str) -> TaskResponse:
    """Get a single task by ID, including full output."""
    task = engine.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return _to_response(task)


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str) -> dict[str, bool]:
    """Cancel a running or pending task."""
    task = engine.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.WAITING):
        raise HTTPException(status_code=400, detail="Task is not in a cancellable state")
    cancelled = engine.cancel(task_id)
    if not cancelled:
        raise HTTPException(status_code=400, detail="Task could not be cancelled")
    return {"ok": True}


@router.post("/{task_id}/respond")
def respond_to_task(task_id: str, body: RespondBody) -> dict[str, bool]:
    """Submit an answer to a task that is waiting for user input."""
    if not body.answer:
        raise HTTPException(status_code=400, detail="answer is required")
    task = engine.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != TaskStatus.WAITING:
        raise HTTPException(status_code=400, detail="Task is not waiting for input")
    delivered = engine.submit_answer(task_id, body.answer)
    if not delivered:
        raise HTTPException(status_code=400, detail="No pending question to answer")
    return {"ok": True}


@router.post("/clear-finished")
def clear_finished() -> dict[str, Any]:
    """Remove all completed, failed, and cancelled tasks."""
    terminal_statuses = {"completed", "failed", "cancelled"}
    to_remove = [
        tid
        for tid, t in engine._tasks.items()
        if t.status.value in terminal_statuses
    ]
    for tid in to_remove:
        engine._tasks.pop(tid, None)
        engine._running.pop(tid, None)
        engine._subscribers.pop(tid, None)
    return {"ok": True, "removed": len(to_remove)}
