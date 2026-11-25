"""Central Kanban sync client (phase 2 of hermes-mods ``core/kanban-central-service.md``). [daveey]

Connects the phase 1 seams in :mod:`hermes_cli.kanban_db_sync` to the Board service (the hermes-hub Worker's
per-board Durable Object):

- **pull first**: ``GET /v2/boards/<id>/changes?since=<cursor>`` pages are applied through
  :func:`~hermes_cli.kanban_db_sync.apply_remote_changes` with the cursor advancing in the same transaction;
  ``410`` (cursor below the retention floor) rebuilds from ``/snapshot``.
- **then push**: pending capture-trigger outbox rows go to ``POST /v2/boards/<id>/ops`` as row ops in FIFO order,
  HLC-stamped here (triggers cannot). ``applied``/``merged`` remove the row; ``rejected`` marks it and, on a
  primary board, restores the service's row.
- **modes** (per board, ``kanban_sync_state.mode``): ``shadow`` pushes the live board's outbox but applies the feed
  only into a separate copy (``<root>/kanban/sync-shadow/<board>.db``) that nothing else reads, so ``sync diff`` can
  compare the two while the live board stays local. ``primary`` applies the feed into the live board and claims
  through service leases (:class:`HubLeaseClient`).
- **import** enqueues every existing row once (tasks as ``import`` so an id another gateway created is refused).

The gateway runs :func:`run_forever` as a supervised watcher while ``kanban.sync.enabled`` is on. Identity is the
gateway's hub enrollment (``~/.config/hermes-mail/{config.json,gateway.key}``), overridable with
``kanban.sync.{url,key_file,gateway}``.
"""

from __future__ import annotations

import json
from hermes_cli import loop_accounting as la
import logging
import os
import re
import sqlite3
import threading
import threading as _hub_threading  # Hub's own handle: tests replace the module-level ``threading``/``time``
import time
from time import monotonic as _hub_monotonic
import urllib.error
import urllib.parse
import urllib.request
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from hermes_cli import kanban_awaited_card_wake as acw  # parks naming a card a pull or rebuild moved re-probe now
from hermes_cli import kanban_db_sync as sync, kanban_sync_wire as wire_ops
from hermes_cli import kanban_replica_lag as lag  # pushed rows the replica holds only after its next pull
from hermes_cli import kanban_sync_backlog as backlog  # page budget per pull; boards the loop reruns at once
from hermes_cli import kanban_sync_outbox_age as outbox_age  # oldest pending op: status, WARNING, defect card
from hermes_cli.kanban_sync_backlog import BUSY_RETRY_S, PULL_PAGE_BUDGET, Pulled
from hermes_cli import kanban_sync_timing as sync_timing  # off unless kanban.sync.diagnostics.timing_until arms it
from hermes_cli import kanban_sync_cpu as sync_cpu
from hermes_cli import kanban_sync_probe_diag as probe_diag, kanban_sync_sweep_cadence as sweep_cadence
from hermes_cli.kanban_sync_pending import (discard_replica_writes, guard_replica, pending_task_fields,
                                           without_pending)
from hermes_cli import kanban_sync_refusal as refusal  # one board's ContainmentRefused ends only its round
from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
from hermes_cli.kanban_sync_connections import connection, connection_scope, invalidate
from hermes_cli.kanban_sync_hub_routes import HubRoutesMixin
from hermes_cli.kanban_replay_sender import PushResult, ReplayFault, push
from hermes_cli.kanban_sync_compat import REASSESSMENT_REFUSALS, TRANSIENT_REASONS, transient_rejection  # re-exported compatibility seam
from hermes_cli.kanban_sync_hub_transport import peer_closed as _peer_closed, decode_json
from hermes_cli.kanban_sync_hub_transport import BoundedRedirects, BoundedResponseError, read_response
from hermes_cli.kanban_sync_client_attempt import note_attempt, raise_bounded_error, replay_after_timeout
from hermes_cli.kanban_sync_bounded_probe import DeadlineRequests as _DeadlineRequests, schedstat, watch_written
from hermes_cli.kanban_sync_repair_window import (  # shared per-board repair-window state, re-exported
    _REPAIR_CREDIT, _REPAIR_DEFERRED, _REPAIRED, _REPAIRED_AT, applies_chain, decide, deferred_repairs_due,
    next_deferred_repair, note_round, reset as reset_repair_window,
)
from hermes_cli.kanban_sync_own_writes import credited, reset as reset_own_writes, take as take_credited
from hermes_cli.kanban_sync_links_inputs import ledger_slices, links_signature
from hermes_cli.kanban_sync_pickup_publish import publish_pickup_if_changed, reset_published_pickup  # moved out, re-exported

_log = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 5.0
PUSH_BATCH = 200
MAX_BATCHES_PER_ROUND = 25
FEED_PAGE = 500
SNAPSHOT_PAGE = 1000
HTTP_TIMEOUT_S = 30.0
EVENT_IMPORT_WINDOW_S = 30 * 24 * 3600  # the service's event retention (worker board.ts EVENT_RETENTION_MS)
# Row order for an import: links need both tasks, everything else names its task.
BOARD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")  # the service's BOARD_RE
IMPORT_ORDER = ("tasks", "task_links", "task_runs", "task_comments", "task_events", "task_attachments", "task_chat_links")


class SyncUnavailable(RuntimeError):
    """Sync cannot run here (no hub enrollment, bad config)."""


class HubError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"hub {status}: {detail}")
        self.status, self.detail = status, detail


class BoardBudgetExhausted(TimeoutError):
    """The shared Board deadline ran out before this attempt; an earlier attempt may have been sent."""


class Resync(Exception):
    """The cursor is below the service's retention floor: rebuild from a snapshot."""


class SchemaAhead(RuntimeError):
    """The board holds columns this core neither syncs nor keeps local: a newer core migrated it. Pushing would
    silently drop those columns from every op (``_wire_fields``), so this core must not sync the board. [daveey]"""

    def __init__(self, columns: dict[str, set[str]]) -> None:
        self.columns = columns
        named = "; ".join(f"{t}: {', '.join(sorted(c))}" for t, c in sorted(columns.items()))
        super().__init__(f"this core does not declare columns a newer core added ({named}); refusing to sync "
                         "so their values are not dropped. Run a current core in the gateway that holds the "
                         "host sync role")


# (db file, schema_version, identity of the declared tasks columns) -> undeclared columns.
_SCHEMA_SEEN: dict[tuple[str, int, int], dict[str, set[str]]] = {}


