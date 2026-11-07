"""Bounded, application-correlated timing diagnostics for the kanban sync loop. Off unless armed.

Arm with the installation-root config key ``kanban.sync.diagnostics.timing_until``: a finite Unix timestamp at most
:data:`MAX_ACTIVATION_S` ahead when the loop first sees it. Missing, null, non-numeric, non-finite, past or overlong
values (an overlong value never arms later either) and an unreadable config are off. The loop checks the key at its
safe boundary (just before it waits for work); one capture ends at the first such boundary after the earliest of
the configured time, 600 s of monotonic time, a changed/removed value or a config read failure (there is no hard
timer: the stop can overrun that time by up to one sync round plus one wait, at most about one poll period), or
at once on a write failure or the file cap; the same value never arms twice in a process. Off costs one
signature-cached config read per loop and creates no file or thread.

Known limits while a capture is armed: ``select`` holds the diagnostic lock across ``SyncWork.select`` (including
its per-board outbox reads), so a feed callback can wait that long before it returns to its socket; the wait shows
in ``chg.lk``, and loop/product latency is unaffected. A new capture rotates a file at least half the cap to
``.1``, replacing any older ``.1``: archive a capture you need before arming the next one.

What is recorded (monotonic ns; wall clock only for provenance/expiry), to ``<root>/logs/kanban-sync-timing.jsonl``:
- ``chg``: a decoded Board feed change frame, before ``SyncWork.changed`` (the connect-time catch-up is not one).
- ``sel``: the round's selection, taken under the same lock as the change hook, so every change is either claimed by
  this round (it was dirty when ``select`` consumed it) or stays pending for a later service, never both.
- ``loc``: the loop's first observation of a board's pending outbox (not the outbox write time).
- ``svc``: each board's actual service with the claimed earliest change/local watermark, outcome and error class.
  A failed or raised service is never a match: its claim returns to pending with a failure count.
- ``rep``: the metrics report's outer span with collect/limit/cpu/adv/send component spans (send covers the whole
  private bounded transport, join and timeout included), the result and the sampled window/end only.
- ``wait`` and ``flush`` spans (flush cost is its own record, written by the next flush); ``pend`` and ``stop``.

Records are allowlisted numbers, booleans and enums (no names, IDs, URLs, keys, messages or paths): boards are
process-local ordinals. Every record has a sequence number; a full queue, a schema violation, a diagnostic
exception, a write failure, the file cap or a missing stop marker marks the capture invalid rather than green.
Nothing here can change sync: product callables run exactly once, outside the diagnostic ``try``.
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional

CONFIG_KEY = ("kanban", "sync", "diagnostics", "timing_until")
LOG_NAME = "kanban-sync-timing.jsonl"
SCHEMA = 1
MAX_ACTIVATION_S = 600.0
QUEUE_MAX = 50_000
FILE_MAX_BYTES = 32 * 1024 * 1024
STOP_RESERVE_BYTES = 8192  # always left for the pend + stop records
PEND_MAX = 32
COMPLETE_REASONS = frozenset({"expired", "bound", "disabled"})
SPANS = ("collect", "limit", "cpu", "adv", "send")
_HEX40 = re.compile(r"[0-9a-f]{40}")
_ERROR = object()

OUTCOMES = frozenset({"ok", "timeout", "http", "os", "other"})
ENUMS: dict[str, frozenset] = {
    "ev": frozenset({"start", "chg", "sel", "loc", "svc", "rep", "wait", "flush", "pend", "stop"}),
    "kind": frozenset({"changes", "changed", "resync", "other"}),
    "out": frozenset({"ok", "error", "raised"}),
    "err": frozenset({"SchemaAhead", "HubError", "Resync", "SyncUnavailable", "SyncRoundBusy", "OSError", "URLError",
                      "TimeoutError", "BoardBudgetExhausted", "ConnectionError", "ConnectionResetError",
                      "ConnectionRefusedError", "OperationalError", "DatabaseError", "IntegrityError", "Error",
                      "other"}),
    "mode": frozenset({"primary", "replica", "off", "?", "other"}),
    "res": frozenset({"sent", "not_sent", "none"}),
    "why": frozenset({"remote", "local", "period", "stopped", "other"}),
    "reason": frozenset({"expired", "bound", "disabled", "config_error", "write_error", "file_cap", "loop_exit"}),
    "code_src": frozenset({"boot", "none"}),
    **{f"{s}_out": OUTCOMES for s in SPANS},
}
_INTS = ("seq run pid t t0 t1 wall_ms until_ms deadline_ns schema queue_max file_max b n nb claims local c_first "
         "c_last c_n c_t l_t fails lk cpu_win_ms cpu_end_ms ovh_ns bytes lost errors flushes flush_ns pend_more")
FIELDS: dict[str, Any] = {
    **{k: int for k in _INTS.split()},
    **{f"{s}_{p}": int for s in SPANS for p in ("t0", "t1", "st")},
    "loop": bool, "valid": bool, "disk_moved": bool, "code": "hex40",
    **ENUMS,
}


def log_path(root: Optional[Path] = None) -> Path:
    if root is None:
        from hermes_constants import get_default_hermes_root
        root = Path(get_default_hermes_root())
    return Path(root) / "logs" / LOG_NAME


def _valid_until(value: Any) -> Optional[float]:
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def _enum(value: Any, field: str) -> str:
    return value if value in ENUMS[field] else "other"


def _err_class(error: Any) -> str:
    """``RoundResult.error`` is ``"<Type>: <message>"``: keep only an allowlisted type name."""
    return _enum(error.split(":", 1)[0].strip(), "err") if isinstance(error, str) else "other"


def _outcome(exc: Optional[BaseException]) -> tuple[str, Optional[int]]:
    if exc is None:
        return "ok", None
    if isinstance(exc, TimeoutError):
        return "timeout", None
    status = getattr(exc, "status", None)
    if type(status) is int:
        return "http", status
    return ("os" if isinstance(exc, OSError) else "other"), None


def _code_identity() -> tuple[Optional[str], str, Optional[bool]]:
    """The revision this process booted on (recorded by ``hermes_bootstrap``), never the mutable checkout HEAD;
    ``disk_moved`` says whether the checkout has moved since."""
    from hermes_cli.git_fingerprint import process_boot_fingerprint, read_git_revision_fingerprint
    from hermes_cli._startup_fast import project_root_str
    root = Path(project_root_str())
    boot = process_boot_fingerprint(root)
    sha = boot.rsplit(":", 1)[-1] if boot else ""
    if not _HEX40.fullmatch(sha):
        return None, "none", None
    disk = read_git_revision_fingerprint(root)
    return sha, "boot", (disk != boot) if disk else None


class _Mark:
    """The earliest unserviced change/local watermark of one board."""
    __slots__ = ("fails", "first", "l_t", "last", "n", "t")

    def __init__(self, first=None, t=None, l_t=None) -> None:
        self.first, self.last, self.n, self.t, self.l_t, self.fails = first, first, 0 if first is None else 1, t, l_t, 0

    def fields(self) -> dict:
        out = {"fails": self.fails}
        if self.first is not None:
            out.update(c_first=self.first, c_last=self.last, c_n=self.n, c_t=self.t)
        if self.l_t is not None:
            out["l_t"] = self.l_t
        return out


def _least(a, b):
    return b if a is None else a if b is None else min(a, b)


def _merge(a: Optional[_Mark], b: Optional[_Mark]) -> Optional[_Mark]:
    if a is None or b is None:
        return a if b is None else b
    out = _Mark(_least(a.first, b.first), _least(a.t, b.t), _least(a.l_t, b.l_t))
    out.last = max((x for x in (a.last, b.last) if x is not None), default=None)
    out.n, out.fails = a.n + b.n, max(a.fails, b.fails)
    return out


class _Capture:
    def __init__(self, until: float, deadline_ns: int, fd: int, run: int) -> None:
        self.until, self.deadline_ns, self.fd, self.run = until, deadline_ns, fd, run
        self.seq = self.lost = self.errors = self.bytes = self.flushes = self.flush_ns = self.ovh_ns = 0
        self.queue: deque = deque()
        self.pending: dict[int, _Mark] = {}
        self.claims: dict[int, _Mark] = {}
        self.write_failed = self.capped = False


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


_NULL = _Null()


class _Span:
    def __init__(self, diag: TimingDiag, marks: dict, name: str) -> None:
        self.diag, self.marks, self.name = diag, marks, name

    def __enter__(self):
        self.t0 = self.diag.clock_ns()
        return self

    def __exit__(self, _type, exc, _tb) -> bool:
        t1 = self.diag.clock_ns()
        try:
            out, status = _outcome(exc)
            self.marks.update({f"{self.name}_t0": self.t0, f"{self.name}_t1": t1, f"{self.name}_out": out})
            if status is not None:
                self.marks[f"{self.name}_st"] = status
            self.marks["ovh_ns"] = self.marks.get("ovh_ns", 0) + self.diag.clock_ns() - t1
        except Exception:  # health: allow BLE001 -- diagnostics must never change the reporter's outcome
            self.diag._broken()
        return False


class TimingDiag:
    """Process diagnostic state. One instance (:data:`_DIAG`) serves the sync loop; tests build their own."""

    def __init__(self, *, root: Optional[Path] = None, read_doc: Optional[Callable[[Path], Any]] = None,
                 clock_ns: Callable[[], int] = time.monotonic_ns, wall: Callable[[], float] = time.time,
                 code_identity: Optional[Callable[[], tuple]] = None, queue_max: int = QUEUE_MAX,
                 file_max: int = FILE_MAX_BYTES, write: Callable[[int, bytes], int] = os.write) -> None:
        self.root, self.read_doc, self.clock_ns, self.wall = root, read_doc, clock_ns, wall
        self.code_identity, self.queue_max, self.file_max, self.write = code_identity, queue_max, file_max, write
        self.lock = threading.Lock()
        self.capture: Optional[_Capture] = None
        self._consumed: deque = deque(maxlen=64)
        self._ords: dict[str, int] = {}
        self._loop: Optional[int] = None
        self._marks: Optional[dict] = None

    # --- config and lifecycle (sync loop thread, at its safe boundary) ---------------------------------------

    def _read_until(self) -> Any:
        root = self.root
        if root is None:
            from hermes_constants import get_default_hermes_root
            root = get_default_hermes_root()
        read = self.read_doc
        if read is None:
            from hermes_cli.config_backend import read_config_doc_readonly as read
        try:
            node = read(Path(root) / "config.yaml")
        except FileNotFoundError:
            return None
        for key in CONFIG_KEY:
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        return _valid_until(node)

    def boundary(self) -> None:
        """Check the config (start/stop a capture) and flush. The loop calls this just before it waits."""
        try:
            self._loop = threading.get_ident()
            try:
                until = self._read_until()
            except Exception:  # health: allow BLE001 -- an unreadable config only disables diagnostics
                until = _ERROR
            cap = self.capture
            if cap is not None:
                reason = self._stop_reason(cap, until)
                if reason is None:
                    self._flush(cap)
                    return
                self._stop(cap, reason)
            if until is not _ERROR and until is not None and until not in self._consumed:
                self._consumed.append(until)
                remaining = until - self.wall()
                if 0 < remaining <= MAX_ACTIVATION_S:
                    self._start(until, remaining)
        except Exception:  # health: allow BLE001 -- diagnostics must never break the sync loop
            self._broken()

    def _stop_reason(self, cap: _Capture, until: Any) -> Optional[str]:
        if until is _ERROR:
            return "config_error"
        if until is None or until != cap.until:
            return "disabled"
        if self.wall() >= until:
            return "expired"
        if self.clock_ns() >= cap.deadline_ns:
            return "bound"
        return None

    def _start(self, until: float, remaining: float) -> None:
        path = log_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size >= self.file_max // 2:
                os.replace(path, path.with_name(path.name + ".1"))
        except FileNotFoundError:
            pass
        try:  # resolved before the fd exists; any failure only leaves the capture unverified
            code, src, moved = (self.code_identity or _code_identity)()
        except Exception:  # health: allow BLE001 -- identity failure marks provenance unverified, never leaks an fd
            code, src, moved = None, "none", None
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:  # until the capture owns the fd, any failure closes it
            now = self.clock_ns()
            cap = _Capture(until, now + int(remaining * 1e9), fd, random.SystemRandom().getrandbits(62))
            cap.bytes = os.fstat(fd).st_size  # earlier captures count against the same file cap
            rec = {"ev": "start", "t": now, "wall_ms": int(self.wall() * 1000), "until_ms": int(until * 1000),
                   "deadline_ns": cap.deadline_ns, "pid": os.getpid(), "run": cap.run, "schema": SCHEMA,
                   "queue_max": self.queue_max, "file_max": self.file_max, "code_src": src}
        except BaseException:
            os.close(fd)
            raise
        if code is not None:
            rec["code"] = code
        if moved is not None:
            rec["disk_moved"] = moved
        with self.lock:
            self.capture = cap
            self._emit(cap, rec)
        self._flush(cap)

    def stop(self, reason: str = "loop_exit") -> None:
        cap = self.capture
        if cap is not None:
            try:
                self._stop(cap, reason)
            except Exception:  # health: allow BLE001 -- diagnostics must never break the sync loop's exit
                self._broken()

    # --- record queue and file -------------------------------------------------------------------------------

    def _broken(self) -> None:
        cap = self.capture
        if cap is not None:
            cap.errors += 1

    def _ord(self, board: str) -> int:
        return self._ords.setdefault(board, len(self._ords))

    def _emit(self, cap: _Capture, rec: dict) -> None:
        """Caller holds the lock. A full queue still consumes the sequence number: the gap is visible."""
        rec["seq"] = cap.seq
        cap.seq += 1
        if len(cap.queue) >= self.queue_max:
            cap.lost += 1
            return
        cap.queue.append(rec)

    def _encode(self, cap: _Capture, batch) -> list[bytes]:
        lines = []
        for rec in batch:
            if all(_allowed(k, v) for k, v in rec.items()):
                lines.append(json.dumps(rec, separators=(",", ":")).encode("ascii") + b"\n")
            else:
                cap.errors += 1
                cap.lost += 1
        return lines

    def _fit(self, cap: _Capture, lines: list[bytes], budget: int) -> bytes:
        kept, size = [], 0
        for line in lines:
            if size + len(line) > budget:
                cap.lost += len(lines) - len(kept)
                cap.capped = True
                break
            kept.append(line)
            size += len(line)
        return b"".join(kept)

    def _write(self, cap: _Capture, data: bytes) -> None:
        view = memoryview(data)
        try:
            while view:
                view = view[self.write(cap.fd, view):]
        except OSError:
            cap.write_failed = True
        cap.bytes += len(data) - len(view)

    def _flush(self, cap: _Capture) -> None:
        t0 = self.clock_ns()
        with self.lock:
            batch, cap.queue = cap.queue, deque()
        data = self._fit(cap, self._encode(cap, batch), self.file_max - STOP_RESERVE_BYTES - cap.bytes)
        if data and not cap.write_failed:
            self._write(cap, data)
        if cap.write_failed:
            cap.lost += len(batch)
            self._stop(cap, "write_error")
            return
        t1 = self.clock_ns()
        cap.flushes += 1
        cap.flush_ns += t1 - t0
        with self.lock:
            self._emit(cap, {"ev": "flush", "t0": t0, "t1": t1, "n": len(batch), "bytes": len(data)})
        if cap.capped:
            self._stop(cap, "file_cap")

    def _stop(self, cap: _Capture, reason: str) -> None:
        with self.lock:
            if self.capture is cap:
                self.capture = None
            batch, cap.queue = cap.queue, deque()
            marks = list(cap.pending.items())
            for board, mark in cap.claims.items():
                marks.append((board, mark))
        data = self._fit(cap, self._encode(cap, batch), self.file_max - STOP_RESERVE_BYTES - cap.bytes)
        tail = []
        for board, mark in marks[:PEND_MAX]:
            tail.append({"ev": "pend", "seq": cap.seq, "b": board, **mark.fields()})
            cap.seq += 1
        valid = reason in COMPLETE_REASONS and not (cap.lost or cap.errors or cap.write_failed)
        tail.append({"ev": "stop", "seq": cap.seq, "t": self.clock_ns(), "wall_ms": int(self.wall() * 1000),
                     "reason": reason, "run": cap.run, "pid": os.getpid(), "lost": cap.lost, "errors": cap.errors,
                     "flushes": cap.flushes, "flush_ns": cap.flush_ns, "ovh_ns": cap.ovh_ns,
                     "pend_more": max(0, len(marks) - PEND_MAX), "valid": valid})
        cap.seq += 1
        data += b"".join(json.dumps(r, separators=(",", ":")).encode("ascii") + b"\n" for r in tail)
        if not cap.write_failed:
            self._write(cap, data)
        try:
            os.close(cap.fd)
        except OSError:
            pass

    # --- hooks -----------------------------------------------------------------------------------------------

    def decoded_change(self, board: str, callback: Callable[[str], Any], kind: Any) -> Any:
        """FeedWatcher, for a decoded change frame only: record it, then run ``callback`` under the same lock as
        the round's selection, so a change is claimed by exactly one selection."""
        if self.capture is None:
            return callback(board)
        t = self.clock_ns()
        with self.lock:
            try:
                self._note_change(board, kind, t)
            except Exception:  # health: allow BLE001 -- diagnostics must never drop a feed notification
                self._broken()
            return callback(board)

    def _note_change(self, board: str, kind: Any, t: int) -> None:
        cap = self.capture
        if cap is None:
            return
        locked = self.clock_ns()
        b = self._ord(board)
        seq = cap.seq
        self._emit(cap, {"ev": "chg", "b": b, "t": t, "kind": _enum(kind, "kind"), "lk": locked - t})
        cap.pending[b] = _merge(cap.pending.get(b), _Mark(seq, t))
        cap.ovh_ns += self.clock_ns() - locked

    def select(self, fn: Callable, boards: list, now: float, pending: Callable[[list], bool]) -> list:
        """The loop's ``SyncWork.select``: claim each selected board's pending watermark at the moment the round
        consumes its dirty flag. Later changes stay pending for that board's next service."""
        cap = self.capture
        if cap is None or threading.get_ident() != self._loop:
            return fn(boards, now, pending)
        seen: list = []

        def observed(paths):
            got = pending(paths)
            if got:
                seen.append((list(paths), self.clock_ns()))
            return got
        t0 = self.clock_ns()
        with self.lock:
            out = fn(boards, now, observed)
            t1 = self.clock_ns()
            try:
                self._claim(cap, boards, out, seen, t0, t1)
            except Exception:  # health: allow BLE001 -- diagnostics must never change the selection
                self._broken()
        return out

    def _claim(self, cap: _Capture, boards: list, out: list, seen: list, t0: int, t1: int) -> None:
        if self.capture is not cap:
            return
        by_path = {path: slug for slug, path in boards}
        local = claims = 0
        for paths, t in seen:
            if len(paths) == 1 and paths[0] in by_path:
                b = self._ord(by_path[paths[0]])
                mark = cap.pending.get(b) or cap.claims.get(b)
                if mark is None or mark.l_t is None:
                    cap.pending[b] = _merge(cap.pending.get(b), _Mark(l_t=t))
                    self._emit(cap, {"ev": "loc", "b": b, "t": t})
                    local += 1
        for slug, _path in out:
            b = self._ord(slug)
            mark = _merge(cap.claims.pop(b, None), cap.pending.pop(b, None))
            if mark is not None:
                cap.claims[b] = mark
                claims += 1
        self._emit(cap, {"ev": "sel", "t0": t0, "t1": t1, "n": len(out), "nb": len(boards), "claims": claims,
                         "local": local})
        cap.ovh_ns += self.clock_ns() - t1

    def board_start(self, slug: str, results: list) -> Optional[tuple]:
        cap = self.capture
        if cap is None:
            return None
        try:
            with self.lock:
                b = self._ord(slug)
            return cap, b, len(results), threading.get_ident() == self._loop, self.clock_ns()
        except Exception:  # health: allow BLE001 -- diagnostics must never stop a board's service
            self._broken()
            return None

    def board_end(self, token: Optional[tuple], results: list,
                  error: Optional[Callable[[Any], Any]] = None) -> None:
        """After one board's service: ``results`` grew by its RoundResult, or the service raised."""
        if token is None:
            return
        t1 = self.clock_ns()
        cap, b, before, loop, t0 = token
        try:
            rec: dict = {"ev": "svc", "b": b, "t0": t0, "t1": t1, "loop": loop, "out": "raised"}
            if len(results) > before:
                result = results[-1]
                failed = error(result) if error is not None else getattr(result, "error", None)
                rec["out"] = "error" if failed else "ok"
                if failed:
                    rec["err"] = _err_class(failed)
                if isinstance(getattr(result, "mode", None), str):
                    rec["mode"] = _enum(result.mode, "mode")
            with self.lock:
                if self.capture is not cap:
                    return
                mark = cap.claims.pop(b, None) if loop else None
                if mark is not None:
                    rec.update(mark.fields())
                    if rec["out"] != "ok":  # never a match: back to pending, its failure counted
                        mark.fails += 1
                        cap.pending[b] = _merge(mark, cap.pending.get(b))
                self._emit(cap, rec)
            cap.ovh_ns += self.clock_ns() - t1
        except Exception:  # health: allow BLE001 -- diagnostics must never break the sync round
            self._broken()

    def report(self, fn: Callable[[], Any]) -> Any:
        """The loop's ``Reporter.report_due``: its full outer duration, with the reporter's component spans."""
        cap = self.capture
        if cap is None or threading.get_ident() != self._loop:
            return fn()
        marks: dict = {}
        self._marks = marks
        t0 = self.clock_ns()
        try:
            result = fn()
        except BaseException:
            self._end_report(cap, marks, t0, "none")
            raise
        self._end_report(cap, marks, t0, "sent" if result is True else "not_sent" if result is False else "none")
        return result

    def _end_report(self, cap: _Capture, marks: dict, t0: int, res: str) -> None:
        t1 = self.clock_ns()
        self._marks = None
        with self.lock:
            if self.capture is cap:
                self._emit(cap, {"ev": "rep", "t0": t0, "t1": t1, "res": res, **marks})

    def span(self, name: str):
        marks = self._marks
        if marks is None or name not in SPANS or threading.get_ident() != self._loop:
            return _NULL
        return _Span(self, marks, name)

    def note_cpu_sample(self, measured: Any) -> None:
        marks = self._marks
        if marks is None or not isinstance(measured, dict) or threading.get_ident() != self._loop:
            return
        for src, dst in (("cpu_busy_window_ms", "cpu_win_ms"), ("cpu_busy_sample_end_ms", "cpu_end_ms")):
            if type(measured.get(src)) is int:
                marks[dst] = measured[src]

    def wait(self, fn: Callable[..., Any], *args: Any) -> Any:
        """The loop's wait for work. Its start is the loop's safe boundary: config check and flush first."""
        self.boundary()
        cap = self.capture
        if cap is None:
            return fn(*args)
        t0 = self.clock_ns()
        why = fn(*args)
        t1 = self.clock_ns()
        with self.lock:
            if self.capture is cap:
                self._emit(cap, {"ev": "wait", "t0": t0, "t1": t1, "why": _enum(why, "why")})
        return why


