"""Opt-in per-process evidence consumers. Submit performs no filesystem/network IO.

Call activate outside scheduler/DB locks. Each process owns a distinct epoch directory.
An absent final seal (including shutdown timeout or writer failure) is incomplete evidence.
"""
from __future__ import annotations

import atexit
from collections import Counter, deque
import json
import os
from pathlib import Path
import threading
import time
import uuid

from hermes_cli.kanban_fairness_config import resolve
from hermes_cli.kanban_fairness_schema import make_record


MAX_RECORDS = 1024
MAX_QUEUE_BYTES = 8 * 1024 * 1024
MAX_PAYLOAD_BYTES = 256 * 1024
_SESSIONS = {}


def append_records(*args, **kwargs):
    from hermes_cli.kanban_fairness_store import append_records as append

    return append(*args, **kwargs)


def seal_records(*args, **kwargs):
    from hermes_cli.kanban_fairness_store import seal_records as seal

    return seal(*args, **kwargs)


def _key(board, home=None):
    if home is None:
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
    return (os.getpid(), str(home), board)


def enabled(*, board) -> bool:
    if not _SESSIONS:
        return False
    session = _SESSIONS.get(_key(board))
    return session is not None and not session.stopping.is_set()


def submit(*, board, kind, payload) -> bool:
    """Observe only: callers must not describe an uncommitted write as committed."""
    if not _SESSIONS:
        return False
    session = _SESSIONS.get(_key(board))
    return session.enqueue(kind, payload) if session is not None else False


class Session:
    def __init__(self, settings, *, board, source_gateway, board_uid, clock_error_ns):
        self.epoch = uuid.uuid4().hex
        self.out = settings.directory / "runtime" / self.epoch
        self.maximum = settings.max_bytes
        self.identity = dict(capture_id=settings.capture_id, source_gateway=source_gateway,
                             board_uid=board_uid, source_epoch=self.epoch, clock_error_ns=clock_error_ns)
        self.board = board
        self.queue = deque()
        self.bytes = 0
        self.loss = Counter()
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.abandoned = threading.Event()
        self.failed = False
        self.sealed = False
        self.seq = 0
        # Validate supplied identities before starting any thread or making directories.
        self.record("loss", self.loss_payload("clock_gap", 1), 1)
        self.out.mkdir(parents=True, exist_ok=False)
        self.thread = threading.Thread(target=self.run, name="fairness-evidence", daemon=True)
        self.thread.start()

    @staticmethod
    def loss_payload(reason, count):
        return dict(reason=reason, count=count, first_seq=None, last_seq=None,
                    start_wall_ns=None, end_wall_ns=None)

    def record(self, kind, payload, seq, *, wall=None, mono=None):
        return make_record(**self.identity, seq=seq, kind=kind, payload=payload,
                           wall_ns=time.time_ns() if wall is None else wall,
                           mono_ns=time.monotonic_ns() if mono is None else mono)

    def enqueue(self, kind, payload):
        if self.stopping.is_set() or self.failed:
            return False
        try:
            # Snapshot caller-owned data; at most one bounded record is handed to writer.
            raw = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(raw) > MAX_PAYLOAD_BYTES:
                self.loss["oversize"] += 1
                return False
            payload = json.loads(raw)
            self.record(kind, payload, 1)
        except (TypeError, ValueError, OverflowError, RecursionError):
            self.loss["unsupported"] += 1
            return False
        if not self.lock.acquire(blocking=False):
            self.loss["dropped"] += 1
            return False
        try:
            if self.stopping.is_set():
                return False
            if len(self.queue) >= MAX_RECORDS or self.bytes + len(raw) > MAX_QUEUE_BYTES:
                self.loss["dropped"] += 1
                return False
            self.queue.append((kind, raw, len(raw), time.time_ns(), time.monotonic_ns()))
            self.bytes += len(raw)
        finally:
            self.lock.release()
        self.wake.set()
        return True

    def write(self, kind, payload, wall=None, mono=None):
        record = self.record(kind, payload, self.seq + 1, wall=wall, mono=mono)
        append_records(self.out, [record], max_bytes=self.maximum)
        self.seq += 1

    def run(self):
        try:
            while True:
                self.wake.wait()
                self.wake.clear()
                while True:
                    with self.lock:
                        item = self.queue.popleft() if self.queue else None
                        if item is not None:
                            self.bytes -= item[2]
                    if item is None:
                        break
                    kind, payload, _, wall, mono = item
                    self.write(kind, json.loads(payload), wall, mono)
                if self.stopping.is_set():
                    break
            for reason, count in self.loss.items():
                self.write("loss", self.loss_payload(reason, count))
            if self.seq and not self.abandoned.is_set():
                seal_records(self.out, expected_sources=[dict(source_gateway=self.identity["source_gateway"],
                             source_epoch=self.epoch, last_seq=self.seq)])
                self.sealed = True
        except Exception:
            # Evidence failure cannot change scheduler behavior; unsealed epoch is a gap.
            self.failed = True
            self.loss["writer_error"] += 1

    def close(self, timeout=2.0):
        self.stopping.set()
        self.wake.set()
        self.thread.join(min(2.0, max(0.0, timeout)))
        if self.thread.is_alive():
            self.abandoned.set()
        return self.sealed and not self.thread.is_alive()


def activate(config, *, home: Path, board: str, source_gateway: str, board_uid: str,
             clock_error_ns=None):
    settings = resolve(config, home=home)
    if settings is None:
        return None
    key = _key(board, home)
    if key in _SESSIONS:
        raise ValueError("capture already active for this profile/board/process")
    session = Session(settings, board=board, source_gateway=source_gateway, board_uid=board_uid,
                      clock_error_ns=clock_error_ns)
    _SESSIONS[key] = session
    atexit.register(session.close)
    return session