def schema_ahead(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Per synced table, the columns this core does not know (``undeclared_columns``), cached per schema version
    so a round costs one PRAGMA, not one per table. [daveey]"""
    version = conn.execute("PRAGMA schema_version").fetchone()[0]
    key = (str(sync._db_key(conn)), int(version), id(sync.SYNCED_COLUMNS["tasks"]))
    if key not in _SCHEMA_SEEN:
        _SCHEMA_SEEN[key] = sync.undeclared_columns(conn)
    return _SCHEMA_SEEN[key]


def refuse_if_schema_ahead(conn: sqlite3.Connection) -> None:
    ahead = schema_ahead(conn)
    if ahead:
        raise SchemaAhead(ahead)


@dataclass(frozen=True)
class Identity:
    url: str
    key: str
    gateway: str


def load_identity(cfg: Optional[dict] = None) -> Identity:
    """The gateway's hub enrollment: ``kanban.sync.{url,key_file,gateway}`` or the hermes-mail files."""
    cfg = dict(cfg if cfg is not None else sync.sync_config())
    mail_dir = Path.home() / ".config" / "hermes-mail"
    mail: dict[str, Any] = {}
    try:
        mail = json.loads((mail_dir / "config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        pass
    url = str(cfg.get("url") or mail.get("hub_url") or mail.get("url") or "").rstrip("/")
    gateway = str(cfg.get("gateway") or mail.get("gateway") or "")
    key_file = Path(str(cfg.get("key_file") or mail_dir / "gateway.key")).expanduser()
    try:
        key = key_file.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        raise SyncUnavailable(f"kanban sync: no hub key at {key_file} ({exc})") from exc
    if not url or not gateway or not key:
        raise SyncUnavailable("kanban sync: hub url, gateway name and key are all required")
    return Identity(url=url, key=key, gateway=gateway)


CPU_REPORT_BUDGET_S = 2.0
_CPU_FIELDS = ("cpu_busy_pct", "cpu_busy_window_ms", "cpu_busy_sample_end_ms")


def _bounded_call(ident: Identity, method: str, path: str, body: Optional[dict], budget_s: float,
                  probe: Optional[dict] = None) -> dict:
    """One request with a hard total wall-clock budget, on a private connection that is closed afterwards.

    The request runs on a daemon thread whose own Hub carries the absolute deadline (every attempt's socket timeout
    is recomputed by ``Hub._remaining``). Waiting for that thread is bounded by the same deadline, so a response
    that trickles in byte by byte, each recv inside its timeout, still cannot hold the caller past ``budget_s``.
    A trickle never trips a socket timeout, so on expiry the caller also shuts the request's socket down (or, if it
    is still connecting, the connect does so itself): the abandoned thread's blocked recv returns at once and the
    thread closes its connection. A POST is never resent, so nothing is retried on that socket. Name resolution
    (getaddrinfo) cannot be interrupted; it is bounded by the system resolver's own timeouts."""
    deadline = time.monotonic() + budget_s
    box: dict = {}
    expiry = _hub_threading.Lock()

    def cut(conn) -> None:
        import socket
        sock = getattr(conn, "sock", None)
        if sock is not None:
            try:  # the base method: never touches an SSL object another thread is reading through
                socket.socket.shutdown(sock, socket.SHUT_RDWR)
            except OSError:
                pass

    class _Bounded(Hub):
        def _connection(self, parts, timeout=None):
            pair = super()._connection(parts, timeout)
            conn = pair[0]
            open_socket = conn.connect

            def connect_then_check() -> None:
                open_socket()
                with expiry:
                    if box.get("expired"):
                        cut(conn)
            conn.connect = connect_then_check
            if probe is not None:  # the metrics sender's per-send line: were request bytes written? [daveey]
                watch_written(conn, probe)
            with expiry:
                box["conn"] = conn
            return pair

    hub = _Bounded(ident, timeout=budget_s, deadline=deadline)

    def run() -> None:
        try:
            box["result"] = hub._call(method, path, body)
        except BaseException as exc:  # health: allow BLE001 -- re-raised in the caller's thread below
            box["error"] = exc
        finally:
            hub.close()
            if probe is not None:
                probe["worker"] = schedstat()

    worker = _hub_threading.Thread(target=run, daemon=True, name="kanban-bounded-call")
    worker.start()
    worker.join(max(0.0, deadline - time.monotonic()))
    if worker.is_alive():
        if probe is not None:
            # The worker's socket times out at this same deadline, so it may exit between is_alive() and this read.
            # Its task entry goes only after run() returned, whose finally already stored its own schedstat.
            stat = schedstat(worker.native_id)
            if stat is not None:
                probe["worker"] = stat
        with expiry:
            box["expired"] = True
            if "conn" in box:
                cut(box["conn"])
        raise TimeoutError(f"Board request exceeded its {budget_s:g} s total budget")
    if "error" in box:
        raise_bounded_error(box["error"], deadline, time.monotonic(), budget_s)
    return box["result"]


class Hub(HubRoutesMixin):
    """The Board service's HTTP API for one gateway.

    Requests reuse one kept-alive HTTP/1.1 connection per thread. A fresh connection per request (``urlopen``) paid a
    TCP connect, a TLS handshake and a platform trust evaluation (truststore → ``SecTrustEvaluateWithError`` on macOS)
    every time: ~8.8 ms of thread CPU per request against ~0.5 ms on a reused connection, at several requests a second
    while cards run. Errors keep ``urlopen``'s contract: non-2xx → :class:`HubError` / :class:`Resync`, transport
    failures → ``urllib.error.URLError`` (an ``OSError``). A proxy from the environment keeps the ``urlopen`` path.
    """

    # Drop a connection idle this long instead of racing the server's own idle close. Not a data cache: it only bounds
    # how long a socket is reused.
    IDLE_REUSE_S = 30.0

    def __init__(self, ident: Identity, timeout: float = HTTP_TIMEOUT_S, *, deadline: Optional[float] = None) -> None:
        self.ident, self.timeout = ident, timeout
        self.deadline = deadline
        self._local = _hub_threading.local()
        self._ssl_context = None
        self._ssl_lock = _hub_threading.Lock()

    def put_attachment(self, board: str, uid: str, digest: str, data: bytes) -> dict:
        from hermes_cli.kanban_sync_attachment_http import upload
        return upload(self, board, uid, digest, data)

    def get_attachment_blob(self, board: str, uid: str) -> bytes:
        from hermes_cli.kanban_sync_attachment_http import download
        return download(self, board, uid)

    def _headers(self) -> dict:
        return {"authorization": f"Bearer {self.ident.key}", "content-type": "application/json",
                "user-agent": "hermes-kanban-sync/1"}

    def _remaining(self, timeout: Optional[float] = None) -> float:
        """Recompute the shared absolute budget immediately before each HTTP attempt."""
        timeout = self.timeout if timeout is None else timeout
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
            if timeout <= 0:
                raise BoardBudgetExhausted("Board request budget exhausted")
        return timeout

    @sync_cpu.board_call
    def _call(self, method: str, path: str, body: Optional[dict] = None, *, replay_safe: bool = False,
              max_response_bytes: Optional[int] = None) -> dict:
        if max_response_bytes is not None and (type(max_response_bytes) is not int or max_response_bytes <= 0):
            raise ValueError("max_response_bytes must be a positive integer")
        timeout = self._remaining()
        data = None if body is None else json.dumps(body).encode()
        parts = urllib.parse.urlsplit(self.ident.url)
        bounded: dict[str, Any] = {} if max_response_bytes is None else {"max_response_bytes": max_response_bytes}
        if parts.scheme not in ("http", "https") or not parts.hostname or self._proxied(parts):
            return self._call_urlopen(method, path, data, timeout, **bounded)
        return self._call_kept_alive(parts, method, path, data, timeout, replay_safe=replay_safe, **bounded)

    def _call_urlopen(self, method: str, path: str, data: Optional[bytes], timeout: Optional[float] = None, *,
                      max_response_bytes: Optional[int] = None) -> dict:
        from hermes_cli.kanban_sync_hub_urlopen import call

        return call(self, method, path, data, timeout, max_response_bytes=max_response_bytes)

    @staticmethod
    def _proxied(parts: urllib.parse.SplitResult) -> bool:
        proxies = urllib.request.getproxies()
        return bool(proxies.get(parts.scheme) or proxies.get("all")) and not urllib.request.proxy_bypass(parts.hostname)

    def close(self) -> None:
        """Close this thread's kept-alive connection (other threads' close when they reuse or exit)."""
        self._drop()

    def _drop(self) -> None:
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn is not None and getattr(self._local, "pid", None) == os.getpid():
            try:
                conn.close()
            except OSError:
                pass

    def _connection(self, parts: urllib.parse.SplitResult, timeout: Optional[float] = None):
        """``(connection, reused)`` for this thread. A connection inherited across ``fork`` is never touched.

        ``timeout`` is this request's (deadline-capped) budget; a reused socket takes it too."""
        timeout = self.timeout if timeout is None else timeout
        loc = self._local
        conn = getattr(loc, "conn", None)
        if conn is not None and loc.pid != os.getpid():
            loc.conn = conn = None  # the parent process's socket: leave it to the parent
        if conn is not None and (_hub_monotonic() - loc.used > self.IDLE_REUSE_S or _peer_closed(conn)):
            self._drop()
            conn = None
        if conn is not None:
            conn.timeout = timeout
            try:
                if getattr(conn, "sock", None) is not None:
                    conn.sock.settimeout(timeout)
            except OSError:  # the socket is already dead: nothing was sent on it, so open a fresh one
                self._drop()
                conn = None
        if conn is not None:
            return conn, True
        import http.client
        if parts.scheme == "https":
            with self._ssl_lock:
                if self._ssl_context is None:
                    import ssl
                    self._ssl_context = ssl.create_default_context()  # truststore-injected when installed
            conn = http.client.HTTPSConnection(parts.hostname, parts.port, timeout=timeout,
                                               context=self._ssl_context)
        else:
            conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
        loc.conn, loc.pid, loc.used = conn, os.getpid(), _hub_monotonic()
        return conn, False

    def _call_kept_alive(self, parts: urllib.parse.SplitResult, method: str, path: str, data: Optional[bytes],
                         timeout: Optional[float] = None, *, replay_safe: bool = False,
                         max_response_bytes: Optional[int] = None) -> dict:
        import http.client
        target = (parts.path or "") + path
        headers = self._headers()
        for attempt in (0, 1):
            attempt_timeout = self._remaining(timeout)  # exhausted here: no attempt was made, so no timing
            started = time.monotonic()  # this attempt only: a stale-socket retry restarts the clock
            try:
                conn, reused = self._connection(parts, attempt_timeout)
                try:
                    conn.request(method, target, body=data, headers=headers)
                except (OSError, http.client.HTTPException) as exc:
                    self._drop()
                    if reused and attempt == 0:  # a stale kept-alive socket: the request never reached the server
                        continue
                    if replay_after_timeout(exc, attempt, replay_safe, self.deadline, time.monotonic()):
                        continue
                    raise urllib.error.URLError(exc) from exc
                try:
                    resp = conn.getresponse()
                except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError) as exc:
                    self._drop()
                    # Only GET or an explicitly replay-safe operation may repeat after a lost answer.
                    # Reserve is revision-fenced; other POSTs (ops, leases) must not opt in.
                    if reused and attempt == 0 and (method == "GET" or replay_safe):
                        continue
                    raise urllib.error.URLError(exc) from exc
                except (OSError, http.client.HTTPException) as exc:
                    self._drop()
                    if replay_after_timeout(exc, attempt, replay_safe, self.deadline, time.monotonic()):
                        continue
                    raise urllib.error.URLError(exc) from exc
                try:
                    raw = read_response(resp, max_response_bytes, deterministic=200 <= resp.status < 300)
                except BoundedResponseError:
                    self._drop()  # unread bytes must never become the next response; never replay this refusal
                    raise
                except (OSError, http.client.HTTPException) as exc:
                    self._drop()
                    if replay_after_timeout(exc, attempt, replay_safe, self.deadline, time.monotonic()):
                        continue
                    raise urllib.error.URLError(exc) from exc
                if resp.will_close:
                    self._drop()
                else:
                    self._local.used = _hub_monotonic()
                status = resp.status
                if 300 <= status < 400 and method == "GET":  # the Board service sends none; urlopen follows them
                    bounded: dict[str, Any] = {} if max_response_bytes is None else {"max_response_bytes": max_response_bytes}
                    return self._call_urlopen(method, path, data, timeout, **bounded)
                if 300 <= status < 400:  # never resend a POST the server may have applied
                    raise HubError(status, raw.decode("utf-8", "replace")[:500])
                if not 200 <= status < 300:
                    text = raw.decode("utf-8", "replace")[:500]
                    if status == 410:
                        raise Resync(text)
                    raise HubError(status, text)
                return decode_json(raw)
            except Exception as exc:  # health: allow BLE001 -- stamps diagnostics, then re-raises the same error
                note_attempt(exc, self.deadline, started, time.monotonic())
                raise
        raise AssertionError("unreachable")  # pragma: no cover

    def ops(self, board: str, ops: list[dict], *, custody=None, legacy_parents=()) -> dict:
        if custody is not None:
            from hermes_cli.kanban_sync_exposure import send
            return send(self, custody, board, ops, legacy_parents=legacy_parents)
        return self._call("POST", f"/v2/boards/{board}/ops", {"ops": ops})

    def changes(self, board: str, since: int, limit: int = FEED_PAGE) -> dict:
        return self._call("GET", f"/v2/boards/{board}/changes?since={int(since)}&limit={int(limit)}")

    def snapshot_page(self, board: str, table: str, after: str = "", limit: int = SNAPSHOT_PAGE) -> dict:
        from urllib.parse import quote
        return self._call("GET", f"/v2/boards/{board}/snapshot?table={table}&after={quote(after)}&limit={limit}")

    def containment_context(self, board: str, task: str, producer: str | None = None) -> dict:
        query = urllib.parse.urlencode({"task": task, **({"producer": producer} if producer is not None else {})})
        return self._call("GET", f"/v2/boards/{urllib.parse.quote(board, safe='')}/containment/context?{query}")

    def row_evidence(self, board: str, table: str, uid: str, *, max_response_bytes: Optional[int] = None) -> dict:
        """Exact authoritative evidence; optional wire/decoded cap is enforced before JSON parsing."""
        from urllib.parse import urlencode
        from hermes_cli.kanban_replay_reply import row_evidence_envelope
        bounded: dict[str, Any] = {} if max_response_bytes is None else {"max_response_bytes": max_response_bytes}
        return row_evidence_envelope(self._call("GET", f"/v2/boards/{board}/evidence?" + urlencode({"table": table, "uid": uid}), **bounded))

    def sessions(self, board: str, body: dict) -> dict:
        """Session owners (session handover, :mod:`hermes_cli.session_owner`): ``get`` / ``move``. A 409 refusal
        returns its body (``{reason, owner}``) instead of raising."""
        try:
            return self._call("POST", f"/v2/boards/{board}/sessions", body)
        except HubError as exc:
            if exc.status != 409:
                raise
            try:
                res = json.loads(exc.detail)
            except ValueError:
                raise exc from None
            if not isinstance(res, dict) or not res.get("reason"):
                raise
            return res

    def report_metrics(self, metrics: dict) -> dict:
        """This machine's own load for the registry: Runners display and worker-first pool admission
        (see hermes_cli.machine_metrics). Sending stays best-effort.

        A report carrying a CPU busy sample (hub overflow gate) gets a hard :data:`CPU_REPORT_BUDGET_S` total: its
        own Hub whose per-attempt budget comes from ``_remaining`` (connect, resend, redirect), plus a wall-clock
        bound on the whole exchange so a trickling response cannot stretch it. On expiry it raises
        ``TimeoutError``; the sample is dropped, never resent (the Board also rejects a stale sample by its end
        stamp, since a client timeout does not prove the server never processes the request)."""
        if not any(k in metrics for k in _CPU_FIELDS):
            return self._call("POST", "/v2/machines/me/metrics", metrics)
        return _bounded_call(self.ident, "POST", "/v2/machines/me/metrics", metrics, CPU_REPORT_BUDGET_S)


_HTTP_HUB = Hub  # Concrete transport identity survives a caller replacing the factory.

def board_paths() -> list[tuple[str, Path]]:
    """``(slug, kanban.db)`` for every board on this host that ``kanban.sync.boards`` selects."""
    from hermes_cli.kanban_sync_board_paths import board_paths as discover
    return discover(sync, BOARD_ID_RE)


def ensure_link_boards(slugs: Iterable[str], gateway: str) -> list[str]:
    """Create a link-only board file for each slug this host has no board for (kanban_db_sync.LINKS_DIR)."""
    from hermes_cli import kanban_db
    local = {slug for slug, _ in board_paths()}
    created = []
    for slug in sorted(set(slugs) - local):
        if not BOARD_ID_RE.match(slug):
            continue
        path = kanban_db.kanban_home() / "kanban" / sync.LINKS_DIR / f"{slug}.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(path)
        try:
            ensure_identity(conn, slug, gateway)
        finally:
            conn.close()
        created.append(slug)
    return created


def shadow_path(slug: str) -> Path:
    from hermes_cli import kanban_db
    return kanban_db.kanban_home() / "kanban" / sync.SHADOW_DIR / f"{slug}.db"


def _connect(path: Path) -> sqlite3.Connection:
    from hermes_cli import kanban_db_connect
    return kanban_db_connect.connect(path)


def ensure_identity(conn: sqlite3.Connection, board: str, gateway: str) -> dict:
    st = sync.sync_state(conn)
    if st is None:
        raise SyncUnavailable("kanban sync is not installed on this board (kanban.sync.enabled / kanban.sync.boards)")
    if st["board_id"] != board or st["gateway_id"] != gateway:
        sync.set_identity(conn, board, gateway)
        st = sync.sync_state(conn)
    return st


# --- HLC ------------------------------------------------------------------------------------------

def _next_hlc(conn: sqlite3.Connection, gateway: str) -> str:
    """Monotonic per-gateway HLC ``<ms:13>-<counter:4>-<gateway>``; the last stamp survives restarts."""
    last = (sync.sync_state(conn) or {}).get("hlc_last") or ""
    try:
        last_ms, last_ctr = int(last[:13]), int(last[14:18])
    except ValueError:
        last_ms, last_ctr = 0, 0
    now = time.time_ns() // 1_000_000
    ms, ctr = (now, 0) if now > last_ms else (last_ms, last_ctr + 1)
    if ctr > 9999:
        ms, ctr = ms + 1, 0
    stamp = f"{ms:013d}-{ctr:04d}-{gateway}"
    conn.execute("UPDATE kanban_sync_state SET hlc_last = ?", (stamp,))
    return stamp


# --- push -----------------------------------------------------------------------------------------


def _full_row_if_unseen(conn: sqlite3.Connection, row: dict) -> dict:
    """A task edit captured before the service has acknowledged the card (``sync_version`` NULL: it was created
    before sync was switched on and not imported yet) carries only the changed columns, which the service cannot
    insert. Send the card's full current row instead; a later edit is then a plain merge.

    Child rows (runs, comments, events, attachments, chat links) are small and always go whole: an update captured
    for a row whose insert the service refused earlier (a deploy still rolling out) would otherwise reach it with no
    ``task_id`` and be refused ``not_found``."""
    if row["op"] in ("compat", "compat_import") and row["table_name"] == "tasks":
        from hermes_cli.kanban_worker_placement import acknowledged_outbox
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (row["row_uid"],)).fetchone()
        return acknowledged_outbox(row, task)
    if row["op"] != "upsert":
        return row
    if row["table_name"] in sync._UID_TABLES:
        child = conn.execute(f"SELECT * FROM {row['table_name']} WHERE uid = ?", (row["row_uid"],)).fetchone()
        return {**row, "fields": _row_fields(conn, row["table_name"], child)} if child is not None else row
    if row["table_name"] != "tasks":
        return row
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (row["row_uid"],)).fetchone()
    if task is None or task["sync_version"] is not None:
        from hermes_cli.kanban_worker_placement import acknowledged_outbox
        return acknowledged_outbox(row, task)
    fields = row["fields"] if isinstance(row["fields"], dict) else json.loads(row["fields"])
    virtual = {k: v for k, v in fields.items() if k in sync.VIRTUAL_WIRE_FIELDS["tasks"]}  # not in the row
    return {**row, "fields": {**_row_fields(conn, "tasks", task), **virtual}}


