"""SessionState v2 transport (design items 4, 7). [daveey]

* ``GET /api/session-state/{session_id}`` -- the persisted object of that session's lineage (any id of it).
* ``GET /api/session-state?since=<rev>`` -- every object whose ``rev`` is above ``since`` plus the store's rev.
* ``POST /api/session-state/actions`` -- unblock / answer / archive / release; returns the post-transition object.
* ``WS /api/session-state/ws`` -- client sends ``{"type": "subscribe", "keys": [session ids] | ["*"],
  "since_rev": n?}``; server answers ``{"type": "snapshot", "rev", "objects"}`` (objects changed after
  ``since_rev``, else every current object of the keys) and then pushes ``{"type": "state", "object"}`` per
  publication of a subscribed lineage.

The GETs read only persisted rows: they never collect or reduce. A session never computed yet is 404 (the
subscription asks the store to compute it). Shadow mode: no consumer renders from these yet.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

router = APIRouter()
_log = logging.getLogger(__name__)
_CLOSE_NOT_ALLOWED = 4403
_CLOSE_UNAVAILABLE = 4503
_QUEUE_MAX = 1000


def _store():
    from hermes_session_state_store import current

    store = current()
    if store is None:
        raise HTTPException(status_code=503, detail="session state store is not running")
    return store


def _ws_allowed(ws: WebSocket) -> bool:
    from hermes_cli.web_server_chat import _ws_auth_ok, _ws_request_is_allowed

    return _ws_auth_ok(ws) and _ws_request_is_allowed(ws)


@router.get("/api/session-state")
def list_changes(since: int = 0) -> dict[str, Any]:
    store = _store()
    rev = store.current_rev()
    return {"rev": rev, "objects": store.changes_since(max(0, int(since)))}


@router.get("/api/session-state/{session_id}")
def get_state(session_id: str) -> dict[str, Any]:
    obj = _store().get(session_id)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"no session state for {session_id}")
    return obj


@router.post("/api/session-state/actions")
async def post_action(request: Request) -> Any:
    from hermes_session_state_actions import ActionRefused, apply_action

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    store = _store()
    try:
        return await asyncio.to_thread(apply_action, store, body)
    except ActionRefused as refusal:
        current: Optional[dict] = store.get(str(body.get("session_id") or "")) if body.get("session_id") else None
        return JSONResponse(status_code=refusal.status, content={
            "ok": False, "refusal": refusal.code, "message": refusal.message, "object": current})


def _resolve(store: Any, keys: list[str]) -> Optional[set[str]]:
    if "*" in keys:
        return None
    lineages = set()
    for key in keys:
        lineage = store.lineage_for(key)
        if lineage is None:
            store.emit_session(key, "subscribe")  # compute it; the push reaches this subscriber
            lineage = key
        lineages.add(lineage)
    return lineages


@router.websocket("/api/session-state/ws")
async def session_state_ws(ws: WebSocket) -> None:
    if not _ws_allowed(ws):
        await ws.close(code=_CLOSE_NOT_ALLOWED)
        return
    from hermes_session_state_store import current

    store = current()
    await ws.accept()
    if store is None:
        await ws.close(code=_CLOSE_UNAVAILABLE, reason="session state store is not running")
        return
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    wanted: dict[str, Optional[set[str]]] = {"lineages": set()}

    def push(obj: dict) -> None:
        lineages = wanted["lineages"]
        if lineages is None or obj.get("identity", {}).get("lineage") in lineages \
                or obj.get("identity", {}).get("tip") in lineages:
            loop.call_soon_threadsafe(_offer, queue, obj)

    cancel = store.subscribe(push)
    sender = asyncio.create_task(_send_loop(ws, queue))
    try:
        while True:
            message = await ws.receive_json()
            if not isinstance(message, dict) or message.get("type") != "subscribe":
                await ws.send_json({"type": "error", "message": "expected {type: subscribe, keys, since_rev?}"})
                continue
            keys = [str(k) for k in message.get("keys") or () if k]
            lineages = await asyncio.to_thread(_resolve, store, keys)
            wanted["lineages"] = lineages
            since = message.get("since_rev")
            await ws.send_json(await asyncio.to_thread(_snapshot, store, lineages, keys, since))
    except WebSocketDisconnect:
        pass
    finally:
        cancel()
        sender.cancel()


def _offer(queue: asyncio.Queue, obj: dict) -> None:
    try:
        queue.put_nowait(obj)
    except asyncio.QueueFull:
        _log.warning("session state websocket queue full; dropping a push (client catches up by since_rev)")


def _snapshot(store: Any, lineages: Optional[set[str]], keys: list[str], since: Any) -> dict[str, Any]:
    rev = store.current_rev()
    if isinstance(since, int) and not isinstance(since, bool) and since >= 0:
        objects = store.changes_since(since, lineages)
    elif lineages is None:
        objects = store.changes_since(0)
    else:
        objects = [obj for key in keys if (obj := store.get(key)) is not None]
    return {"type": "snapshot", "rev": rev, "objects": objects}


async def _send_loop(ws: WebSocket, queue: asyncio.Queue) -> None:
    while True:
        obj = await queue.get()
        try:
            await ws.send_json({"type": "state", "object": obj})
        except (WebSocketDisconnect, RuntimeError):
            return
