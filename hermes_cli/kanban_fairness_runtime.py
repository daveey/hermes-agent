"""Opt-in per-process evidence consumers. Submit performs no filesystem/network IO.

Call activate outside scheduler/DB locks. Each process owns a distinct epoch directory.
An absent final seal (including shutdown timeout or writer failure) is incomplete evidence.
"""
from __future__ import annotations

import atexit
from contextlib import contextmanager
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
MAX_BATCH_RECORDS = 128
MAX_BATCH_BYTES = 1024 * 1024
_SESSIONS = {}
_DEFAULT_HOME = None


def close_all(timeout=2.0):
    """All profile/board consumers share one process shutdown allowance."""
    deadline = time.monotonic() + min(2.0, max(0.0, timeout))
    sessions = [session for key, session in tuple(_SESSIONS.items()) if key[0] == os.getpid()]
    for session in sessions:
        session.begin_close(timeout=max(0.0, deadline - time.monotonic()))
    for session in sessions:
        session.close(timeout=max(0.0, deadline - time.monotonic()))


atexit.register(close_all)


def append_records(*args, **kwargs):
    from hermes_cli.kanban_fairness_store import append_records as append

    return append(*args, **kwargs)


def seal_records(*args, **kwargs):
    from hermes_cli.kanban_fairness_store import seal_records as seal

    return seal(*args, **kwargs)


def _key(board, home=None):
    if home is None:
        from hermes_constants import get_hermes_home_override

        # Startup resolves the fallback once. Context and environment reads do no IO.
        home = get_hermes_home_override() or os.environ.get("HERMES_HOME") or _DEFAULT_HOME
    return (os.getpid(), str(home), board)


def enabled(*, board) -> bool:
    if not _SESSIONS:
        return False
    session = _SESSIONS.get(_key(board))
    return session is not None and not session.stopping.is_set()


def invalidate(*, board):
    """Fail closed after observation failure, without retrying the broken sink or IO."""
    session = _SESSIONS.get(_key(board)) if _SESSIONS else None
    if session is not None:
        session.abandoned.set()


@contextmanager
def observation(*, board):
    """Track actual reads across shutdown, without waiting on a busy capture lock."""
    session = _SESSIONS.get(_key(board)) if _SESSIONS else None
    admitted = False
    if session is not None:
        if session.lock.acquire(blocking=False):
            try:
                if not session.stopping.is_set():
                    session.readers += 1
                    admitted = True
            finally:
                session.lock.release()
        else:
            session.abandoned.set()
    try:
        yield
    finally:
        if admitted:
            if session.lock.acquire(blocking=False):
                try:
                    session.readers -= 1
                finally:
                    session.lock.release()
            else:
                session.abandoned.set()


def submit(*, board, kind, payload) -> bool:
    """Observe only: callers must not describe an uncommitted write as committed."""
    if not _SESSIONS:
        return False
    session = _SESSIONS.get(_key(board))
    return session.enqueue(kind, payload) if session is not None else False


class PayloadLimit(ValueError):
    """A bounded preflight rejected this input before serialization."""