# Rejections the service lifts on its own; anything else is final. A capped number of retries keeps a real mismatch
# (an old service that will never know the table) visible as rejected instead of retrying forever.

# Backed off 2 s doubling to 5 min: 20 attempts span about an hour, longer than a Durable Object takes to pick up
# a deploy (observed: minutes to an hour on a busy board).
MAX_TRANSIENT_ATTEMPTS = 20


def _backoff_s(attempts: int) -> int:
    return min(2 ** (attempts + 1), 300)


def _unacked_parents(conn: sqlite3.Connection, rows: list[dict], gateway: str) -> list[tuple[dict, str]]:
    """Full task upserts for cards that child rows in this batch belong to but the service has not acknowledged
    (``sync_version`` NULL), sent ahead of them: a child captured before its card was queued (a board attached
    while busy, then imported) would otherwise be refused ``not_found`` and hold the card back behind it."""
    if not any(r["table_name"] != "tasks" for r in rows):
        return []
    in_batch = {r["row_uid"] for r in rows if r["table_name"] == "tasks"}
    # A materialized parent owns its retry even during backoff. New children must
    # not synthesize a competing full-row upsert while that retry is pending.
    in_batch.update(r[0] for r in conn.execute(
        "SELECT row_uid FROM kanban_sync_outbox WHERE table_name='tasks' AND state='pending' AND op!='release'"))
    in_batch.update(r[0] for r in conn.execute(
        "SELECT row_uid FROM kanban_sync_omissions WHERE table_name='tasks' AND state!='delivered'"))
    from hermes_cli.kanban_sync_reserved_key import replacement_children
    recovering = replacement_children(conn)
    wanted: list[str] = []
    for r in rows:
        if r["table_name"] == "tasks" or r["op_id"] in recovering:
            continue
        if r["table_name"] == "task_links":
            if r["op"] != "upsert":
                continue
            # Both ends of a new link: a link must never reach the Board ahead of a card it names. [daveey]
            tids = list(str(r["row_uid"]).split(">", 1))
        else:
            fields = r["fields"] if isinstance(r["fields"], dict) else json.loads(r["fields"] or "{}")
            tids = [fields.get("task_id")]
        for tid in tids:
            if tid and tid not in in_batch and tid not in wanted:
                wanted.append(tid)
    out: list[tuple[dict, str]] = []
    for tid in wanted:
        task = conn.execute("SELECT * FROM tasks WHERE id = ? AND sync_version IS NULL", (tid,)).fetchone()
        if task is None:
            continue
        saved = conn.execute("SELECT wire FROM kanban_sync_parent_envelopes WHERE task_id=?", (tid,)).fetchone()
        op = json.loads(saved[0]) if saved else {
            "client_op_id": sync.uuid7(), "table": "tasks", "uid": tid, "op": "upsert",
            "fields": wire_ops.wire_fields("tasks", _row_fields(conn, "tasks", task)), "hlc": _next_hlc(conn, gateway)}
        out.append((op, tid))
    return out


def _within_batch(conn: sqlite3.Connection, rows: list[dict], wire: list[dict],
                  gateway: str) -> tuple[list[dict], list[dict], list[tuple[dict, str]]]:
    """``(rows, wire, parents)`` trimmed so parents + rows fit one request. The Board refuses more than PUSH_BATCH
    ops (400), and the parents ride on top of a full batch: one unacknowledged card behind 200 pending rows failed
    every round with 201 ops and nothing left the hub for 7 h (2026-11-17). The dropped rows go in the next batch."""
    parents = _unacked_parents(conn, rows, gateway)
    # Re-checked after every trim: a card whose own tasks row fell off the tail is no longer "in the batch", so
    # the next pass can return more parents than the last. Rows shrink each pass; one row has at most two parents.
    while len(parents) + len(rows) > PUSH_BATCH and len(rows) > 1:
        rows = rows[:max(1, min(len(rows) - 1, PUSH_BATCH - len(parents)))]
        wire = wire[:len(rows)]
        parents = _unacked_parents(conn, rows, gateway)
    return rows, wire, parents


def _push_legacy(conn: sqlite3.Connection, hub: Hub, board: str, gateway: str, *, primary: bool,
         max_batches: int = MAX_BATCHES_PER_ROUND, before_head: bool = False,
         seen: Optional[dict] = None) -> PushResult:
    """Drain pending outbox rows in FIFO batches. A transport failure leaves them pending (retried next round).

    ``before_head`` (t_01a1d36ddcea59fe): a round sets it before its pulls, and after them while it stopped behind the
    feed head. It stops at the first run row (sent whole, the Board keeps the last write, so a pending startup could
    overwrite an end the Board holds in an unread page; ``_local_row_is_newer`` lets that end land locally first),
    and skips a card whose live ``status`` the Board's row has as done/archived (a stale wake: the strip below judges
    it once the completion is pulled). See ``kanban_sync_backlog.sendable_before_head``; ``seen`` is its Board-read
    cache (one per round, shared by both sends)."""
    from hermes_cli.kanban_db_connect import write_txn
    from hermes_cli import kanban_sync_live_claim as _live_claim
    from hermes_cli import kanban_actionability_transport as retain_transport
    refuse_if_schema_ahead(conn)  # an older core would strip the newer columns; the ops stay pending [daveey]
    out, seen = PushResult(), {} if seen is None else seen
    for _ in range(max_batches):
        from hermes_cli.kanban_sync_parent_order import select_pending
        from hermes_cli.kanban_sync_status_origin import absorb_clock, capability
        origin_cap = capability(hub, board)  # one snapshot per batch: selection and serialization agree
        rows, selected, stopped = select_pending(conn, hub, board, seen, now=int(time.time()),
                                                limit=PUSH_BATCH, before_head=before_head, origin_cap=origin_cap)
        if not rows:
            break
        command_batch = retain_transport.own_batch(rows)
        retain_split, rows = len(command_batch) < len(rows), command_batch
        retain_transport.before_send(conn, rows, hub, board)
        with write_txn(conn):
            absorb_clock(conn, origin_cap)
            from hermes_cli.kanban_terminal_guard import strip_stale_push  # no wake reopens a done card [daveey]
            from hermes_cli import kanban_sync_envelope as envelopes
            envelopes.prune_parents(conn)
            rows, _wire, parents = _within_batch(conn, rows, [{} for _r in rows], gateway)
            wire = [retain_transport.outbound(conn, r) if r["op"] in ("retain", "actionability_record") else envelopes.outbound(conn, r, lambda r=r: strip_stale_push(
                conn, wire_ops.translate(_full_row_if_unseen(conn, r), _next_hlc(conn, gateway))), supported=bool(origin_cap),
                reassessment=type(origin_cap.get("owner_reassessment_v1")) is int and origin_cap["owner_reassessment_v1"] == 1) for r in rows]
            parents = [(envelopes.parent(conn, tid, lambda op=op: op, supported=bool(origin_cap)), tid) for op, tid in parents]
            wire = [op for op, _tid in parents] + wire
        try:
            resp = hub.ops(board, wire, custody=conn, legacy_parents=[op for op, _tid in parents]) if isinstance(hub, _HTTP_HUB) else hub.ops(board, wire)
        except (HubError, OSError) as exc:
            with write_txn(conn):
                conn.executemany("UPDATE kanban_sync_outbox SET attempts = attempts + 1, last_error = ? WHERE op_id = ?",
                                 [(str(exc)[:300], r["op_id"]) for r in rows])
            raise
        out.sent += len(rows)
        authoritative: list[sync.RemoteChange] = []
        with write_txn(conn):
            for res in resp.get("results", []):
                status, op_id = res.get("status"), res.get("client_op_id")
                row = next((r for r in rows if r["op_id"] == op_id), None)
                if row is None:
                    from hermes_cli.kanban_sync_compat import rejected_parent
                    row = rejected_parent(conn, parents, res)
                if row is None:
                    continue
                if row["op"] in ("retain", "actionability_record"):
                    result = retain_transport.record_result(conn, row, res)
                    setattr(out, result, getattr(out, result) + 1)
                    continue  # No generic transient remap, mirror edits or acknowledgement authority.
                if status in ("applied", "merged"):
                    from hermes_cli.kanban_sync_omissions import acknowledge
                    sent = next(op.get('fields', {}) for op in wire if op['client_op_id'] == op_id)
                    acknowledge(conn, op_id, res.get("row"), sent=sent)
                    lag.acked(shadow_path(board), lag.acked_task_ids([row]))  # the replica has it after its next pull
                    conn.execute("DELETE FROM kanban_sync_outbox WHERE op_id = ?", (op_id,))
                    if row["table_name"] == "tasks" and row["op"] != "release" and res.get("version"):
                        conn.execute("UPDATE tasks SET sync_version = MAX(COALESCE(sync_version, 0), ?) WHERE id = ?",
                                     (res["version"], row["row_uid"]))
                    elif row["table_name"] == "task_runs" and res.get("version"):
                        # The Board now holds this row at that version: its own feed copy of it (or anything
                        # older) must not overwrite the local row later.
                        conn.execute("UPDATE task_runs SET sync_version = MAX(COALESCE(sync_version, 0), ?) "
                                     "WHERE uid = ?", (res["version"], row["row_uid"]))
                    setattr(out, status, getattr(out, status) + 1)
                    if status == "merged" and primary and res.get("row"):
                        authoritative.append(_row_change(row["table_name"], res["row"]))
                elif transient_rejection(res) and row["attempts"] + 1 < MAX_TRANSIENT_ATTEMPTS:
                    # The service will accept this later (a deploy still rolling out, a storage hiccup, an earlier op
                    # on the card that was itself transient): keep it pending and retry next round.
                    reason = f"{res.get('reason')}: {res.get('detail') or ''}".strip(": ")
                    # A fresh op id: the service remembers every answer by op id (idempotency), so a retry under the
                    # old id would get the same rejection back forever.
                    from hermes_cli.kanban_sync_omissions import remap
                    new_id = sync.uuid7()
                    remap(conn, op_id, new_id)
                    from hermes_cli.kanban_sync_reserved_key import remap as remap_key_recovery
                    remap_key_recovery(conn, op_id, new_id)
                    conn.execute("UPDATE kanban_sync_outbox SET op_id = ?, wire_json = NULL, attempts = attempts + 1, last_error = ?, "
                                 "retry_after = ? WHERE op_id = ?",
                                 (new_id, f"retrying: {reason}"[:500], int(time.time()) + _backoff_s(row["attempts"]), op_id))
                    out.deferred += 1
                else:
                    reason = f"{res.get('reason')}: {res.get('detail') or ''}".strip(": ")
                    conn.execute("UPDATE kanban_sync_outbox SET state = 'rejected', last_error = ?, attempts = attempts + 1 "
                                 "WHERE op_id = ?", (reason[:500], op_id))
                    out.rejected += 1
                    _log.warning("kanban sync: %s %s %s rejected (%s)", board, row["table_name"], row["row_uid"], reason)
                    if res.get("reason") in ("forbidden_field", *REASSESSMENT_REFUSALS):
                        from hermes_cli.kanban_sync_compat import VIRTUAL_DROPPED, enqueue_retry, record_rejection
                        sent_op = next(op for op in wire if op["client_op_id"] == op_id)
                        if enqueue_retry(conn, row, sent_op, res) != VIRTUAL_DROPPED:
                            record_rejection(conn, row, reason)
                    elif _live_claim.is_live_claim(row, res):  # task row, run handoff, lifecycle event (hub #111)
                        if _live_claim.refused(conn, row, res, reason, primary, authoritative):
                            continue
                    if primary and res.get("row"):
                        authoritative.append(_row_change(row["table_name"], res["row"]))
        if authoritative:
            cursor = (sync.sync_state(conn) or {}).get("cursor", 0)
            sync.apply_remote_changes(conn, authoritative, new_cursor=cursor, board=board)
        if out.deferred or stopped or (len(selected) < PUSH_BATCH and not retain_split):  # no hot deferred retry
            break
    return out


# --- pull -----------------------------------------------------------------------------------------

def _row_change(table: str, row: dict, *, seq: int = 0, client_op_id: Optional[str] = None) -> sync.RemoteChange:
    return sync.RemoteChange(seq=seq, table=table, uid=str(row["uid"]), op="upsert", fields=row,
                             version=int(row.get("version") or 0), hlc=str(row.get("hlc") or ""),
                             origin_gateway=str(row.get("origin_gateway") or ""), client_op_id=client_op_id)


def adapt(change: dict) -> Optional[sync.RemoteChange]:
    """A feed change -> RemoteChange. Lease rows are dropped: the task's full row carries its lease mirror."""
    table = change.get("table")
    if table not in sync.SYNCED_COLUMNS:
        return None
    fields = change.get("fields") or {}
    return sync.RemoteChange(
        seq=int(change.get("seq") or 0), table=table, uid=str(change["uid"]), op=str(change["op"]), fields=fields,
        version=int(fields.get("version") or 0), hlc=str(change.get("hlc") or ""),
        origin_gateway=str(change.get("origin_gateway") or ""), client_op_id=change.get("client_op_id"),
    )