def _allowed(key: str, value: Any) -> bool:
    kind = FIELDS.get(key)
    if kind is int or kind is bool:
        return type(value) is kind
    if kind == "hex40":
        return isinstance(value, str) and bool(_HEX40.fullmatch(value))
    return isinstance(kind, frozenset) and value in kind


_DIAG = TimingDiag()


# Facade for the sync loop, FeedWatcher, sync_all_once and Reporter: each looks the instance up at call time.
def decoded_change(board: str, callback: Callable[[str], Any], kind: Any) -> Any:
    return _DIAG.decoded_change(board, callback, kind)


def select(fn: Callable, boards: list, now: float, pending: Callable[[list], bool]) -> list:
    return _DIAG.select(fn, boards, now, pending)


def board_start(slug: str, results: list) -> Optional[tuple]:
    return _DIAG.board_start(slug, results)


def board_end(token: Optional[tuple], results: list) -> None:
    _DIAG.board_end(token, results)


def report(fn: Callable[[], Any]) -> Any:
    return _DIAG.report(fn)


def span(name: str):
    return _DIAG.span(name)


def note_cpu_sample(measured: Any) -> None:
    _DIAG.note_cpu_sample(measured)


def wait(fn: Callable[..., Any], *args: Any) -> Any:
    return _DIAG.wait(fn, *args)


def stop(reason: str = "loop_exit") -> None:
    _DIAG.stop(reason)