def bounded_payload(payload):
    """Reject huge/deep/nonprimitive inputs before JSON allocates an unbounded copy."""
    budget = MAX_PAYLOAD_BYTES

    def copy(value, depth=0):
        nonlocal budget
        if depth > 32:
            raise PayloadLimit("payload nesting limit")
        budget -= 1
        if type(value) is str:
            budget -= len(value)
        if budget < 0:
            raise PayloadLimit("payload size limit")
        if type(value) in (list, dict):
            if len(value) > budget:
                raise PayloadLimit("payload item limit")
            if type(value) is dict:
                result = {}
                for key, child in value.items():
                    if type(key) is not str:
                        raise ValueError("payload keys must be strings")
                    result[copy(key, depth + 1)] = copy(child, depth + 1)
                return result
            return [copy(child, depth + 1) for child in value]
        if type(value) is int and value.bit_length() > 128:
            raise PayloadLimit("payload integer limit")
        if value is not None and type(value) not in (str, bool, int, float):
            raise ValueError("payload must contain JSON primitives")
        return value

    # Serialize only detached primitives, never the concurrently mutable caller data.
    raw = json.dumps(copy(payload), separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise PayloadLimit("encoded payload limit")
    return raw


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
        self.readers = 0
        self.producers = {}
        self.deadline_ns = None
        self.ready = threading.Event()
        self.decision = threading.Event()
        self.approved = False
        self.close_lock = threading.Lock()
        self.loss_lock = threading.Lock()
        # Validate supplied identities before starting any thread or making directories.
        self.record("loss", self.loss_payload("clock_gap", 1), 1)
        from hermes_cli.kanban_fairness_budget import initialize

        initialize(settings.directory, settings.max_bytes)
        self.out.mkdir(parents=True, exist_ok=False)
        from hermes_cli.kanban_fairness_tail import open_epoch

        open_epoch(self.out, self.identity)
        from agent.memory_provider import spawn_context_thread

        self.thread = spawn_context_thread(self.run, name="fairness-evidence", daemon=True)
        self.thread.start()

    @staticmethod
    def loss_payload(reason, count):
        return dict(reason=reason, count=count, first_seq=None, last_seq=None,
                    start_wall_ns=None, end_wall_ns=None)

    def record(self, kind, payload, seq, *, wall=None, mono=None):
        return make_record(**self.identity, seq=seq, kind=kind, payload=payload,
                           wall_ns=time.time_ns() if wall is None else wall,
                           mono_ns=time.monotonic_ns() if mono is None else mono)

    def note_loss(self, reason):
        # Contention never waits on the scheduler. Uncounted loss invalidates the tail.
        if not self.loss_lock.acquire(blocking=False):
            self.abandoned.set()
            return
        try:
            self.loss[reason] += 1
        finally:
            self.loss_lock.release()

    def enqueue(self, kind, payload):
        # Publish custody BEFORE checking stopping. A producer that passed the
        # check is visible until both queue admission and rejection accounting
        # finish. Individual dict operations are atomic; no IO or waiting here.
        token = object()
        self.producers[token] = True
        try:
            return self._enqueue(kind, payload)
        finally:
            self.producers.pop(token)

    def _enqueue(self, kind, payload):
        if self.stopping.is_set() or self.failed:
            return False
        if not self.lock.acquire(blocking=False):
            self.note_loss("dropped")
            return False
        try:
            if self.stopping.is_set():
                return False
            # The writer's final empty-queue check shares this admission lock.
            raw = bounded_payload(payload)
            self.record(kind, json.loads(raw), 1)
            if len(self.queue) >= MAX_RECORDS or self.bytes + len(raw) > MAX_QUEUE_BYTES:
                self.note_loss("dropped")
                return False
            self.queue.append((kind, raw, len(raw), time.time_ns(), time.monotonic_ns()))
            self.bytes += len(raw)
        except PayloadLimit:
            self.note_loss("oversize")
            return False
        except (TypeError, ValueError, OverflowError, RecursionError, RuntimeError):
            self.note_loss("unsupported")
            return False
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
                        batch, size = [], 0
                        while self.queue and len(batch) < MAX_BATCH_RECORDS:
                            if size + self.queue[0][2] > MAX_BATCH_BYTES:
                                break
                            item = self.queue.popleft()
                            batch.append(item)
                            size += item[2]
                        self.bytes -= size
                        done = not batch and self.stopping.is_set()
                        if done and (self.producers or self.readers):
                            self.abandoned.set()
                    if not batch:
                        break
                    records = [self.record(kind, json.loads(payload), self.seq + index,
                                           wall=wall, mono=mono)
                               for index, (kind, payload, _, wall, mono) in enumerate(batch, 1)]
                    append_records(self.out, records, max_bytes=self.maximum)
                    self.seq += len(records)
                if done:
                    break
            with self.loss_lock:
                losses = dict(self.loss)
            for reason, count in losses.items():
                self.write("loss", self.loss_payload(reason, count))
            if self.seq and not self.abandoned.is_set():
                manifest = seal_records(self.out, expected_sources=[dict(source_gateway=self.identity["source_gateway"],
                                        source_epoch=self.epoch, last_seq=self.seq)])
                from hermes_cli.kanban_fairness_tail import close_epoch

                self.ready.set()
                self.decision.wait()
                if self.approved:
                    close_epoch(self.out, self.identity, manifest, self.deadline_ns, self.completed_ns)
                    self.sealed = True
        except (OSError, ValueError, TypeError, RuntimeError):
            # Evidence failure cannot change scheduler behavior; unsealed epoch is a gap.
            self.failed = True
            self.note_loss("writer_error")
        finally:
            self.ready.set()

    def close(self, timeout=2.0):
        """Authorize a sealed data tail within deadline, NOT attest durable publication.

        True may precede receipt IO completion; only load() proves a durable tail.
        Once authorized, slow receipt publication cannot turn it into a rejection.
        """
        if not self.close_lock.acquire(blocking=False):
            return False
        try:
            return self._close(timeout)
        finally:
            self.close_lock.release()

    def begin_close(self, timeout=2.0):
        """Signal every writer before spending the shared allowance waiting."""
        timeout = min(2.0, max(0.0, timeout))
        if self.deadline_ns is None:
            self.deadline_ns = time.monotonic_ns() + int(timeout * 1_000_000_000)
        self.stopping.set()
        if self.producers:
            self.abandoned.set()
        if self.lock.acquire(blocking=False):
            try:
                if self.readers:
                    self.abandoned.set()
            finally:
                self.lock.release()
        else:
            self.abandoned.set()
        self.wake.set()

    def _close(self, timeout):
        if self.decision.is_set():
            return self.approved
        self.begin_close(timeout)
        assert self.deadline_ns is not None
        deadline = min(self.deadline_ns, time.monotonic_ns() + int(max(0.0, timeout) * 1_000_000_000))

        def remaining():
            return max(0.0, (deadline - time.monotonic_ns()) / 1_000_000_000)
        self.ready.wait(remaining())
        self.completed_ns = time.monotonic_ns()
        self.approved = (self.ready.is_set() and self.seq > 0 and not self.failed
                         and not self.abandoned.is_set() and self.completed_ns <= deadline)
        if not self.approved:
            self.abandoned.set()
        self.decision.set()
        self.thread.join(remaining())
        for key, session in tuple(_SESSIONS.items()):
            if session is self:
                del _SESSIONS[key]
        return self.approved


def activate(config, *, home: Path, board: str, source_gateway: str, board_uid: str,
             clock_error_ns=None):
    global _DEFAULT_HOME
    settings = resolve(config, home=home)
    if settings is None:
        return None
    if not os.environ.get("HERMES_HOME"):
        from hermes_constants import get_process_hermes_home

        _DEFAULT_HOME = str(get_process_hermes_home())
    key = _key(board, home)
    if key in _SESSIONS:
        raise ValueError("capture already active for this profile/board/process")
    session = Session(settings, board=board, source_gateway=source_gateway, board_uid=board_uid,
                      clock_error_ns=clock_error_ns)
    _SESSIONS[key] = session
    return session