def own_partition(conn: sqlite3.Connection, changes: list[sync.RemoteChange], gateway: str) -> list[sync.RemoteChange]:
    """The changes for cards this gateway created (``created_on`` = it, or unset) and their child rows.

    A primary board's live file holds only its own cards, so the dispatcher's sweeps (stale claims, orphans,
    concurrency counts) never see another gateway's work; the replica copy holds the whole board.

    A link is kept when its CHILD is ours, even if the parent is another gateway's card: the edge gates our card,
    and the Board's claim check counts it. Dropping it made the local promoter re-promote a child the Board refused
    to lease (60 promoted/claim_rejected events in 7 min, 2026-10-28). The parent's status comes from the replica
    (``kanban_db_sync.refresh_remote_parents``). [daveey]"""
    kept: list[sync.RemoteChange] = []
    mine: set[str] = set()

    def is_mine(task_id: Optional[str]) -> bool:
        if not task_id:
            return False
        if task_id in mine:
            return True
        return conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is not None

    for ch in changes:
        if ch.table == "tasks":
            if ch.op == "upsert" and ch.fields.get("created_on") in (None, gateway):
                mine.add(ch.uid)
            if not is_mine(ch.uid):
                continue
        elif ch.table == "task_links":
            _parent, _, child = ch.uid.partition(">")
            if not is_mine(child):
                continue
        elif not is_mine(ch.fields.get("task_id") if ch.op == "upsert" else
                         _child_task(conn, ch.table, ch.uid)):
            continue
        kept.append(ch)
    return kept


def _child_task(conn: sqlite3.Connection, table: str, uid: str) -> Optional[str]:
    row = conn.execute(f"SELECT task_id FROM {table} WHERE uid = ?", (uid,)).fetchone()
    return row[0] if row else None


def pull(conn: sqlite3.Connection, hub: Hub, board: str, *, observers: bool,
         partition: Optional[str] = None, max_pages: Optional[int] = PULL_PAGE_BUDGET) -> Pulled:
    """Apply the feed pages after this file's cursor, at most ``max_pages`` (None: to the head). Returns the number
    of changes read; its ``more`` is true when the budget ran out before the head. On a board with an outbox
    (primary), fields with an unanswered local edit are not overwritten; ``partition`` (a gateway name) keeps only
    that gateway's cards. [daveey]"""
    from hermes_cli.kanban_sync_stop import checkpoint
    total = pages = 0
    from hermes_cli.kanban_sync_apply_budget import apply_page, service_pushes
    changed: dict[str, str] = {}  # status changes applied, for the parks that name those cards
    discard_replica_writes(conn, hub, board)  # a replica's outbox is never sent: its ops would mask the feed
    cursor = int((sync.sync_state(conn) or {}).get("cursor") or 0)
    while True:
        if max_pages is not None and pages >= max_pages:
            return Pulled(total, more=True, status_changed=changed)  # unbounded, a backlog starved the round's push for hours
        pages += 1
        checkpoint()
        service_pushes()
        page = acw.changes_page(hub, board, cursor, changed)  # a Resync carries the pages applied before it
        checkpoint()
        changes = [c for c in (adapt(ch) for ch in page.get("changes", [])) if c is not None]
        if partition is not None:
            changes = own_partition(conn, changes, partition)
        changes = without_pending(changes, pending_task_fields(conn))
        nxt = int(page.get("next", cursor))
        if (not page.get("changes") and nxt == cursor and not page.get("more")
                and not (sync.sync_state(conn) or {}).get("applying")):
            # An idle poll: no change, same cursor. The apply would only rewrite ``applying`` and ``cursor`` to
            # the values they hold, a write transaction (WAL frame, fsync, observer wake) every round on every
            # board for nothing. [daveey]
            return Pulled(total, status_changed=changed)
        # [daveey] The pre-filter above can miss a local edit committed after it read the outbox (a claim in
        # flight): ``local_pending_wins`` checks again inside the apply transaction.
        from hermes_cli.kanban_sync_quarantine import certified_page
        changes = certified_page(conn, hub, board, changes)
        applied = apply_page(conn, changes, new_cursor=nxt, board=board, observers=observers,
                             local_pending_wins=True)
        changed.update(getattr(applied, "status_changed", None) or {})
        total += len(page.get("changes", []))
        cursor = nxt
        if not page.get("more"):
            return Pulled(total, status_changed=changed)


def resync(conn: sqlite3.Connection, hub: Hub, board: str, *, observers: bool, partition: Optional[str] = None) -> int:
    """Rebuild from the service snapshot (every live row, paged per table), then resume the feed at its head.
    ``partition`` keeps only that gateway's cards (a primary board's live file)."""
    from hermes_cli.kanban_sync_stop import checkpoint
    head: Optional[int] = None
    count = 0
    for table in IMPORT_ORDER:
        after = ""
        while True:
            checkpoint()
            page = hub.snapshot_page(board, table, after)
            checkpoint()
            head = page["head"] if head is None else head
            rows = page.get("rows", [])
            changes = [_row_change(table, r) for r in rows]
            if partition is not None:
                changes = own_partition(conn, changes, partition)
            cursor = int((sync.sync_state(conn) or {}).get("cursor") or 0)
            from hermes_cli.kanban_sync_quarantine import certified_page
            changes = certified_page(conn, hub, board, changes)
            sync.apply_remote_changes(conn, changes, new_cursor=cursor, board=board, observers=observers,
                                      local_pending_wins=True)  # [daveey] an unpushed local edit outlives a rebuild
            count += len(rows)
            if not page.get("more"):
                break
            after = str(page["next"])
    from hermes_cli.kanban_sync_snapshot_tasks import finish_rebuild  # the history replay can reopen a done card
    finish_rebuild(conn, hub, board, int(head or 0), partition=partition)
    return count


# --- one board ------------------------------------------------------------------------------------

@dataclass
class RoundResult:
    board: str
    mode: str
    pulled: int = 0
    push: Optional[PushResult] = None
    error: Optional[str] = None
    repaired: dict[str, list[str]] = field(default_factory=dict)  # cards re-pushed by repair_divergence
    rejection_resolutions: list[dict] = field(default_factory=list)
    repair_deferred: bool = False  # a replica-only change waits for the repair window (see ``decide``)
    behind: bool = False  # a pull stopped at its page budget: the loop runs this board again at once
    skipped: bool = False  # SyncRoundBusy: another holder (a dispatcher tick) had the board; retried shortly
    cause: Optional[str] = None  # a failure's host-to-Board transport class (kanban_sync_host_outage.classify)


def open_shadow(slug: str, gateway: str) -> sqlite3.Connection:
    path = shadow_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as cleanup:
        conn = _connect(path)
        cleanup.callback(conn.close)
        ensure_identity(conn, slug, gateway)
        guard_replica(conn)  # only the pull writes a replica (kanban_sync_pending.guard_replica)
        from hermes_cli import kanban_sync_quarantine as quarantine
        quarantine.restore(conn, quarantine.load_rebuild(path), path)
        cleanup.pop_all()
        return conn


@contextmanager
def _pulled_replica(slug: str, hub: Hub, gateway: str, max_pages: Optional[int] = PULL_PAGE_BUDGET, *,
                    diagnostic: bool = False):
    """Borrow the pulled replica for this round; retention rebuilds discard the old handle. ``diagnostic``
    (``sync diff``) neither rebuilds on Resync (it raises) nor transfers attachments."""
    path = shadow_path(slug)
    with ExitStack() as stack:
        replica = stack.enter_context(connection(path, lambda: open_shadow(slug, gateway)))
        rebuilt = False
        generation = lag.pull_started(path)  # done only below: a failed pull leaves this gateway's pushes lagging
        from hermes_cli import kanban_sync_replica_unfreeze as unfreeze
        repair_stop = not diagnostic and unfreeze.needed(replica, path)
        try:
            if repair_stop:
                unfreeze.begin(path)
                raise Resync('replica consumed changes while stop-frozen')
            pulled = pull(replica, hub, slug, observers=False, max_pages=max_pages)
        except Resync as exc:
            if diagnostic:
                raise
            before = acw.statuses(replica, getattr(exc, "status_changed", None))  # read before the file goes
            reason = 'consumed changes while stop-frozen' if repair_stop else 'cursor below the retention floor'
            _log.warning("kanban sync: %s replica %s; rebuilding from a snapshot", slug, reason)
            from hermes_cli import kanban_sync_quarantine as quarantine
            quarantine.save_rebuild(replica, path)
            stack.close()
            invalidate(path)  # must close the pooled descriptor BEFORE unlinking its database
            path.unlink(missing_ok=True)
            replica = stack.enter_context(connection(path, lambda: open_shadow(slug, gateway)))
            rebuilt = True
            pulled = Pulled(resync(replica, hub, slug, observers=False), status_changed=acw.rebuilt(before, replica))
            if repair_stop:
                unfreeze.finish(path)  # only a complete fresh snapshot certifies recovery
        if not diagnostic:
            from hermes_cli.kanban_sync_closure_backfill import backfill
            pulled.status_changed.update(backfill(replica, hub, slug))
        if not pulled.more:  # both mean "holds everything up to the feed head", not "a page was read"
            lag.pull_done(path, generation)
            try:  # readers derive the replica's staleness from this (kanban_chat_links); a failed stamp only ages it
                sync.mark_pulled(replica)
            except sqlite3.Error as exc:
                _log.warning("kanban sync: %s replica pull not stamped (%s)", slug, exc)
        if not diagnostic:
            from hermes_cli.kanban_sync_attachments import transfer_pending
            transfer_pending(replica, hub, slug)
        yield replica, pulled, rebuilt


def _pull_replica(slug: str, hub: Hub, gateway: str, live_path: Optional[Path] = None,
                  max_pages: Optional[int] = PULL_PAGE_BUDGET, *, diagnostic: bool = False) -> Pulled:
    """Pull the replica for standalone callers that do not run repairs, only while no sync round holds the board:
    one replica puller per board per host (a ``sync diff`` beside the gateway once wrote the cursor backwards,
    t_01a1d36ddcea59fe). A busy round returns ``busy`` without pulling; the caller reads the replica as-is.
    ``live_path`` defaults to the board's configured file; ``diagnostic``: see :func:`_pulled_replica`."""
    from hermes_cli.kanban_sync_guard import SyncRoundBusy, board_sync_lock
    if live_path is None:
        live_path = dict(board_paths()).get(slug)
    with ExitStack() as stack:
        try:
            if live_path is not None:
                stack.enter_context(board_sync_lock(Path(live_path)))
        except SyncRoundBusy:
            return Pulled(busy=True)
        with _pulled_replica(slug, hub, gateway, max_pages, diagnostic=diagnostic) as (_, pulled, _rebuilt):
            return pulled


def sync_board(slug: str, path: Path, hub: Hub, gateway: str) -> RoundResult:
    """Exclude explicit detach until this round's repairs and sends have ended."""
    from hermes_cli.kanban_placement import config_snapshot
    from hermes_cli.kanban_sync_guard import board_sync_lock
    with board_sync_lock(path), config_snapshot():  # one placement load per round, not one per placeholder check
        return _sync_board_locked(slug, path, hub, gateway)


