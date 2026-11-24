"""Bounded, local-only request-stage evidence. Never log URLs, queries or headers.

ASGI send completion means handed to the server transport, NOT received by a
client. Missing ingress proves nothing unless boot/sequence/drop coverage holds.
"""
from __future__ import annotations

import json
import logging.handlers
import os
import queue
import re
import threading
import time
import uuid
from pathlib import Path

from hermes_constants import get_hermes_home

POLL_PATHS = frozenset({
    "/api/plugins/user-steps/requests", "/api/plugins/hub-sync/status",
    "/api/plugins/kanban/board", "/api/sessions", "/api/health",
})
_ID = re.compile(r"^[a-f0-9]{32}$")
_SCOPE_KEY = "hermes.request_stages"


class StageJournal:
    """One worker owns rotating IO; producers only put_nowait fixed-size records.

    Each process has its own four 16MiB files. Sequence, cumulative drops and IO
    loss counters are repeated in every row and in idle one-second coverage rows.
    Retention/termination gaps are explicitly NOT evidence of absent requests.
    """

    def __init__(self, path: Path, capacity: int = 4096, max_bytes: int = 16 * 1024 * 1024):
        self.max_bytes = max_bytes
        self.path = path
        self.pending = queue.Queue(maxsize=capacity)
        self.boot = uuid.uuid4().hex
        self.sequence = 0
        self.dropped = 0
        self.io_lost = 0
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name="request-stages", daemon=True)
        self.thread.start()

    def emit(self, stage: str, **fields) -> None:
        self.sequence += 1
        row = {"stage": stage, "seq": self.sequence, "boot": self.boot,
               "pid": os.getpid(), "utc_ns": time.time_ns(), "mono_ns": time.monotonic_ns(),
               "dropped": self.dropped, "io_lost": self.io_lost, **fields}
        try:
            self.pending.put_nowait(row)
        except queue.Full:
            self.dropped += 1

    def _run(self) -> None:
        handler = None
        try:
            while not self.stopped.is_set() or not self.pending.empty():
                try:
                    row = self.pending.get(timeout=1)
                except queue.Empty:
                    self.emit("coverage")
                    continue
                try:
                    if handler is None:
                        self.path.parent.mkdir(parents=True, exist_ok=True)
                        handler = logging.handlers.RotatingFileHandler(
                            self.path, maxBytes=self.max_bytes, backupCount=3, delay=True,
                        )
                    # Use the handler's rotation, but not emit(), which swallows IO errors.
                    record = logging.LogRecord("stages", logging.INFO, "", 0,
                                               json.dumps(row, separators=(",", ":")), (), None)
                    if handler.shouldRollover(record):
                        handler.doRollover()
                    if handler.stream is None:
                        handler.stream = handler._open()
                    handler.stream.write(record.msg + "\n")
                    handler.flush()
                except OSError:
                    self.io_lost += 1
                    if handler is not None:
                        handler.close()
                    handler = None
                finally:
                    self.pending.task_done()
        finally:
            if handler is not None:
                handler.close()


_journal = None


def journal() -> StageJournal:
    global _journal
    if _journal is None:
        _journal = StageJournal(get_hermes_home() / "logs" / f"request-stages-{os.getpid()}.jsonl")
    return _journal


class RequestStageIngress:
    """Outermost ASGI observer: before auth/locks; send wait and encoded-body bytes."""

    def __init__(self, app, sink=None):
        self.app = app
        self.sink = sink

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or path not in POLL_PATHS:
            return await self.app(scope, receive, send)
        sink = self.sink or journal()
        ids = [v for k, v in scope.get("headers", ()) if k.lower() == b"x-hermes-req-id"]
        supplied = ids[0].decode("ascii", errors="replace") if len(ids) == 1 else ""
        correlated = bool(_ID.fullmatch(supplied))
        rid = supplied if correlated else uuid.uuid4().hex
        # The method is also untrusted input: constrain its cardinality.
        method = scope.get("method", "")
        method = method if method in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"} else "OTHER"
        fields = {"id": rid, "client_id": correlated, "path": path, "method": method}
        scope[_SCOPE_KEY] = (sink, fields)
        sink.emit("ingress", **fields)
        total = 0

        async def observed_send(message):
            nonlocal total
            kind = message["type"]
            if kind == "http.response.start":
                sink.emit("response_start", status=message["status"], **fields)
            elif kind == "http.response.body":
                total += len(message.get("body", b""))
                sink.emit("response_body", bytes=total, final=not message.get("more_body", False), **fields)
            await send(message)
            if kind == "http.response.start":
                sink.emit("response_start_sent", **fields)
            elif kind == "http.response.body" and not message.get("more_body", False):
                sink.emit("response_end", bytes=total, **fields)

        try:
            await self.app(scope, receive, observed_send)
        finally:
            sink.emit("app_exit", bytes=total, **fields)


class RequestStageHandler:
    """Innermost middleware: gates finished, immediately before router dispatch.

    This is router/handler dispatch, not a claim a sync endpoint acquired its
    threadpool slot. The remaining interval includes router and threadpool waits.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        state = scope.get(_SCOPE_KEY)
        if state is not None:
            sink, fields = state
            sink.emit("handler_dispatch", **fields)
        return await self.app(scope, receive, send)
