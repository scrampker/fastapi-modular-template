"""WebSocket endpoints for live task streaming."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from scottycore.core.task_engine import engine

router = APIRouter(tags=["websocket"])

# Cookie name used by the auth layer to store the session JWT.
# Matches the name set by the auth router when issuing cookie-based tokens.
_SESSION_COOKIE = "session"


@router.websocket("/ws/tasks/{task_id}")
async def task_stream(websocket: WebSocket, task_id: str) -> None:
    """Stream live updates for a task via WebSocket.

    Authenticates via the ``session`` cookie on the upgrade handshake.
    If the cookie is present but the token is invalid the connection is
    rejected with code 4001.  If no cookie is present the connection is
    allowed (caller-side auth guards should protect the UI route).

    Message types sent to the client:

    - ``{"type": "snapshot", "task": {...}}``   — sent immediately on connect
    - ``{"type": "output",   "line": "..."}``   — new output line
    - ``{"type": "progress", "value": 50, "status_text": "..."}``
    - ``{"type": "question", "question": "...", "options": [...]}``
    - ``{"type": "done",     "status": "completed", "result": ..., "error": ""}``
    - ``{"type": "error",    "message": "..."}`` — task not found
    - ``{"type": "ping"}``                       — 30-second keepalive
    """
    # ── Cookie-based auth ──────────────────────────────────────────────────
    token = websocket.cookies.get(_SESSION_COOKIE)
    if token:
        try:
            from scottycore.core.config import get_settings
            from jose import JWTError, jwt as _jwt

            settings = get_settings()
            _jwt.decode(
                token,
                settings.jwt_secret_key,
                algorithms=[settings.jwt_algorithm],
            )
        except Exception:
            await websocket.close(code=4001, reason="Invalid session token")
            return

    await websocket.accept()

    # ── Resolve task ───────────────────────────────────────────────────────
    task = engine.get_task(task_id)
    if not task:
        await websocket.send_json({"type": "error", "message": "Task not found"})
        await websocket.close()
        return

    # ── Send current state snapshot ────────────────────────────────────────
    await websocket.send_json({
        "type": "snapshot",
        "task": {
            "id": task.id,
            "name": task.name,
            "task_type": task.task_type,
            "status": task.status.value,
            "progress": task.progress,
            "status_text": task.status_text,
            "output_lines": task.output_lines,
            "error": task.error,
            "result": task.result,
            "pending_question": task.pending_question,
            "created_at": task.created_at,
            "started_at": task.started_at,
            "completed_at": task.completed_at,
            "metadata": task.metadata,
        },
    })

    # If the task already finished, close immediately after snapshot
    if task.status.value in ("completed", "failed", "cancelled"):
        await websocket.send_json({"type": "done", "status": task.status.value})
        await websocket.close()
        return

    # ── Subscribe and stream live events ───────────────────────────────────
    queue = engine.subscribe(task_id)
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                await websocket.send_json(event)
                if event.get("type") == "done":
                    break
            except asyncio.TimeoutError:
                # 30-second keepalive ping to prevent idle disconnects
                await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        pass
    finally:
        engine.unsubscribe(task_id, queue)