def _sync_board_locked(slug: str, path: Path, hub: Hub, gateway: str) -> RoundResult:
    """One round for one board. Both modes keep the replica copy (the whole board) current and push the live
    outbox; ``primary`` also applies this gateway's own cards from the feed into the live board."""
    # Dropped first, so a round that fails or finds nothing to repair never leaves a stale deferral that would
    # make the sync loop reselect this board without waiting; a deferring round sets it again.
    _REPAIR_DEFERRED.pop(str(path), None)
    backlog.round_started(path)  # likewise: only a round that completes behind the head reruns at once
    with connection(path, lambda: _connect(path)) as live:
        mode = sync.board_mode(live)
        result = RoundResult(board=slug, mode=mode)
        if mode == "off":
            return result
        refuse_if_schema_ahead(live)  # before pull/repair: an older core must not act on a newer board [daveey]
        ensure_identity(live, slug, gateway)
        from hermes_cli import kanban_sync_quarantine as quarantine
        # An earlier round may have committed a quarantine before a later page
        # failed. Copy that safety state before sending anything this round.
        replica_path = shadow_path(slug)
        if replica_path.exists() and quarantine.file_holds(replica_path):
            with connection(replica_path, lambda: open_shadow(slug, gateway)) as replica:
                quarantine.adopt(replica, live)
        # Send before pulling as well as after: a pull backlog never holds local edits back (9 h, t_01a1d36ddcea59fe).
        # Safe: the Board merges on base_version + field HLC, an acked task's sync_version fences off older feed
        # copies of it, and rejections are resolved here as after the pulls. A failed send never stops the pulls
        # (an /ops outage with /changes up left the replica unpulled, t_01a1d41c2612ebc0): the send after them
        # retries, and its failure fails the round. One Board read per card per round serves both sends; the one
        # before the pulls must read: only its pull can tell an at-head round (the incident slept from the head).
        evidence = backlog.round_evidence(path)
        try:
            _send(live, hub, slug, gateway, mode, result, after_pull=False, before_head=True, seen=evidence)
        except (HubError, OSError, SyncUnavailable, ReplayFault) as exc:
            _log.info("kanban sync: %s push before the pull failed (%s); pulling anyway", slug, exc)
        from hermes_cli.kanban_sync_apply_budget import round_pushes
        with round_pushes(live, hub, slug, gateway, mode, result, evidence), quarantine.mirroring(live), _pulled_replica(slug, hub, gateway) as (replica, pulled, rebuilt):
            quarantine.adopt(replica, live)
            quarantine.notify(live, slug)  # one hub card per new receipt (kanban_sync_quarantine_notice); never raises
            result.pulled = pulled
            result.behind = pulled.more
            acw.expedite(live, pulled.status_changed)  # a park naming a card the pull moved re-probes now [daveey]
            applies: Optional[list] = []  # credited revision steps (kanban_sync_own_writes); None = a rebuild
            if mode == "primary":
                try:  # a never-pulled file rebuilds from a snapshot instead of replaying the feed (None)
                    live_behind = backlog.pull_live(live, hub, slug, gateway)
                    result.behind |= bool(live_behind)
                    applies = None if live_behind is None else applies
                except Resync:
                    from hermes_cli.kanban_sync_resync_sweep import resync_live  # also sweeps the Board's deletes
                    applies = None
                    resync_live(live, hub, slug, gateway)
            from hermes_cli.kanban_sync_repair_revision import ensure_revision
            ensure_revision(live)
            inputs = _repair_inputs(path, replica, mode, gateway)
            # Every step credited since the last round's read: its acks, mirror and reserves, then this pull.
            steps = take_credited(live)
            applies = None if applies is None else steps
            key = str(path)
            outcome = "unchanged"
            repair_changed = inputs is None or _REPAIRED.get(key) != inputs
            if repair_changed:
                outcome = "rebuilt" if rebuilt else decide(key, inputs, applies, _repair_clock(), REPLICA_REPAIR_MIN_S)
                deferred = outcome == "deferred"
                from hermes_cli.kanban_sync_partition_repair import repair_partition_chat_links, repair_partition_links
                if not deferred:
                    from hermes_cli import kanban_sync_partition_content as partition_content
                    decision = partition_content.prepare(live, replica, path=path, inputs=inputs, rebuilt=rebuilt)
                    if not decision.skip:
                        repair_partition_links(live, replica, gateway)
                        repair_partition_chat_links(live, replica, gateway)
            from hermes_cli.kanban_sync_parent_refresh import refresh_and_wake
            # Independent P2 gate and 30s recovery floor; retain mirror-before-repair ordering.
            refresh_and_wake(live, replica, board=slug, mode=mode, rebuilt=rebuilt)
            if repair_changed:
                if deferred:
                    result.repair_deferred = True
                else:
                    from hermes_cli.kanban_sync_repair_content import run
                    result.repaired = run(live, replica, gateway, path=path, inputs=inputs, rebuilt=rebuilt)
                    if not decision.skip:
                        retire_lost_placeholder_links(live, replica, gateway)
                        partition_content.commit(decision, live, replica, inputs=inputs)
                    if inputs is not None:
                        _REPAIRED[key] = inputs
                        _REPAIRED_AT[key] = _repair_clock()
                    _REPAIR_CREDIT.pop(key, None)
            note_round(slug, outcome, _repair_clock())
            if mode == "primary":
                # The mirror now matches this round's pull, including skipped unchanged rows.
                sync.mark_mirrored(replica, slug)  # only then can a parent be missing (kanban_replica_freshness)
                from hermes_cli.kanban_sync_divergence import heal_clobbered_completions, heal_running_over_ended_run
                heal_clobbered_completions(live)
                heal_running_over_ended_run(live)
        # Still behind the head: the Board may hold a run's end in a page not read yet, so run rows wait.
        try:
            _send(live, hub, slug, gateway, mode, result, after_pull=True, before_head=result.behind, seen=evidence)
        except (HubError, OSError, SyncUnavailable, ReplayFault):
            backlog.note(path, result.behind)  # the pulls landed: a board still behind reruns at once
            raise
        if mode == "primary":
            from hermes_cli.kanban_actionability_intents import materialize
            materialize(live, hub, slug)
            from hermes_cli.kanban_worker_placement import reevaluate
            with credited(live):  # reservation observations and the feed pull after them
                reevaluate(live, hub, slug, gateway)
        backlog.completed(slug, path, result.behind)  # at the head: this process's claims on it open
        return result


def _send(live: sqlite3.Connection, hub: Hub, slug: str, gateway: str, mode: str, result: RoundResult, *,
          after_pull: bool, before_head: bool, seen: Optional[dict] = None) -> None:
    """Push the outbox and resolve rejections, adding to ``result``. A round does this before its pulls and after
    them (``after_pull``, which also transfers attachments); for ``before_head`` see :func:`push`."""
    from hermes_cli.kanban_sync_round_send import send
    send(live, hub, slug, gateway, mode, result, after_pull=after_pull, before_head=before_head, seen=seen)


def sync_all_once(hub: Optional[Hub] = None, *,
                  boards: Optional[list[tuple[str, Path]]] = None) -> list[RoundResult]:
    from hermes_cli.kanban_sync_guard import SyncRoundBusy
    from hermes_cli import kanban_containment_functions as udf_errors
    from hermes_cli.kanban_replay_admission import RestoreRequired

    ident = load_identity() if hub is None else hub.ident
    hub = hub or Hub(ident)
    publish_pickup_if_changed(hub)
    from hermes_cli.kanban_source_clones import attest_if_due
    attest_if_due(hub)  # workspace_source clone attestation; inert unless enabled
    results = []
    for slug, path in board_paths() if boards is None else boards:
        mark = sync_timing.board_start(slug, results)
        held = refusal.waiting(slug)
        if held is not None:  # backing off after a containment refusal (run_forever never selects it)
            results.append(RoundResult(board=slug, mode="?", error=held))
            sync_timing.board_end(mark, results)
            continue
        udf_errors.clear_error()
        try:
            results.append(sync_board(slug, path, hub, ident.gateway))
            refusal.cleared(slug)
        except ContainmentRefused as exc:
            # One board's refused page must not stop every board's sync (2026-11-20, kanban_sync_refusal).
            results.append(RoundResult(board=slug, mode="?", error=refusal.refused(slug, exc)))
        except SchemaAhead as exc:
            _log.error("kanban sync: %s not synced: %s", slug, exc)
            results.append(RoundResult(board=slug, mode="?", error=f"SchemaAhead: {exc}"))
        except SyncRoundBusy as exc:
            # Routine since the dispatcher tick holds the board for its pull and reserves: not a failed round
            # (no WARNING, the board is not marked failed, the outbox-age alarm keeps its last verdict).
            _log.debug("kanban sync: round for %s skipped (%s)", slug, exc)
            results.append(RoundResult(board=slug, mode="?", error=f"SyncRoundBusy: {exc}", skipped=True))
        except (HubError, OSError, Resync, sqlite3.Error, SyncUnavailable, RestoreRequired, ReplayFault) as exc:
            _log.warning("kanban sync: round for %s failed (%s: %s)%s", slug, type(exc).__name__, exc,
                         udf_errors.error_suffix(exc))
            from hermes_cli.kanban_sync_host_outage import classify  # one host card, not one per board [daveey]
            results.append(RoundResult(board=slug, mode="?", error=f"{type(exc).__name__}: {exc}", cause=classify(exc)))
        finally:
            sync_timing.board_end(mark, results)
    return results


class FeedWatcher:
    """The Board's push channel for one board: ``GET /v2/boards/<id>/ws`` (WebSocket, gateway key). Any change frame
    sets ``wake`` so the sync loop pulls now instead of at its next poll. Keeps the socket alive with a ping every
    ``ping_s`` and reconnects with backoff; polling still covers a closed socket."""

    def __init__(self, ident: Identity, board: str, wake: threading.Event, keep_running: Callable[[], bool],
                 ping_s: float = 25.0, on_change: Optional[Callable[[str], None]] = None,
                 on_state: Optional[Callable[[str, bool], None]] = None) -> None:
        self.ident, self.board, self.wake, self.keep_running, self.ping_s = ident, board, wake, keep_running, ping_s
        self.on_change = on_change
        self.on_state = on_state
        self.frames = 0

    def url(self) -> str:
        base = self.ident.url
        base = "wss://" + base[len("https://"):] if base.startswith("https://") else "ws://" + base.split("://", 1)[-1]
        return f"{base}/v2/boards/{self.board}/ws"

    def run(self) -> None:
        try:
            from websockets.sync.client import connect
        except ImportError:  # optional: without it the loop polls
            _log.info("kanban sync: websockets not installed; %s is polled only", self.board)
            return
        from hermes_cli.ws_logging import ws_logger
        backoff = 1.0
        while self.keep_running():
            try:
                with connect(self.url(), additional_headers={"authorization": f"Bearer {self.ident.key}",
                                                             "user-agent": "hermes-kanban-sync/1"},
                             open_timeout=HTTP_TIMEOUT_S, logger=ws_logger()) as ws:
                    backoff = 1.0
                    if self.on_state is not None:
                        self.on_state(self.board, True)
                    if self.on_change is not None:
                        self.on_change(self.board)
                        self.wake.set()  # reconnects must catch changes missed while disconnected
                    while self.keep_running():
                        try:
                            raw = ws.recv(timeout=self.ping_s)
                        except TimeoutError:
                            ws.send("ping")
                            continue
                        try:
                            kind = json.loads(raw).get("type")
                        except (ValueError, AttributeError):
                            continue
                        if kind in ("changes", "changed", "resync"):
                            self.frames += 1
                            if self.on_change is not None:
                                sync_timing.decoded_change(self.board, self.on_change, kind)
                            self.wake.set()
            except Exception as exc:  # health: allow BLE001 -- any socket failure: back off and reconnect; polling covers the gap
                if self.on_state is not None:
                    self.on_state(self.board, False)
                _log.info("kanban sync: %s feed socket closed (%s: %s); reconnecting in %.0fs",
                          self.board, type(exc).__name__, exc, backoff)
                deadline = time.monotonic() + backoff
                while self.keep_running() and time.monotonic() < deadline:
                    time.sleep(0.2)
                backoff = min(backoff * 2, 60.0)


LINKS_EVERY_S = 60.0
# state.db answers (session sources, compression lineage, homes) are keyed by value: a pass records the reads it used
# and the next one replays them (``kanban_links_chat_key``). Only a pass that swallowed a failed read has no such key;
# its unchanged board is still reconciled this long after that pass, as every board was before. [daveey]
LINKS_MAX_AGE_S = 300.0
# Per board file: (inputs, started at, state.db reads or None) of the last completed links pass (``_links_inputs``).
_LINKS_DONE: dict[str, tuple[tuple, float, Optional[dict]]] = {}


def _links_clock() -> float:
    return time.monotonic()


# A board's links pass logs its duration with its counts (``kanban sync: <board> chat links {...} in <s>s``); a pass
# with no changes logs only when it took this long, so a slow pass is visible even when it changed nothing; a whole
# pass this slow (ledger read included) also logs ``kanban sync: chat links pass over N board(s) took <s>s``. A board
# skipped as unchanged after LINKS_SLOW_LOG_S or more logs ``<board> chat links unchanged, skipped in <s>s``, and a
# failed board's warning carries ``failed after <s>s``. Each board's time runs from before its connection is borrowed,
# so it counts the connect (any lock wait on it included), ``board_mode`` and the inputs read: lock waits are what it is
# meant to expose. The hub pass ran ~10 min on 2026-11-13 and the line carried no duration to measure it by
# (t_01a1d35d8d7e22a7). [daveey]
LINKS_SLOW_LOG_S = 10.0


def _links_pass_clock() -> float:
    return time.monotonic()


# The running loop's links pass (tests wait on it to observe a pass deterministically); None once the loop exits.
_LINKS_RUNNER: Any = None  # a kanban_sync_links_pass.LinksPass while the loop runs


def _links_unchanged(key: str, inputs: Optional[tuple], slug: str, began: float, reads: Any) -> tuple[bool, float]:
    """(whether a board's links-pass inputs equal its last completed pass's and its state.db reads answer the same
    (no recorded reads: under ``LINKS_MAX_AGE_S`` ago), so the pass is skipped; the pass start to record). Not a
    pure predicate: it pops ``_LINKS_DONE[key]`` and puts it back only on a skip, so a non-skip drops the stale entry
    and the caller re-records it when inputs were readable and
    reconcile succeeded (unreadable inputs or a raising reconcile leave it dropped). A skip ``LINKS_SLOW_LOG_S`` or
    more after ``began`` names its board; ``began`` is taken before the board's connection is
    borrowed, so that time counts the connect (a lock wait included), ``board_mode`` and the inputs read."""
    from hermes_cli import kanban_links_diagnostics as diag
    done = _LINKS_DONE.pop(key, None)
    started = _links_clock()
    reason = diag.board_decision(done, inputs, started, LINKS_MAX_AGE_S, reads)
    diag.board_reason(slug, reason, done[0] if done is not None else None, inputs)
    if reason != "skip":
        return False, started
    _LINKS_DONE[key] = done
    took = _links_pass_clock() - began
    if took >= LINKS_SLOW_LOG_S:
        _log.info("kanban sync: %s chat links unchanged, skipped in %.1fs", slug, took)
    return True, started


@la.accounted("sync.links.inputs")
def _links_inputs(conn: sqlite3.Connection, path: Path, slug: str, gateway: str, mode: str,
                  ledger: Optional[dict[str, frozenset]], signature: Optional[tuple]) -> Optional[tuple]:
    """Everything one board's links pass (``kanban_chat_links.reconcile`` with its creator-chats backfill) reads,
    besides state.db (``kanban_links_chat_key``): the live file's identity and transactional activity revision (tasks,
    events, subscriptions, links; trigger-backed, so dispatcher clocks and sync stamps do not move it), the replica's
    main-file identity/stat and covered activity revision (not transport cursor or heartbeat progress), this gateway's
    ledger rows for the board, the mode and the configuration. Legacy nonempty replica subscriptions are not covered
    by shadow revision triggers and refuse the skip. Read before the pass, so a commit during it re-arms the next one.
    None (cannot tell: no counter, unreadable file) always reconciles. [daveey]"""
    from hermes_cli import kanban_activity_revision as rev
    if ledger is None or signature is None:
        return None
    try:
        st = os.stat(path)
        revision = rev.read_key(conn, rev.LOCAL_TABLES)
        if revision is None:
            return None
        shadow = shadow_path(slug)
        if not shadow.exists():
            replica: tuple = ("absent",)
        else:
            # The loop's own pooled replica handle (generation-recovered on borrow), not another connection.
            with connection(shadow, lambda: open_shadow(slug, gateway)) as replica_conn:
                rst = os.stat(shadow)
                # Creator resolution also reads legacy subscriptions, which shadow activity
                # triggers do not cover. Nonempty legacy stores must use the literal pass.
                if replica_conn.execute("SELECT 1 FROM kanban_notify_subs LIMIT 1").fetchone():
                    return None
                content = rev.read_key(replica_conn, rev.SHADOW_TABLES)
                if content is None:
                    return None
                replica = (rst.st_dev, rst.st_ino, rst.st_size, rst.st_mtime_ns, content)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    return (st.st_dev, st.st_ino), revision, replica, ledger.get(slug, frozenset()), mode, signature


@la.accounted("sync.links")
def reconcile_links(boards: list[tuple[str, Path]], gateway: str, *, skip_unchanged: bool = False) -> None:
    """Bring this gateway's chat links up to date in every synced board (kanban_chat_links.reconcile), creating a
    link-only board for each board this gateway's chats have cards on but this host does not have.

    ``skip_unchanged`` (the sync loop): a board whose links-pass inputs (``_links_inputs``) and state.db reads
    (``kanban_links_chat_key``) equal those of its last completed pass is not reconciled again. A failed pass records
    nothing, so the next one retries. Explicit callers (``hermes kanban sync links``) always reconcile. Boards are
    borrowed through the loop's connection scope (``kanban_sync_connections``). [daveey]"""
    from hermes_cli import kanban_chat_links, kanban_links_chat_key as chat_key
    from hermes_cli import kanban_links_diagnostics as diag
    from hermes_cli.kanban_placement import config_snapshot
    pass_began = _links_pass_clock()
    with diag.source("sync.links"), config_snapshot() as placement, chat_key.pass_reads() as reads:
        ledger: Optional[dict[str, frozenset]] = None
        try:
            with la.pass_("sync.links.sources"):
                cards = kanban_chat_links.reconciliation_cards()
                ledger = la.timed("sync.links.ledger_slices", ledger_slices, cards, gateway)
            local = {slug for slug, _path in boards}
            active = {slug for slug, rows in ledger.items() if any(not row[-1] for row in rows)}  # History only invalidates existing boards.
            if not skip_unchanged or active - local:
                created = la.timed("sync.links.ensure_boards", ensure_link_boards, active, gateway)
                if created:
                    _log.info("kanban sync: link-only boards %s", created)
                    boards = board_paths()
        except Exception as exc:  # health: allow BLE001 -- link-only boards are best effort; local links still reconcile
            _log.warning("kanban sync: link-only boards failed (%s: %s)", type(exc).__name__, exc)
        signature = la.timed("sync.links.signature", links_signature, gateway, placement) if skip_unchanged else None
        for slug, path in boards:
            key = str(path)
            counts: dict = {}
            began = _links_pass_clock()
            try:
                with la.pass_("sync.links.board") as accounting, la.pass_(diag.board_name(slug) + ".work"), connection(path, lambda: _connect(path)) as conn:
                    mode = sync.board_mode(conn)
                    if mode == "off":
                        continue
                    inputs, started = None, 0.0
                    if skip_unchanged:
                        inputs = _links_inputs(conn, path, slug, gateway, mode, ledger, signature)
                        skip, started = _links_unchanged(key, inputs, slug, began, reads)
                        if skip:
                            accounting.skip()
                            continue
                    with reads.board():
                        counts = kanban_chat_links.reconcile(conn, slug, gateway)
                    if inputs is not None:
                        _LINKS_DONE[key] = (inputs, started, reads.key())
                        if reads.key() is None:
                            diag.event(diag.board_name(slug) + ".certificate.degraded")
                took = _links_pass_clock() - began
                if any(counts.values()) or took >= LINKS_SLOW_LOG_S:
                    _log.info("kanban sync: %s chat links %s in %.1fs", slug, counts, took)
            except Exception as exc:  # health: allow BLE001 -- one board's links must not stop the sync loop
                _log.warning("kanban sync: chat links for %s failed after %.1fs (%s: %s)", slug,
                             _links_pass_clock() - began, type(exc).__name__, exc)
    took = _links_pass_clock() - pass_began
    if took >= LINKS_SLOW_LOG_S:
        _log.info("kanban sync: chat links pass over %d board(s) took %.1fs", len(boards), took)


from hermes_cli.kanban_sync_board_watch import BoardWatch as _BoardWatch  # moved out, re-exported
from hermes_cli.kanban_gone_board_handles import sweep as sweep_gone_board_handles


_WATCH = _BoardWatch()
# A full repair pass over a large board is expensive (David-MBP default board, 2026-11-02: 0.73 s thread-CPU
# per pass under cProfile, ~6k rows decoded). The replica's cursor moves on every change any gateway makes to
# the board, so a busy Board made the pass run nearly every round (29 rounds in 90 s; the sync thread used
# 13.5% of a core). A change that only moved the replica cursor therefore waits until this long after the last
# pass; local writes, a rebuilt replica and mode/gateway/placement changes still repair at once.
REPLICA_REPAIR_MIN_S = 30.0


def _repair_clock() -> float:
    return time.monotonic()


def reset_board_watch() -> None:
    """Tests: close the kept connections and forget completed repair passes."""
    from hermes_cli.kanban_sync_repair_content import reset
    from hermes_cli import kanban_sync_partition_content
    reset()
    kanban_sync_partition_content.reset()
    _WATCH.reset()
    reset_repair_window()
    reset_own_writes()
    backlog.reset()
    outbox_age.reset()
    _LINKS_DONE.clear()


def _repair_inputs(live_path: Path, replica: sqlite3.Connection, mode: str, gateway: str) -> Optional[tuple]:
    """Everything the pure repair scans (partition links, remote parents, divergence, missing children, lost
    placeholder links) read: a transactional revision of the live input tables, schema version,
    replica identity/feed cursor, mode, gateway and placement config. Dispatcher clock writes are
    deliberately not inputs. Read before the scans so concurrent relevant commits re-arm the next
    round. Replica writes are its own pull (cursor moves or rebuild replaces the file) and the
    pulled_at stamp, which the scans never read. None (cannot tell) always scans. [daveey]"""
    got = _WATCH.query(live_path, "SELECT revision, schema_version FROM kanban_repair_revision, "
                      "pragma_schema_version WHERE id = 1")
    probe_diag.note(live_path, got)
    if got is None or got[1] is None:
        return None
    live_version = (got[0], tuple(got[1]))
    try:
        replica_file = Path(replica.execute("PRAGMA database_list").fetchone()[2])
        st = os.stat(replica_file)
        cursor = int((sync.sync_state(replica) or {}).get("cursor") or 0)
        from hermes_cli.kanban_placement import config
        placement = json.dumps(config(), sort_keys=True, default=str)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    return live_version, (st.st_dev, st.st_ino, cursor), mode, gateway, placement


def outbox_pending(paths: Iterable[Path]) -> bool:
    """True when any synced board has a local change waiting to be pushed (one indexed read on a kept connection)."""
    for path in paths:
        got = _WATCH.query(path, "SELECT 1 FROM kanban_sync_outbox WHERE state = 'pending' LIMIT 1",
                           unchanged_ok=True)
        probe_diag.note(path, got)
        if got is not None and got[1]:
            return True
    return False


def wait_for_work(wake: threading.Event, paths: Iterable[Path], period: float,
                  keep_running: Callable[[], bool], tick: float = 1.0) -> str:
    """Block until there is sync work: a Board change frame (``wake``), a local change in an outbox, or the poll
    period ran out. Returns which, so a local write is pushed within ``tick`` and a remote one pulled at once."""
    paths = list(paths)
    deadline = time.monotonic() + period
    split = sync_cpu.WaitSplit()  # sync.wait/*: where this step's CPU goes, live
    try:
        while keep_running():
            fired = wake.wait(timeout=max(0.0, min(tick, deadline - time.monotonic())))
            split.mark("event_wait")
            if fired:
                wake.clear()
                return "remote"
            pending = outbox_pending(paths)
            split.mark("outbox_probe")
            if pending:
                return "local"
            if time.monotonic() >= deadline:
                return "period"
        return "stopped"
    finally:
        split.publish()


@connection_scope()
def run_forever(keep_running: Callable[[], bool], interval: Optional[float] = None,
                sleep: Callable[[float], None] = time.sleep) -> None:
    """The gateway's sync loop (call from a worker thread). Installs the lease client for primary boards, holds one
    feed socket per synced board. Connected boards pull on feed/local invalidation; disconnected boards and
    failed pulls retain the poll period. An independent full recovery audits all boards at least every 60s
    (or the configured period if longer), subject to in-flight IO and the next poll tick."""
    from hermes_cli.kanban_sync_stop import run_loop
    run_loop(_run_forever, keep_running, interval)


def _run_forever(keep_running: Callable[[], bool], interval: Optional[float], runstate: Any) -> None:
    try:
        ident = load_identity()
    except SyncUnavailable as exc:
        _log.warning("%s; kanban sync not started", exc)
        runstate.record_down(f"not starting: {exc}")
        return
    from hermes_cli.kanban_sync_schema_alert import note_start, on_give_up  # one hub card per incident [daveey]
    note_start()
    ahead = _schema_ahead_boards(board_paths())
    if ahead:
        on_give_up(runstate, ident.gateway, "not starting: ", ahead)
        return  # give the host sync role up: a current core's gateway can take it [daveey]
    hub = Hub(ident)
    sync.set_lease_client(HubLeaseClient(hub))
    period = float(interval or sync.sync_config().get("interval_seconds") or DEFAULT_INTERVAL_S)
    _log.info("kanban sync: running as gateway %s against %s (feed sockets; poll every %.1fs)", ident.gateway, ident.url, period)
    runstate.started()  # an earlier loop's graceful stop no longer describes this host
    wake = threading.Event()
    from hermes_cli.kanban_sync_work import SyncWork
    work = SyncWork(period)
    watchers: dict[str, threading.Thread] = {}
    next_links = 0.0
    global _LINKS_RUNNER
    from hermes_cli.kanban_sync_links_pass import LinksPass
    links = _LINKS_RUNNER = LinksPass(ident.gateway)
    from hermes_cli.machine_metrics import Reporter
    metrics = Reporter(hub.report_metrics)
    from hermes_cli.machine_report_census import census  # unchanged boards are not re-read per report [daveey]
    metrics.census_fn = census(_WATCH)
    # Hub CPU advisory: measured on its own thread, so a slow sync round can neither void nor delay it. [daveey]
    from hermes_cli.machine_cpu_sampler import AdvisorySampler
    sampler = AdvisorySampler()
    metrics.cpu_take_fn = sampler.take
    metrics.cpu_wait_fn = sampler.wait_fresh  # the sender's bounded wait; never on this thread
    sampler.start()
    # While the CPU gate is on, reports go out on their own anchored thread, each send bounded to 2 s. [daveey]
    from hermes_cli.machine_metrics_sender import MetricsSender, board_post
    sender = MetricsSender(metrics, board_post(ident))
    sender.start()
    try:
        probe_diag.enter()
        with sync_cpu.iterations(keep_running) as rounds:
            for _round in rounds:
                boards = sync_cpu.step(board_paths)
                probe_diag.install(boards)
                if sweep_cadence.due(boards, time.monotonic()):
                    sync_cpu.step(sweep_gone_board_handles)  # close handles a removed board pins
                for slug, _path in boards:
                    if slug not in watchers or not watchers[slug].is_alive():
                        watcher = FeedWatcher(ident, slug, wake, keep_running, on_change=work.changed,
                                              on_state=work.feed_state)
                        watchers[slug] = threading.Thread(target=watcher.run, daemon=True, name=f"kanban-feed-{slug}")
                        watchers[slug].start()
                selected = sync_cpu.step(sync_timing.select, work.select, boards, time.monotonic(), outbox_pending)
                selected += [b for b in sync_cpu.step(deferred_repairs_due, boards, _repair_clock()) if b not in selected]
                selected += [b for b in sync_cpu.step(backlog.due, boards) if b not in selected]
                picked = time.monotonic()  # a board backing off a failed round waits for it, whatever else is due
                selected = [b for b in selected if not work.backing_off(b[0], picked)]
                selected = refusal.runnable(selected)  # a refused board waits out its own backoff, no busy retry
                outbox_age.round_started()
                results = sync_all_once(hub, boards=selected)
                ahead = [r for r in results if (r.error or "").startswith("SchemaAhead")]
                if ahead:
                    _log.error("kanban sync: a newer core migrated this host's board; giving up the sync role")
                    on_give_up(runstate, ident.gateway, "gave up the sync role: ", [(r.board, r.error) for r in ahead])
                    return
                runstate.clear()  # a completed round: the loop runs
                ended = time.monotonic()
                for r in results:
                    if r.skipped:
                        work.changed(r.board)  # run it again after BUSY_RETRY_S, as a changed board
                        continue
                    work.finished(r.board, not r.error, ended)  # a failure backs the board off (kanban_sync_work)
                    if r.push and (r.push.sent or r.push.rejected):
                        _log.info("kanban sync: %s %s pulled=%d pushed=%d rejected=%d", r.board, r.mode, r.pulled,
                                  r.push.sent, r.push.rejected)
                # WARNING: an unsent edit older than 10 min after a good round (a skipped round judges nothing)
                sync_cpu.step(outbox_age.after_round, [r for r in results if not r.skipped], boards)
                if time.monotonic() >= next_links and links.request(boards):  # busy: ask again next iteration
                    next_links = time.monotonic() + LINKS_EVERY_S
                sync_cpu.step(sync_timing.report, sender.loop_report)
                # From this round's results: a board that stopped behind the head reruns at once; one a tick held is
                # retried shortly (a skipped board's stale behind flag would otherwise spin with zero wait).
                remaining = 0.0 if any(r.behind for r in results) else max(0.0, work.deadline - time.monotonic())
                if any(r.skipped for r in results):
                    remaining = min(remaining, BUSY_RETRY_S)
                now = time.monotonic()
                ready = refusal.runnable([(s, p) for s, p in boards if not work.backing_off(s, now)])
                deferred_due = next_deferred_repair(ready)
                if deferred_due is not None:
                    remaining = min(remaining, max(0.0, deferred_due - _repair_clock()))
                # Failed boards' outboxes and deferred repairs wait for backoff; neither may force a zero wait.
                retry_in = work.next_retry(now)
                if retry_in is not None:
                    remaining = min(remaining, retry_in)
                sync_cpu.step(sync_timing.wait, wait_for_work, wake, [p for _, p in ready], remaining, keep_running)
    finally:
        probe_diag.leave()
        links.stop()
        _LINKS_RUNNER = None  # no stale runner for the next loop's tests to wait on
        sender.close()
        sampler.close()
    runstate.record_down(runstate.STOPPED)


def _schema_ahead_boards(boards: list[tuple[str, Path]]) -> list[tuple[str, str]]:
    """``(slug, refusal)`` per board a newer core migrated (logged at ERROR); an unreadable one is the round's error."""
    ahead = []
    for slug, path in boards:
        try:
            with connection(path, lambda p=path: _connect(p)) as conn:
                cols = schema_ahead(conn)
        except (sqlite3.Error, OSError):
            cols = {}
        if cols:
            _log.error("kanban sync: not starting: board %s: %s", slug, SchemaAhead(cols))
            ahead.append((slug, str(SchemaAhead(cols))))
    return ahead


# --- import ---------------------------------------------------------------------------------------

def _row_fields(conn: sqlite3.Connection, table: str, row: sqlite3.Row) -> dict:
    out: dict[str, Any] = {}
    # Every caller already materialized SELECT *: the row carries its schema.
    # Re-reading table_info here issued thousands of PRAGMAs per idle repair round.
    skip = sync.LOCAL_COLUMNS[table] | sync.KEY_COLUMNS[table] | sync.SERVER_COLUMNS.get(table, frozenset())
    allowed = sync.SYNCED_COLUMNS[table] - skip
    for column, value in zip(row.keys(), row):
        if column not in allowed:
            continue
        if (table, column) in sync.REF_COLUMNS:
            ref = conn.execute("SELECT uid FROM task_runs WHERE id = ?", (value,)).fetchone() if value is not None else None
            value = ref[0] if ref else None
        elif isinstance(value, bytes):
            value = {"$blob": value.hex()}
        out[sync._wire_name(table, column)] = value
    return out


def _row_uid(table: str, row: sqlite3.Row) -> str:
    if table == "tasks":
        return row["id"]
    if table == "task_links":
        return f"{row['parent_id']}>{row['child_id']}"
    return row["uid"]


def import_board(conn: sqlite3.Connection, *, tables: Iterable[str] = IMPORT_ORDER,
                 only: Optional[Mapping[str, set[str]]] = None) -> dict[str, int]:
    """Queue every existing row of a synced board for upload (idempotent: the service keys rows by uid).
    ``only`` restricts each table to those row uids (``reconcile`` re-sends just what the service lacks)."""
    from hermes_cli.kanban_db_connect import write_txn
    if sync.board_mode(conn) == "off":
        raise SyncUnavailable("this board does not sync (kanban.sync.enabled / kanban.sync.boards / mode off)")
    counts: dict[str, int] = {}
    now = time.time_ns() // 1_000_000
    with write_txn(conn):
        from hermes_cli.kanban_replay_journal import guard_import
        tables = tuple(tables)
        bound = guard_import(conn, tables, only)
        # Bound rejections retain semantic custody; a bulk import is not authorized repair.
        counts["cleared_rejected"] = (0 if bound or only is not None else
                                      conn.execute("DELETE FROM kanban_sync_outbox WHERE state = 'rejected'").rowcount)
        for table in tables:
            wanted = None if only is None else only.get(table, set())
            if wanted is not None and not wanted:
                counts[table] = 0
                continue
            order = "created_at, id" if table == "tasks" else ("parent_id, child_id" if table == "task_links" else "id")
            n = 0
            # The service drops events older than its retention window; importing them would only churn the feed.
            where = f" WHERE created_at >= {int(time.time()) - EVENT_IMPORT_WINDOW_S}" if table == "task_events" else ""
            for row in conn.execute(f"SELECT * FROM {table}{where} ORDER BY {order}").fetchall():
                if wanted is not None and _row_uid(table, row) not in wanted:
                    continue
                op = "import" if table == "tasks" else "upsert"
                conn.execute(
                    "INSERT INTO kanban_sync_outbox (op_id, table_name, row_uid, op, fields, base_version, hlc, created_at) "
                    "VALUES (?, ?, ?, ?, ?, NULL, '', ?)",
                    (sync.uuid7(), table, _row_uid(table, row), op, json.dumps(_row_fields(conn, table, row)), now),
                )
                n += 1
            counts[table] = n
    return counts


# --- reconcile after a recovery -------------------------------------------------------------------

def _live_uids(conn: sqlite3.Connection, table: str) -> set[str]:
    if table == "tasks":
        return {str(r[0]) for r in conn.execute("SELECT id FROM tasks")}
    if table == "task_links":
        return {f"{p}>{c}" for p, c in conn.execute("SELECT parent_id, child_id FROM task_links")}
    where = f" AND created_at >= {int(time.time()) - EVENT_IMPORT_WINDOW_S}" if table == "task_events" else ""
    return {str(r[0]) for r in conn.execute(f"SELECT uid FROM {table} WHERE uid IS NOT NULL{where}")}


def reconcile(slug: str, path: Optional[Path] = None, hub: Optional[Hub] = None) -> dict[str, Any]:
    """Bring a board and the service back in step after its file lost rows (a corruption rebuild, a
    restore from backup) — including the unsent outbox, which a rebuild cannot recover.

    Reads the service snapshot of every table once. Rows this board has and the service lacks are
    queued for upload (tasks as ``import``, which the service refuses for an id it already holds) and
    pushed. On a ``primary`` board, rows of this gateway's cards that the service has and the file
    lost are applied back from the snapshot; rows the file still has are never overwritten, so local
    edits whose outbox entry was lost are not reverted. Returns counts and the post-reconcile ``diff``."""
    from hermes_cli import kanban_db
    ident = load_identity() if hub is None else hub.ident
    hub = hub or Hub(ident)
    if path is None:
        path = dict(board_paths()).get(slug) or kanban_db.kanban_db_path(board=slug)
    live = _connect(Path(path))
    try:
        mode = sync.board_mode(live)
        if mode == "off":
            return {"board": slug, "mode": "off"}
        ensure_identity(live, slug, ident.gateway)
        # Send what is already pending first (a delete waiting in the outbox must reach the service before
        # its snapshot is compared, or the row would be "restored" here), then refresh the replica: rows the
        # service deleted on purpose (its event GC of done cards, deletes) come back as tombstones there.
        push(live, hub, slug, ident.gateway, primary=mode == "primary")
        backlog.replica_at_head(slug, hub, ident.gateway, Path(path))
        shadow = open_shadow(slug, ident.gateway)
        try:
            tombstoned: dict[str, set[str]] = {}
            for table, uid in shadow.execute("SELECT table_name, row_uid FROM kanban_sync_tombstones"):
                tombstoned.setdefault(str(table), set()).add(str(uid))
        finally:
            shadow.close()
        # Never restore a row this file deleted itself (local tombstone) or still has an unanswered op for.
        held_back: set[tuple[str, str]] = {(str(t), str(u)) for t, u in live.execute(
            "SELECT table_name, row_uid FROM kanban_sync_tombstones")}
        held_back |= {(str(t), str(u)) for t, u in live.execute(
            "SELECT table_name, row_uid FROM kanban_sync_outbox WHERE state = 'pending'")}
        live_uids = {table: _live_uids(live, table) for table in IMPORT_ORDER}
        service_uids: dict[str, set[str]] = {table: set() for table in IMPORT_ORDER}
        restored: dict[str, int] = {}
        for table in IMPORT_ORDER:
            after = ""
            while True:
                page = hub.snapshot_page(slug, table, after)
                rows = page.get("rows", [])
                service_uids[table].update(str(r["uid"]) for r in rows)
                from hermes_cli.kanban_sync_omissions import reconcile_page
                reconcile_page(live, table, rows)
                if mode == "primary":
                    candidates = [r for r in rows if str(r["uid"]) not in live_uids[table]]
                    # Rows written locally since live_uids was read reached the service meanwhile: re-check.
                    now_live = _live_uids(live, table) if candidates else set()
                    lost = own_partition(live, [_row_change(table, r) for r in candidates
                                                if str(r["uid"]) not in now_live
                                                and (table, str(r["uid"])) not in held_back], ident.gateway)
                    from hermes_cli.kanban_sync_quarantine import withhold  # a held task's rows wait, as in a pull
                    lost = withhold(live, lost)
                    if lost:
                        cursor = int((sync.sync_state(live) or {}).get("cursor") or 0)
                        sync.apply_remote_changes(live, lost, new_cursor=cursor, board=slug, observers=False)
                        restored[table] = restored.get(table, 0) + len(lost)
                if not page.get("more"):
                    break
                after = str(page["next"])
        missing = {table: live_uids[table] - service_uids[table] - tombstoned.get(table, set())
                   for table in IMPORT_ORDER}
        from hermes_cli.kanban_sync_reconcile_links import existing_task_chat_links
        missing["task_chat_links"] &= existing_task_chat_links(live, service_uids["tasks"])
        queued = import_board(live, only=missing)
        queued.pop("cleared_rejected", None)
        pushed = push(live, hub, slug, ident.gateway, primary=mode == "primary")
        backlog.replica_at_head(slug, hub, ident.gateway, Path(path))
        shadow = open_shadow(slug, ident.gateway)
        try:
            report = diff(live, shadow, ident.gateway)
        finally:
            shadow.close()
        return {"board": slug, "mode": mode, "queued": {k: v for k, v in queued.items() if v},
                "restored": restored, "pushed": pushed.sent, "rejected": pushed.rejected,
                "deferred": pushed.deferred, "clean": report.get("clean"),
                "field_mismatch": len(report.get("field_mismatch") or {}),
                "missing_in_shadow": len(report.get("missing_in_shadow") or [])}
    finally:
        live.close()


# --- status and diff ------------------------------------------------------------------------------

def status(conn: sqlite3.Connection) -> dict[str, Any]:
    st = sync.sync_state(conn) or {}
    counts = {s: n for s, n in conn.execute("SELECT state, COUNT(*) FROM kanban_sync_outbox GROUP BY state")} if st else {}
    last_error = None
    if st:
        r = conn.execute("SELECT last_error FROM kanban_sync_outbox WHERE last_error IS NOT NULL "
                         "ORDER BY created_at DESC LIMIT 1").fetchone()
        last_error = r[0] if r else None
    mode = sync.board_mode(conn)
    from hermes_cli.kanban_replay_existence import fault
    from hermes_cli.kanban_replay_recovery import fault as transport_fault
    out = {"mode": mode, "board_id": st.get("board_id"), "gateway": st.get("gateway_id"),
           "replay_fault": fault(conn),
           "replay_transport_fault": transport_fault(conn),
           "cursor": st.get("cursor"), "pending": counts.get("pending", 0), "rejected": counts.get("rejected", 0),
           "oldest_pending_age_s": round(age) if st and (age := outbox_age.oldest_pending_age_s(conn)) is not None
           else None, "last_error": last_error}
    from hermes_cli import kanban_sync_quarantine as quarantine
    if quarantined := len(quarantine.entries(conn)):  # a held task is otherwise silent [daveey]
        out["quarantined"] = quarantined
    if mode != "off":
        # A refused start writes no outbox row, so ``last_error`` alone read a stalled board as healthy. [daveey]
        from hermes_cli import kanban_sync_runstate as runstate
        stopped = runstate.describe()
        out["running"] = f"no: {stopped}" if stopped else "yes (no stop recorded)"
    return out


_DIFF_SKIP = {"current_run_id"}  # compared as run uids instead


def _task_rows(conn: sqlite3.Connection, ids: Optional[set[str]] = None) -> dict[str, dict]:
    columns = [c for c in sync.SYNCED_COLUMNS["tasks"] if c not in _DIFF_SKIP]
    present = set(sync.table_columns(conn, "tasks"))
    cols = [c for c in columns if c in present]
    out = {}
    for row in conn.execute(f"SELECT t.id, {', '.join('t.' + c for c in cols)}, r.uid AS run_uid "
                            "FROM tasks t LEFT JOIN task_runs r ON r.id = t.current_run_id"):
        if ids is None or row["id"] in ids:
            out[row["id"]] = dict(row)
    return out


def _child_uids(conn: sqlite3.Connection, table: str, task_ids: set[str],
                gateway: Optional[str] = None) -> set[tuple[str, str]]:
    """Child row uids of ``task_ids``. With ``gateway``, chat links of OTHER gateways' chats on a card created
    elsewhere are left out: a picked-up copy of a foreign card carries no chat links (``import_cards`` copies
    only runs/comments/attachments/events, and the creator's chats are read from the replica), so counting them
    made every running foreign copy a permanent ``task_chat_links`` mismatch with nothing in flight."""
    if table == "task_links":
        return {(p, f"{p}>{c}") for p, c in conn.execute("SELECT parent_id, child_id FROM task_links") if c in task_ids}
    if table == "task_chat_links" and gateway is not None:
        rows = conn.execute(
            "SELECT l.task_id, l.uid FROM task_chat_links l LEFT JOIN tasks t ON t.id = l.task_id WHERE l.uid IS NOT NULL "
            "AND NOT (t.created_on IS NOT NULL AND t.created_on != ?1 AND COALESCE(l.chat_gateway, '') != ?1)", (gateway,))
        return {(t, u) for t, u in rows if t in task_ids}
    return {(t, u) for t, u in conn.execute(f"SELECT task_id, uid FROM {table} WHERE uid IS NOT NULL") if t in task_ids}


def diff(live: sqlite3.Connection, shadow: sqlite3.Connection, gateway: str) -> dict[str, Any]:
    """Compare own rows, pending edits, and rejected mutations; exact audited ended-run retain
    refusals stay visible as command audits rather than replica differences."""
    pending_tasks = {r[0] for r in live.execute(
        "SELECT DISTINCT CASE WHEN table_name = 'tasks' THEN row_uid ELSE json_extract(fields, '$.task_id') END "
        "FROM kanban_sync_outbox WHERE state = 'pending'")}
    # Acknowledged by the service but not pulled back into the replica yet (the replica lags one round):
    # in flight, like an unpushed row, not a difference.
    acked = {r[0]: r[1] for r in live.execute("SELECT id, sync_version FROM tasks WHERE sync_version IS NOT NULL")}
    seen = {r[0]: r[1] or 0 for r in shadow.execute("SELECT id, sync_version FROM tasks")}
    pending_tasks |= {t for t, v in acked.items() if v > seen.get(t, 0)}
    live_tasks = _task_rows(live)
    mine = {r[0] for r in shadow.execute("SELECT id FROM tasks WHERE created_on = ? OR created_on IS NULL", (gateway,))}
    shadow_tasks = _task_rows(shadow, mine | set(live_tasks))
    foreign = shadow.execute("SELECT COUNT(*) FROM tasks WHERE created_on IS NOT NULL AND created_on != ?",
                             (gateway,)).fetchone()[0]
    checked = set(live_tasks) - pending_tasks
    missing = sorted(t for t in checked if t not in shadow_tasks)
    extra = sorted(t for t in mine - set(live_tasks) - pending_tasks)
    fields: dict[str, list[str]] = {}
    for tid in sorted(checked & set(shadow_tasks)):
        a, b = live_tasks[tid], shadow_tasks[tid]
        bad = [k for k in a if k != "id" and a[k] != b.get(k)]
        if bad:
            fields[tid] = bad
    children: dict[str, int] = {}
    for table in ("task_links", "task_comments", "task_runs", "task_chat_links"):
        lv = _child_uids(live, table, checked, gateway)
        sh = _child_uids(shadow, table, checked, gateway)
        n = len(lv ^ sh)
        if n:
            children[table] = n
    from hermes_cli.kanban_sync_retain_drift import rejected_summary
    rejected, audits = rejected_summary(live, shadow)
    clean = not (missing or extra or fields or children or rejected - len(audits))
    return {"clean": clean, "checked": len(checked), "pending": len(pending_tasks & set(live_tasks)),
            "missing_in_shadow": missing, "extra_in_shadow": extra, "field_mismatch": fields,
            "child_mismatch": children, "rejected_ops": rejected, "foreign_cards": foreign,
            "audited_retain_rejections": audits}


def repair_missing_children(live: sqlite3.Connection, shadow: sqlite3.Connection, gateway: str) -> None:
    """Heal capture-gap links, including app-assigned uids the NULL-uid install heal cannot find.

    Only absence is evidence here: never overwrite an existing replica child. Equal acknowledged
    task versions and no outstanding ops distinguish settled local inserts from unpulled work.
    Serialize the live scan and queue so concurrent rounds cannot enqueue the same row twice.
    """
    from hermes_cli.kanban_db_connect import write_txn
    seen = {r[0]: r[1] for r in shadow.execute(
        "SELECT id, sync_version FROM tasks WHERE (created_on = ? OR created_on IS NULL) AND sync_version IS NOT NULL",
        (gateway,))}
    with write_txn(live):
        from hermes_cli.kanban_sync_omissions import repair_holds
        held = {(table, uid) for table in ('tasks', 'task_links', 'task_chat_links')
                for uid in repair_holds(live, table)}
        # Event deletion history can dwarf the links. The tombstone PK starts with table_name;
        # read only the types consulted below, retaining task tombstones as a settlement veto.
        tombstones_sql = ("SELECT table_name, row_uid FROM kanban_sync_tombstones "
                          "WHERE table_name IN ('tasks', 'task_links', 'task_chat_links')")
        held |= {(t, u) for t, u in live.execute(tombstones_sql)}
        held |= {(t, u) for t, u in shadow.execute(tombstones_sql)}
        settled = {r[0] for r in live.execute("SELECT id, sync_version FROM tasks WHERE sync_version IS NOT NULL")
                   if seen.get(r[0]) == r[1] and ("tasks", r[0]) not in held}
        for table in ("task_links", "task_chat_links"):
            remote = _live_uids(shadow, table)
            for row in live.execute(f"SELECT * FROM {table}").fetchall():
                uid = _row_uid(table, row)
                parents = {row["parent_id"], row["child_id"]} if table == "task_links" else {row["task_id"]}
                if not uid or uid in remote or (table, uid) in held or not parents <= settled:
                    continue
                live.execute(
                    "INSERT INTO kanban_sync_outbox (op_id, table_name, row_uid, op, fields, base_version, hlc, created_at) "
                    "VALUES (?, ?, ?, 'upsert', ?, NULL, '', ?)",
                    (sync.uuid7(), table, uid, json.dumps(_row_fields(live, table, row)), time.time_ns() // 1_000_000))
                _log.warning("kanban sync: %s %s absent from the service with nothing in flight "
                             "(an uncaptured write); re-queued", table, uid)


def retire_lost_placeholder_links(live: sqlite3.Connection, shadow: sqlite3.Connection, gateway: str) -> int:
    """Delete from the Board this gateway's placement-attribution chat links (``kanban-placement*`` sessions) that the
    live file no longer holds. Such a link is never a chat (``is_placeholder_session``), so its absence from live is
    the intended state, and before chat-link deletes were captured a local delete left the Board's row behind for
    good: a shadow-only ``task_chat_links`` mismatch at pending=0 (238 rows on the hub, 2026-10-30). Real chat links
    missing from live are left alone, so the diff keeps reporting them. Returns the number of deletes queued."""
    from hermes_cli.kanban_db_connect import write_txn
    from hermes_cli.kanban_placement import config, is_placeholder_session
    placement_config = config()
    lost = [(u, s) for u, s in shadow.execute(
        "SELECT uid, chat_session FROM task_chat_links WHERE chat_gateway = ? AND uid IS NOT NULL", (gateway,))
        if is_placeholder_session(s, placement_config=placement_config)]
    if not lost:
        return 0
    queued = 0
    with write_txn(live):
        have = _live_uids(live, "task_chat_links")
        held = {u for t, u in live.execute(
            "SELECT table_name, row_uid FROM kanban_sync_outbox WHERE state IN ('pending', 'rejected')")
            if t == "task_chat_links"}
        held |= {r[0] for r in live.execute(
            "SELECT row_uid FROM kanban_sync_tombstones WHERE table_name = 'task_chat_links'")}
        now_ms = time.time_ns() // 1_000_000
        for uid, _session in lost:
            if uid in have or uid in held:
                continue
            live.execute(
                "INSERT INTO kanban_sync_outbox (op_id, table_name, row_uid, op, fields, base_version, hlc, created_at) "
                "VALUES (?, 'task_chat_links', ?, 'delete', '{}', NULL, '', ?)", (sync.uuid7(), uid, now_ms))
            live.execute("INSERT OR REPLACE INTO kanban_sync_tombstones (table_name, row_uid, deleted_at) "
                         "VALUES ('task_chat_links', ?, ?)", (uid, now_ms))
            queued += 1
    if queued:
        _log.warning("kanban sync: %d placement chat links absent from live but still on the service; "
                     "delete queued", queued)
    return queued


def repair_divergence(live: sqlite3.Connection, shadow: sqlite3.Connection, gateway: str) -> dict[str, list[str]]:
    from hermes_cli.kanban_sync_divergence import repair_divergence as repair
    return repair(live, shadow, gateway)


def _diverged_runs(live: sqlite3.Connection, shadow: sqlite3.Connection, tid: str) -> list[tuple[str, dict, Any]]:
    """``(uid, full row fields, Board version)`` of ``tid``'s run rows the service never received: same Board version on
    both sides (this gateway has applied every service write of the row), nothing in flight, and a synced field that
    differs. A run the service never saw at all is the install-time gap heal's (it has no uid there)."""
    held = {r[0] for r in live.execute(
        "SELECT DISTINCT row_uid FROM kanban_sync_outbox WHERE table_name = 'task_runs' "
        "AND state IN ('pending', 'rejected')")}
    out = []
    for row in live.execute("SELECT * FROM task_runs WHERE task_id = ? AND uid IS NOT NULL ORDER BY id", (tid,)):
        uid = row["uid"]
        if uid in held:
            continue
        theirs = shadow.execute("SELECT * FROM task_runs WHERE uid = ?", (uid,)).fetchone()
        if theirs is None or theirs["sync_version"] is None or theirs["sync_version"] != row["sync_version"]:
            continue
        mine, remote = _row_fields(live, "task_runs", row), _row_fields(shadow, "task_runs", theirs)
        if any(k in remote and remote[k] != v for k, v in mine.items()):
            out.append((uid, mine, row["sync_version"]))
    return out


# --- leases (primary boards) ----------------------------------------------------------------------

class HubLeaseClient:
    """:class:`kanban_db_sync.LeaseClient` over ``POST /v2/boards/<id>/leases``."""

    def __init__(self, hub: Hub) -> None:
        self.hub = hub

    def _lease(self, board_id: str, body: dict, run_uid: str, gateway_id: str) -> sync.Lease:
        try:
            res = self.hub.leases(board_id, {**body, "client_op_id": sync.uuid7()})
        except (HubError, OSError) as exc:
            raise sync.LeaseUnavailable(str(exc)) from exc
        lease = res.get("lease") or {}
        granted = res.get("status") == "applied" and lease.get("run_uid") == run_uid
        from hermes_cli.kanban_workspace_source import snapshot_from_row
        return sync.Lease(granted=granted, run_uid=run_uid, expires_ms=lease.get("expires"),
                          holder=lease.get("holder"), reason=None if granted else res.get("reason"),
                          version=res.get("version"), source_snapshot=snapshot_from_row(res.get("row"), grant={
                              k: res.get(k) for k in ("status", "lease", "source_predecessor")}),
                          spend_allowance=res.get("spend_allowance") if granted and lease.get("task_id") == body.get("task")
                          and lease.get("holder") == gateway_id and isinstance(res.get("spend_allowance"), dict) else None)

    def acquire(self, *, board_id: str, task_uid: str, run_uid: str, gateway_id: str, ttl_s: int, lane: str = "ready",
                source_protocol: Optional[int] = None, expected_reviewer_allowance: Optional[dict] = None) -> sync.Lease:
        return self._lease(board_id, {"action": "grant", "task": task_uid, "run_uid": run_uid, "ttl_s": ttl_s, "lane": lane,
                                      **({"source_protocol": source_protocol} if source_protocol else {}),
                                      "expected_reviewer_allowance": expected_reviewer_allowance}, run_uid, gateway_id)

    def renew(self, *, board_id: str, task_uid: str, run_uid: str, gateway_id: str, ttl_s: int) -> sync.Lease:
        return self._lease(board_id, {"action": "renew", "task": task_uid, "run_uid": run_uid, "ttl_s": ttl_s}, run_uid, gateway_id)

    def release(self, *, board_id: str, task_uid: str, run_uid: str, gateway_id: str,
                unused_grant: bool = False) -> None:
        # ``unused_grant``: released before anything started locally (the Board does not count it as a start).
        self._lease(board_id, {"action": "release", "task": task_uid, "run_uid": run_uid,
                               **({"unused_grant": True} if unused_grant else {})}, run_uid, gateway_id)
