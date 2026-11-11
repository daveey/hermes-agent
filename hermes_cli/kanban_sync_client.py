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

from hermes_cli import kanban_lockguard as _kanban_lockguard
import json
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

from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_timing as sync_timing  # off unless kanban.sync.diagnostics.timing_until arms it
from hermes_cli.kanban_sync_connections import connection, connection_scope, invalidate
from hermes_cli.kanban_sync_client_attempt import note_attempt, raise_bounded_error, replay_after_timeout

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


def _peer_closed(conn) -> bool:
    """True when an idle kept-alive socket is readable (the server sent FIN) or already gone."""
    import select
    sock = getattr(conn, "sock", None)
    if sock is None:
        return True
    try:  # poll, not select: select raises for fds >= 1024, which a busy gateway has
        if not hasattr(select, "poll"):  # Windows: select has no fd ceiling there
            return bool(select.select([sock], [], [], 0)[0])
        poller = select.poll()
        poller.register(sock, select.POLLIN | select.POLLPRI)
        return bool(poller.poll(0))
    except (OSError, ValueError):
        return True


class _DeadlineRequests(urllib.request.BaseHandler):
    """urllib invokes request processors again for each redirect, before sending."""

    def __init__(self, remaining: Callable[[float], float]) -> None:
        self.remaining = remaining

    def http_request(self, request):
        request.timeout = self.remaining(request.timeout)
        return request

    https_request = http_request


CPU_REPORT_BUDGET_S = 2.0
_CPU_FIELDS = ("cpu_busy_pct", "cpu_busy_window_ms", "cpu_busy_sample_end_ms")


def _bounded_call(ident: Identity, method: str, path: str, body: Optional[dict], budget_s: float) -> dict:
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

    worker = _hub_threading.Thread(target=run, daemon=True, name="kanban-bounded-call")
    worker.start()
    worker.join(max(0.0, deadline - time.monotonic()))
    if worker.is_alive():
        with expiry:
            box["expired"] = True
            if "conn" in box:
                cut(box["conn"])
        raise TimeoutError(f"Board request exceeded its {budget_s:g} s total budget")
    if "error" in box:
        raise_bounded_error(box["error"], deadline, time.monotonic(), budget_s)
    return box["result"]


class Hub:
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

    def _call(self, method: str, path: str, body: Optional[dict] = None, *, replay_safe: bool = False) -> dict:
        timeout = self._remaining()
        data = None if body is None else json.dumps(body).encode()
        parts = urllib.parse.urlsplit(self.ident.url)
        if parts.scheme not in ("http", "https") or not parts.hostname or self._proxied(parts):
            return self._call_urlopen(method, path, data, timeout)
        return self._call_kept_alive(parts, method, path, data, timeout, replay_safe=replay_safe)

    def _call_urlopen(self, method: str, path: str, data: Optional[bytes], timeout: Optional[float] = None) -> dict:
        timeout = self._remaining(timeout)
        req = urllib.request.Request(self.ident.url + path, data=data, method=method, headers=self._headers())
        # A per-call opener leaves global urllib users alone. Its processor also caps
        # urllib's internal redirect follows, not merely the initial fallback send.
        open_request = (urllib.request.build_opener(_DeadlineRequests(self._remaining)).open
                        if self.deadline is not None else urllib.request.urlopen)
        started = time.monotonic()
        try:
            try:
                with open_request(req, timeout=timeout) as resp:
                    return json.loads(resp.read() or b"{}")
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")[:500]
                if exc.code == 410:
                    raise Resync(text) from exc
                raise HubError(exc.code, text) from exc
        except Exception as exc:  # health: allow BLE001 -- stamps diagnostics, then re-raises the same error
            note_attempt(exc, self.deadline, started, time.monotonic())
            raise

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
                         timeout: Optional[float] = None, *, replay_safe: bool = False) -> dict:
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
                    raw = resp.read()
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
                    return self._call_urlopen(method, path, data, timeout)
                if 300 <= status < 400:  # never resend a POST the server may have applied
                    raise HubError(status, raw.decode("utf-8", "replace")[:500])
                if not 200 <= status < 300:
                    text = raw.decode("utf-8", "replace")[:500]
                    if status == 410:
                        raise Resync(text)
                    raise HubError(status, text)
                return json.loads(raw or b"{}")
            except Exception as exc:  # health: allow BLE001 -- stamps diagnostics, then re-raises the same error
                note_attempt(exc, self.deadline, started, time.monotonic())
                raise
        raise AssertionError("unreachable")  # pragma: no cover

    def ops(self, board: str, ops: list[dict]) -> dict:
        return self._call("POST", f"/v2/boards/{board}/ops", {"ops": ops})

    def changes(self, board: str, since: int, limit: int = FEED_PAGE) -> dict:
        return self._call("GET", f"/v2/boards/{board}/changes?since={int(since)}&limit={int(limit)}")

    def snapshot_page(self, board: str, table: str, after: str = "", limit: int = SNAPSHOT_PAGE) -> dict:
        from urllib.parse import quote
        return self._call("GET", f"/v2/boards/{board}/snapshot?table={table}&after={quote(after)}&limit={limit}")

    def row_evidence(self, board: str, table: str, uid: str) -> dict:
        """Exact authoritative row/deletion/history evidence, never a replica lookup."""
        from urllib.parse import urlencode
        return self._call("GET", f"/v2/boards/{board}/evidence?" + urlencode({"table": table, "uid": uid}))

    def leases(self, board: str, body: dict) -> dict:
        return self._call("POST", f"/v2/boards/{board}/leases", body)

    def repin(self, board: str, *, task_id: str, expected_revision: int, runs_on: list[str]) -> dict:
        """Explicit rollback/manual pin; caller must pull the authoritative row afterward."""
        return self._call("POST", f"/v2/boards/{board}/placement/repin", {
            "task_id": task_id, "expected_revision": expected_revision, "runs_on": runs_on})

    def reserve(self, board: str, *, task_id: str, expected_revision: int, candidates: list[str]) -> dict:
        """Atomic Board placement; errors leave the persisted local queue fence intact."""
        return self._call("POST", f"/v2/boards/{board}/placement/reserve", {
            "task_id": task_id, "expected_revision": expected_revision,
            "policy": "worker-first-v1", "candidates": candidates}, replay_safe=True)

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

    def machines(self) -> dict:
        """The machine registry (RFC "One machine registry"): ``{me, machines: [{name, labels, pinned_only, ...}]}``."""
        return self._call("GET", "/v2/machines")

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

    def report_pickup(self, pickup: Optional[str]) -> dict:
        """Publish this machine's ``kanban.sync.pickup`` (None → null) on its own registry row."""
        return self._call("POST", "/v2/machines/me", {"pickup": pickup})


# --- boards ---------------------------------------------------------------------------------------

def board_paths() -> list[tuple[str, Path]]:
    """``(slug, kanban.db)`` for every board on this host that ``kanban.sync.boards`` selects."""
    from hermes_cli import kanban_db, kanban_db_boards
    root = kanban_db.kanban_home()
    found = [("default", root / "kanban.db")] if (root / "kanban.db").exists() else []
    # The shared real-board enumerator: ``_archived/`` and other non-slug folders are not boards.
    found += [(slug, p) for slug, p in kanban_db_boards.named_board_db_files() if BOARD_ID_RE.match(slug)]
    local = {slug for slug, _ in found}
    links_dir = root / "kanban" / sync.LINKS_DIR
    if links_dir.is_dir():  # boards only another gateway has, where this gateway's chats have cards
        found += [(p.stem, p) for p in sorted(links_dir.glob("*.db")) if BOARD_ID_RE.match(p.stem) and p.stem not in local]
    config = sync.sync_config()  # [daveey] once, not per board: every Desktop session-list poll lands here
    return [(slug, path) for slug, path in found if sync.board_selected(str(path), config)]


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

def _wire_fields(table: str, fields: dict) -> dict:
    """Only the synced wire columns (a column a later migration added is not sent until it is declared)."""
    allowed = {sync._wire_name(table, c) for c in sync.SYNCED_COLUMNS[table]}
    return {k: v for k, v in fields.items() if k in allowed}


def translate(row: dict, hlc: str) -> dict:
    """One outbox row -> one row op."""
    op = {"client_op_id": row["op_id"], "table": row["table_name"], "uid": row["row_uid"], "hlc": hlc}
    if row["op"] == "delete":
        return {**op, "op": "delete"}
    fields = row["fields"] if isinstance(row["fields"], dict) else json.loads(row["fields"])
    out = {**op, "op": "upsert", "fields": _wire_fields(row["table_name"], fields)}
    if row["op"] == "import" and row["table_name"] == "tasks":
        out["import"] = True
    return out


def _full_row_if_unseen(conn: sqlite3.Connection, row: dict) -> dict:
    """A task edit captured before the service has acknowledged the card (``sync_version`` NULL: it was created
    before sync was switched on and not imported yet) carries only the changed columns, which the service cannot
    insert. Send the card's full current row instead; a later edit is then a plain merge.

    Child rows (runs, comments, events, attachments, chat links) are small and always go whole: an update captured
    for a row whose insert the service refused earlier (a deploy still rolling out) would otherwise reach it with no
    ``task_id`` and be refused ``not_found``."""
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
    return {**row, "fields": _row_fields(conn, "tasks", task)}


# Rejections the service lifts on its own; anything else is final. A capped number of retries keeps a real mismatch
# (an old service that will never know the table) visible as rejected instead of retrying forever.
TRANSIENT_REASONS = ("storage_error", "depends_on_rejected")
# Backed off 2 s doubling to 5 min: 20 attempts span about an hour, longer than a Durable Object takes to pick up
# a deploy (observed: minutes to an hour on a busy board).
MAX_TRANSIENT_ATTEMPTS = 20


def _backoff_s(attempts: int) -> int:
    return min(2 ** (attempts + 1), 300)


def transient_rejection(res: dict) -> bool:
    reason = str(res.get("reason") or "")
    detail = str(res.get("detail") or "")
    return (reason in TRANSIENT_REASONS or (reason == "invalid" and "unknown table" in detail)
            or (reason == "not_found" and detail.startswith(("no task", "unknown task"))))  # card goes first next round


def _unacked_parents(conn: sqlite3.Connection, rows: list[dict], gateway: str) -> list[tuple[dict, str]]:
    """Full task upserts for cards that child rows in this batch belong to but the service has not acknowledged
    (``sync_version`` NULL), sent ahead of them: a child captured before its card was queued (a board attached
    while busy, then imported) would otherwise be refused ``not_found`` and hold the card back behind it."""
    if not any(r["table_name"] != "tasks" for r in rows):
        return []
    in_batch = {r["row_uid"] for r in rows if r["table_name"] == "tasks"}
    wanted: list[str] = []
    for r in rows:
        if r["table_name"] == "tasks":
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
        op = {"client_op_id": sync.uuid7(), "table": "tasks", "uid": tid, "op": "upsert",
              "fields": _wire_fields("tasks", _row_fields(conn, "tasks", task)), "hlc": _next_hlc(conn, gateway)}
        out.append((op, tid))
    return out


def _record_live_claim_rejection(conn: sqlite3.Connection, row: dict, reason: str) -> None:
    """A card write the Board refused because another gateway holds the card's live lease leaves a
    ``sync_rejected`` event on the card (inside the push txn), so ``kanban show`` says the write never happened
    instead of the card silently reverting to the service's row. [daveey]"""
    from hermes_cli import kanban_db as kb
    fields = row["fields"] if isinstance(row["fields"], dict) else json.loads(row["fields"] or "{}")
    if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (row["row_uid"],)).fetchone() is None:
        return
    kb._append_event(conn, row["row_uid"], "sync_rejected",
                     {"reason": reason[:300], "fields": sorted(fields), "op_id": row["op_id"]})


@dataclass
class PushResult:
    sent: int = 0
    applied: int = 0
    merged: int = 0
    rejected: int = 0
    deferred: int = 0


def push(conn: sqlite3.Connection, hub: Hub, board: str, gateway: str, *, primary: bool,
         max_batches: int = MAX_BATCHES_PER_ROUND) -> PushResult:
    """Drain pending outbox rows in FIFO batches. A transport failure leaves them pending (retried next round)."""
    from hermes_cli.kanban_db_connect import write_txn
    refuse_if_schema_ahead(conn)  # an older core would strip the newer columns; the ops stay pending [daveey]
    out = PushResult()
    for _ in range(max_batches):
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM kanban_sync_outbox WHERE state = 'pending' AND (retry_after IS NULL OR retry_after <= ?) "
            "ORDER BY created_at, rowid LIMIT ?", (int(time.time()), PUSH_BATCH))]
        if not rows:
            break
        with write_txn(conn):
            wire = [translate(_full_row_if_unseen(conn, r), _next_hlc(conn, gateway)) for r in rows]
            parents = _unacked_parents(conn, rows, gateway)
            wire = [op for op, _tid in parents] + wire
        try:
            resp = hub.ops(board, wire)
        except (HubError, OSError) as exc:
            with write_txn(conn):
                conn.executemany("UPDATE kanban_sync_outbox SET attempts = attempts + 1, last_error = ? WHERE op_id = ?",
                                 [(str(exc)[:300], r["op_id"]) for r in rows])
            raise
        out.sent += len(rows)
        authoritative: list[sync.RemoteChange] = []
        parent_ops = {op["client_op_id"]: tid for op, tid in parents}
        with write_txn(conn):
            for res in resp.get("results", []):
                status, op_id = res.get("status"), res.get("client_op_id")
                if op_id in parent_ops:
                    if status in ("applied", "merged") and res.get("version"):
                        conn.execute("UPDATE tasks SET sync_version = MAX(COALESCE(sync_version, 0), ?) WHERE id = ?",
                                     (res["version"], parent_ops[op_id]))
                    continue
                row = next((r for r in rows if r["op_id"] == op_id), None)
                if row is None:
                    continue
                if status in ("applied", "merged"):
                    conn.execute("DELETE FROM kanban_sync_outbox WHERE op_id = ?", (op_id,))
                    if row["table_name"] == "tasks" and res.get("version"):
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
                    conn.execute("UPDATE kanban_sync_outbox SET op_id = ?, attempts = attempts + 1, last_error = ?, "
                                 "retry_after = ? WHERE op_id = ?",
                                 (sync.uuid7(), f"retrying: {reason}"[:500], int(time.time()) + _backoff_s(row["attempts"]), op_id))
                    out.deferred += 1
                else:
                    reason = f"{res.get('reason')}: {res.get('detail') or ''}".strip(": ")
                    conn.execute("UPDATE kanban_sync_outbox SET state = 'rejected', last_error = ?, attempts = attempts + 1 "
                                 "WHERE op_id = ?", (reason[:500], op_id))
                    out.rejected += 1
                    _log.warning("kanban sync: %s %s %s rejected (%s)", board, row["table_name"], row["row_uid"], reason)
                    if row["table_name"] == "tasks" and res.get("reason") == "live_claim":
                        _record_live_claim_rejection(conn, row, reason)
                    if primary and res.get("row"):
                        authoritative.append(_row_change(row["table_name"], res["row"]))
        if authoritative:
            cursor = (sync.sync_state(conn) or {}).get("cursor", 0)
            sync.apply_remote_changes(conn, authoritative, new_cursor=cursor, board=board)
        if out.deferred or len(rows) < PUSH_BATCH:  # deferred rows retry next round, not in a hot loop
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


def pending_task_fields(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Wire fields per task with a local edit still in the outbox (not rejected)."""
    out: dict[str, set[str]] = {}
    for uid, fields in conn.execute("SELECT row_uid, fields FROM kanban_sync_outbox "
                                    "WHERE table_name = 'tasks' AND state != 'rejected'"):
        out.setdefault(uid, set()).update(json.loads(fields))
    return out


def without_pending(changes: list[sync.RemoteChange], pending: dict[str, set[str]]) -> list[sync.RemoteChange]:
    """A field with an unanswered local edit keeps the local value until that edit's answer arrives
    (RFC "Offline queue and reconnect": pull first, local-pending fields win locally)."""
    out = []
    for ch in changes:
        keep = pending.get(ch.uid) if ch.table == "tasks" and ch.op == "upsert" else None
        if keep:
            ch = sync.RemoteChange(ch.seq, ch.table, ch.uid, ch.op, {k: v for k, v in ch.fields.items() if k not in keep},
                                   ch.version, ch.hlc, ch.origin_gateway, ch.client_op_id)
        out.append(ch)
    return out


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
         partition: Optional[str] = None) -> int:
    """Apply every feed page after this file's cursor. Returns the number of changes read. On a board with an
    outbox (primary), fields with an unanswered local edit are not overwritten; ``partition`` (a gateway name)
    keeps only that gateway's cards."""
    from hermes_cli.kanban_sync_stop import checkpoint
    total = 0
    cursor = int((sync.sync_state(conn) or {}).get("cursor") or 0)
    while True:
        checkpoint()
        page = hub.changes(board, cursor)
        checkpoint()
        changes = [c for c in (adapt(ch) for ch in page.get("changes", [])) if c is not None]
        if partition is not None:
            changes = own_partition(conn, changes, partition)
        changes = without_pending(changes, pending_task_fields(conn))
        nxt = int(page.get("next", cursor))
        if not page.get("changes") and nxt == cursor and not page.get("more") and not _applying(conn):
            # An idle poll: no change, same cursor. The apply would only rewrite ``applying`` and ``cursor`` to
            # the values they hold, a write transaction (WAL frame, fsync, observer wake) every round on every
            # board for nothing. [daveey]
            return total
        # [daveey] The pre-filter above can miss a local edit committed after it read the outbox (a claim in
        # flight): ``local_pending_wins`` checks again inside the apply transaction.
        sync.apply_remote_changes(conn, changes, new_cursor=nxt, board=board, observers=observers,
                                  local_pending_wins=True)
        total += len(page.get("changes", []))
        cursor = nxt
        if not page.get("more"):
            return total


def _applying(conn: sqlite3.Connection) -> bool:
    return bool((sync.sync_state(conn) or {}).get("applying"))


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
            sync.apply_remote_changes(conn, changes, new_cursor=cursor, board=board, observers=observers,
                                      local_pending_wins=True)  # [daveey] an unpushed local edit outlives a rebuild
            count += len(rows)
            if not page.get("more"):
                break
            after = str(page["next"])
    from hermes_cli.kanban_db_connect import write_txn
    with write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET cursor = ?", (int(head or 0),))
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
    repair_deferred: bool = False  # a replica-only change waits for the repair window (see ``_defer_repair``)


def open_shadow(slug: str, gateway: str) -> sqlite3.Connection:
    path = shadow_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as cleanup:
        conn = _connect(path)
        cleanup.callback(conn.close)
        ensure_identity(conn, slug, gateway)
        cleanup.pop_all()
        return conn


@contextmanager
def _pulled_replica(slug: str, hub: Hub, gateway: str):
    """Borrow the pulled replica for this round; retention rebuilds discard the old handle."""
    path = shadow_path(slug)
    with ExitStack() as stack:
        replica = stack.enter_context(connection(path, lambda: open_shadow(slug, gateway)))
        rebuilt = False
        try:
            pulled = pull(replica, hub, slug, observers=False)
        except Resync:
            _log.warning("kanban sync: %s replica cursor below the retention floor; rebuilding from a snapshot", slug)
            stack.close()
            invalidate(path)  # must close the pooled descriptor BEFORE unlinking its database
            path.unlink(missing_ok=True)
            replica = stack.enter_context(connection(path, lambda: open_shadow(slug, gateway)))
            rebuilt = True
            pulled = resync(replica, hub, slug, observers=False)
        try:  # readers derive the replica's staleness from this (kanban_chat_links); a failed stamp only ages it
            sync.mark_pulled(replica)
        except sqlite3.Error as exc:
            _log.warning("kanban sync: %s replica pull not stamped (%s)", slug, exc)
        yield replica, pulled, rebuilt


def _pull_replica(slug: str, hub: Hub, gateway: str) -> int:
    """Pull the whole replica for standalone callers that do not run repairs."""
    with _pulled_replica(slug, hub, gateway) as (_, pulled, _rebuilt):
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
    with connection(path, lambda: _connect(path)) as live:
        mode = sync.board_mode(live)
        result = RoundResult(board=slug, mode=mode)
        if mode == "off":
            return result
        refuse_if_schema_ahead(live)  # before pull/repair: an older core must not act on a newer board [daveey]
        ensure_identity(live, slug, gateway)
        with _pulled_replica(slug, hub, gateway) as (replica, pulled, rebuilt):
            result.pulled = pulled
            if mode == "primary":
                try:
                    pull(live, hub, slug, observers=True, partition=gateway)
                except Resync:
                    _log.warning("kanban sync: %s live cursor below the retention floor; rebuilding from a snapshot", slug)
                    resync(live, hub, slug, observers=True, partition=gateway)
            from hermes_cli.kanban_sync_repair_revision import ensure_revision
            ensure_revision(live)
            inputs = _repair_inputs(path, replica, mode, gateway)
            key = str(path)
            if inputs is None or _REPAIRED.get(key) != inputs:
                deferred = not rebuilt and _defer_repair(key, inputs)
                from hermes_cli.kanban_sync_partition_repair import repair_partition_chat_links, repair_partition_links
                if not deferred:
                    repair_partition_links(live, replica, gateway)
                    repair_partition_chat_links(live, replica, gateway)
                # The remote-parent mirror is cheap and gates child promotion, so a deferral never delays it.
                if mode == "primary" and sync.refresh_remote_parents(live, replica):
                    sync.wake_dispatcher()  # a foreign parent moved: re-gate its children now
                if deferred:
                    result.repair_deferred = True
                else:
                    from hermes_cli.kanban_sync_repair_content import run
                    result.repaired = run(live, replica, gateway, path=path, inputs=inputs, rebuilt=rebuilt)
                    retire_lost_placeholder_links(live, replica, gateway)
                    if inputs is not None:
                        _REPAIRED[key] = inputs
                        _REPAIRED_AT[key] = _repair_clock()
            if mode == "primary":
                heal_clobbered_completions(live)
        result.push = push(live, hub, slug, gateway, primary=mode == "primary")
        from hermes_cli.kanban_sync_rejections import resolve_rejections
        result.rejection_resolutions = resolve_rejections(live, hub, slug)
        if mode == "primary":
            from hermes_cli.kanban_worker_placement import reevaluate
            reevaluate(live, hub, slug, gateway)
        return result


_UNPUBLISHED = object()
_PICKUP_STATE: dict[str, Any] = {"value": _UNPUBLISHED, "retry_at": 0.0}
_PICKUP_UNSUPPORTED_S = 600.0


def reset_published_pickup() -> None:
    """Tests: forget the last posted pickup so the next round posts again."""
    _PICKUP_STATE["value"] = _UNPUBLISHED
    _PICKUP_STATE["retry_at"] = 0.0


def publish_pickup_if_changed(hub: Any, *, now: Optional[float] = None) -> bool:
    """Post :func:`hermes_cli.kanban_worker_node.pickup_mode` (None → null) once per sync round, and only when it
    changed. A failed post is retried next round. An older Board (404/405) stays quiet for 10 minutes. Never
    raises into the sync loop. A hub without ``report_pickup`` (test fakes) is a no-op."""
    from hermes_cli.kanban_worker_node import pickup_mode
    mode = pickup_mode()
    if _PICKUP_STATE["value"] == mode:
        return False
    clock = time.monotonic() if now is None else now
    if clock < float(_PICKUP_STATE["retry_at"]):
        return False
    poster = getattr(hub, "report_pickup", None)
    if poster is None:
        return False
    try:
        poster(mode)
    except HubError as exc:
        if exc.status in (404, 405):
            _PICKUP_STATE["retry_at"] = clock + _PICKUP_UNSUPPORTED_S
            _log.info("kanban sync: board has no pickup route yet (%s); retrying later", exc.status)
        else:
            _log.warning("kanban sync: pickup publish failed (%s)", exc)
        return False
    except OSError as exc:
        _log.warning("kanban sync: pickup publish failed (%s)", exc)
        return False
    _PICKUP_STATE["value"] = mode
    _PICKUP_STATE["retry_at"] = 0.0
    return True


def sync_all_once(hub: Optional[Hub] = None, *,
                  boards: Optional[list[tuple[str, Path]]] = None) -> list[RoundResult]:
    from hermes_cli.kanban_sync_guard import SyncRoundBusy

    ident = load_identity() if hub is None else hub.ident
    hub = hub or Hub(ident)
    publish_pickup_if_changed(hub)
    results = []
    for slug, path in board_paths() if boards is None else boards:
        mark = sync_timing.board_start(slug, results)
        try:
            results.append(sync_board(slug, path, hub, ident.gateway))
        except SchemaAhead as exc:
            _log.error("kanban sync: %s not synced: %s", slug, exc)
            results.append(RoundResult(board=slug, mode="?", error=f"SchemaAhead: {exc}"))
        except (HubError, OSError, Resync, sqlite3.Error, SyncUnavailable, SyncRoundBusy) as exc:
            _log.warning("kanban sync: round for %s failed (%s: %s)", slug, type(exc).__name__, exc)
            results.append(RoundResult(board=slug, mode="?", error=f"{type(exc).__name__}: {exc}"))
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
# Machine-session sources come from state.db (``kanban_db_notify._local_session_sources``), which is not in a links
# pass's key: a missing session is re-read after its 300 s TTL. An unchanged board is still reconciled this long after
# its last pass. A shared-cache refresh can add this interval after the missing-source TTL; content changes
# still reconcile immediately on the next links pass, and notification delivery checks sources independently. [daveey]
LINKS_MAX_AGE_S = 300.0
# Per board file: (inputs, started at) of the last completed links pass (see ``_links_inputs``).
_LINKS_DONE: dict[str, tuple[tuple, float]] = {}


def _links_clock() -> float:
    return time.monotonic()


def _ledger_slices(cards: Iterable[Mapping[str, Any]], gateway: str) -> dict[str, frozenset]:
    """Per board, every field of this gateway's origin-ledger rows that a links pass reads (``ledger_boards``,
    ``kanban_chat_links._ledger_links``). Compared by value: a re-projection with the same rows is no change."""
    out: dict[str, set] = {}
    for c in cards:
        if not c.get("board") or c.get("origin_gateway") not in (None, gateway):
            continue
        out.setdefault(str(c["board"]), set()).add(
            (c.get("id"), c.get("session"), c.get("via"), c.get("origin_profile"), bool(c.get("released"))))
    return {board: frozenset(rows) for board, rows in out.items()}


def _links_signature(gateway: str, placement: Mapping[str, Any]) -> Optional[tuple]:
    """The configuration a links pass reads besides its board: gateway names, profile, homes, placement config."""
    try:
        from hermes_constants import get_hermes_home
        from hermes_cli.kanban_origin import gateway_names
        from hermes_cli.profiles import get_active_profile_name
        return (gateway, gateway_names()[0], get_active_profile_name(), str(get_hermes_home()),
                json.dumps(placement, sort_keys=True, default=str))
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None


def _links_inputs(conn: sqlite3.Connection, path: Path, slug: str, gateway: str, mode: str,
                  ledger: Optional[dict[str, frozenset]], signature: Optional[tuple]) -> Optional[tuple]:
    """Everything one board's links pass (``kanban_chat_links.reconcile`` with its creator-chats backfill) reads,
    besides state.db (``LINKS_MAX_AGE_S``): the live file's identity and transactional activity revision (tasks,
    events, subscriptions, links; trigger-backed, so dispatcher clocks and sync stamps do not move it), the replica's
    identity and feed cursor (its only writer besides the ``pulled_at`` stamp is the pull), this gateway's ledger rows
    for the board, the mode and the configuration. Read before the pass, so a commit during it re-arms the next one.
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
                replica = (rst.st_dev, rst.st_ino, int((sync.sync_state(replica_conn) or {}).get("cursor") or 0))
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    return (st.st_dev, st.st_ino), revision, replica, ledger.get(slug, frozenset()), mode, signature


def reconcile_links(boards: list[tuple[str, Path]], gateway: str, *, skip_unchanged: bool = False) -> None:
    """Bring this gateway's chat links up to date in every synced board (kanban_chat_links.reconcile), creating a
    link-only board for each board this gateway's chats have cards on but this host does not have.

    ``skip_unchanged`` (the sync loop): a board whose links-pass inputs (``_links_inputs``) equal those of its last
    completed pass, less than ``LINKS_MAX_AGE_S`` ago, is not reconciled again. A failed pass records nothing, so the
    next one retries. Explicit callers (``hermes kanban sync links``) always reconcile. Boards are borrowed through
    the loop's connection scope (``kanban_sync_connections``). [daveey]"""
    from hermes_cli import kanban_chat_links
    from hermes_cli.kanban_placement import config_snapshot
    with config_snapshot() as placement:
        ledger: Optional[dict[str, frozenset]] = None
        try:
            from hermes_cli.kanban_session_activity import _remote_links
            ledger = _ledger_slices(_remote_links(), gateway)
            local = {slug for slug, _path in boards}
            if not skip_unchanged or set(ledger) - local:
                created = ensure_link_boards(set(ledger), gateway)
                if created:
                    _log.info("kanban sync: link-only boards %s", created)
                    boards = board_paths()
        except Exception as exc:  # health: allow BLE001 -- link-only boards are best effort; local links still reconcile
            _log.warning("kanban sync: link-only boards failed (%s: %s)", type(exc).__name__, exc)
        signature = _links_signature(gateway, placement) if skip_unchanged else None
        for slug, path in boards:
            key = str(path)
            try:
                with connection(path, lambda: _connect(path)) as conn:
                    mode = sync.board_mode(conn)
                    if mode == "off":
                        continue
                    inputs, started = None, 0.0
                    if skip_unchanged:
                        inputs = _links_inputs(conn, path, slug, gateway, mode, ledger, signature)
                        done = _LINKS_DONE.pop(key, None)
                        started = _links_clock()
                        if inputs is not None and done is not None and done[0] == inputs \
                                and started - done[1] < LINKS_MAX_AGE_S:
                            _LINKS_DONE[key] = done
                            continue
                    counts = kanban_chat_links.reconcile(conn, slug, gateway)
                    if inputs is not None:
                        _LINKS_DONE[key] = (inputs, started)
                if any(counts.values()):
                    _log.info("kanban sync: %s chat links %s", slug, counts)
            except Exception as exc:  # health: allow BLE001 -- one board's links must not stop the sync loop
                _log.warning("kanban sync: chat links for %s failed (%s: %s)", slug, type(exc).__name__, exc)


class _BoardWatch:
    """One long-lived read-only connection per board file, for the sync loop's cheap questions.

    ``outbox_pending`` runs every 0.5 s on every synced board. Opening a connection per call re-read the schema
    each time (about 13 ms of CPU per pass over 10 boards, 2.5% of a core in the gateway, 2026-10-31). A kept
    connection answers the same indexed query in microseconds. Repair scans use a transactional
    input-table revision rather than data_version: dispatcher-clock commits must not invalidate them.
    A file replaced on disk (new inode) is reopened with a new generation, so a restored revision
    never equals a cached key for the old file. [daveey]"""

    MAX = 64

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conns: dict[str, tuple[tuple[int, int], sqlite3.Connection, int]] = {}
        self._gen = 0
        self._answers: dict[str, tuple[int, str, Any]] = {}

    def _conn(self, path: Path) -> Optional[tuple[sqlite3.Connection, int]]:
        key = os.path.realpath(path)
        try:
            st = os.stat(key)
        except OSError:
            self._drop(key)
            return None
        ent = self._conns.get(key)
        if ent is not None and ent[0] == (st.st_dev, st.st_ino):
            return ent[1], ent[2]
        self._drop(key)
        if len(self._conns) >= self.MAX:
            self._drop(next(iter(self._conns)))
        conn = _kanban_lockguard.connect(f"{Path(key).as_uri()}?mode=ro", uri=True, timeout=1,
                                         check_same_thread=False)
        self._gen += 1
        self._conns[key] = ((st.st_dev, st.st_ino), conn, self._gen)
        return conn, self._gen

    def _drop(self, key: str) -> None:
        self._answers.pop(key, None)
        ent = self._conns.pop(key, None)
        if ent is not None:
            try:
                ent[1].close()
            except sqlite3.Error:
                pass

    def query(self, path: Path, sql: str, *, unchanged_ok: bool = False) -> Optional[tuple[int, Any]]:
        """(generation, first row) or None when the file cannot be read."""
        with self._lock:
            try:
                got = self._conn(path)
                if got is None:
                    return None
                conn, gen = got
                key = os.path.realpath(path)
                version = conn.execute("PRAGMA data_version").fetchone()[0] if unchanged_ok else None
                cached = self._answers.get(key)
                if unchanged_ok and cached is not None and cached[:2] == (version, sql):
                    return gen, cached[2]
                answer = conn.execute(sql).fetchone()
                if unchanged_ok:
                    # Version is read BEFORE the query: a concurrent commit may cause
                    # one extra read, but cannot publish a stale answer as current.
                    self._answers[key] = (version, sql, answer)
                return gen, answer
            except sqlite3.Error:
                self._drop(os.path.realpath(path))
                return None

    def reset(self) -> None:
        with self._lock:
            for key in list(self._conns):
                self._drop(key)


_WATCH = _BoardWatch()
# Per board file: the inputs the last completed repair pass saw (see ``_repair_inputs``).
_REPAIRED: dict[str, tuple] = {}
# Per board file: when that pass finished (``_repair_clock``), and when a deferred pass is due.
_REPAIRED_AT: dict[str, float] = {}
_REPAIR_DEFERRED: dict[str, float] = {}
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
    reset()
    _WATCH.reset()
    _REPAIRED.clear()
    _REPAIRED_AT.clear()
    _REPAIR_DEFERRED.clear()
    _LINKS_DONE.clear()


def _defer_repair(key: str, inputs: Optional[tuple]) -> bool:
    """True when the only repair input that changed since the last completed pass is the replica's feed cursor
    (same replica file, live revision, schema, mode, gateway and placement) and that pass finished less than
    ``REPLICA_REPAIR_MIN_S`` ago; records when the deferred pass is due so the sync loop runs it then. Unknown
    inputs (None) or no completed pass never defer. A primary board's own cards are applied by its pull before
    the inputs are read, so a remote change to them arrives as a live revision change, not a deferral."""
    last = _REPAIRED.get(key)
    if inputs is None or last is None:
        return False
    live_version, (dev, ino, _cursor), *rest = inputs
    last_version, (last_dev, last_ino, _last_cursor), *last_rest = last
    if live_version != last_version or (dev, ino) != (last_dev, last_ino) or rest != last_rest:
        return False
    due = _REPAIRED_AT.get(key, float("-inf")) + REPLICA_REPAIR_MIN_S
    if _repair_clock() >= due:
        return False
    _REPAIR_DEFERRED[key] = due
    return True


def deferred_repairs_due(boards: list[tuple[str, Path]], now: float) -> list[tuple[str, Path]]:
    """Boards whose deferred repair pass is due at ``now`` (``_repair_clock`` time)."""
    return [(slug, path) for slug, path in boards
            if (due := _REPAIR_DEFERRED.get(str(path))) is not None and now >= due]


def next_deferred_repair(boards: list[tuple[str, Path]]) -> Optional[float]:
    """The earliest due time (``_repair_clock``) of a deferred repair pass among ``boards``, or None."""
    dues = [due for _slug, path in boards if (due := _REPAIR_DEFERRED.get(str(path))) is not None]
    return min(dues) if dues else None


def _repair_inputs(live_path: Path, replica: sqlite3.Connection, mode: str, gateway: str) -> Optional[tuple]:
    """Everything the pure repair scans (partition links, remote parents, divergence, missing children, lost
    placeholder links) read: a transactional revision of the live input tables, schema version,
    replica identity/feed cursor, mode, gateway and placement config. Dispatcher clock writes are
    deliberately not inputs. Read before the scans so concurrent relevant commits re-arm the next
    round. Replica writes are its own pull (cursor moves or rebuild replaces the file) and the
    pulled_at stamp, which the scans never read. None (cannot tell) always scans. [daveey]"""
    got = _WATCH.query(live_path, "SELECT revision, schema_version FROM kanban_repair_revision, "
                      "pragma_schema_version WHERE id = 1")
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
        if got is not None and got[1]:
            return True
    return False


def wait_for_work(wake: threading.Event, paths: Iterable[Path], period: float,
                  keep_running: Callable[[], bool], tick: float = 0.5) -> str:
    """Block until there is sync work: a Board change frame (``wake``), a local change in an outbox, or the poll
    period ran out. Returns which, so a local write is pushed within ``tick`` and a remote one pulled at once."""
    paths = list(paths)
    deadline = time.monotonic() + period
    while keep_running():
        if wake.wait(timeout=max(0.0, min(tick, deadline - time.monotonic()))):
            wake.clear()
            return "remote"
        if outbox_pending(paths):
            return "local"
        if time.monotonic() >= deadline:
            return "period"
    return "stopped"


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
    ahead = _schema_ahead_boards(board_paths())
    if ahead:
        runstate.record_down("not starting: " + "; ".join(f"board {slug}: {why}" for slug, why in ahead))
        return  # give the host sync role up: a current core's gateway can take it [daveey]
    hub = Hub(ident)
    sync.set_lease_client(HubLeaseClient(hub))
    period = float(interval or sync.sync_config().get("interval_seconds") or DEFAULT_INTERVAL_S)
    _log.info("kanban sync: running as gateway %s against %s (feed sockets; poll every %.1fs)", ident.gateway, ident.url, period)
    wake = threading.Event()
    from hermes_cli.kanban_sync_work import SyncWork
    work = SyncWork(period)
    watchers: dict[str, threading.Thread] = {}
    next_links = 0.0
    from hermes_cli.machine_metrics import Reporter
    metrics = Reporter(hub.report_metrics)
    while keep_running():
        boards = board_paths()
        for slug, _path in boards:
            if slug not in watchers or not watchers[slug].is_alive():
                watcher = FeedWatcher(ident, slug, wake, keep_running, on_change=work.changed,
                                      on_state=work.feed_state)
                watchers[slug] = threading.Thread(target=watcher.run, daemon=True, name=f"kanban-feed-{slug}")
                watchers[slug].start()
        selected = sync_timing.select(work.select, boards, time.monotonic(), outbox_pending)
        selected += [b for b in deferred_repairs_due(boards, _repair_clock()) if b not in selected]
        results = sync_all_once(hub, boards=selected)
        ahead = [r for r in results if (r.error or "").startswith("SchemaAhead")]
        if ahead:
            _log.error("kanban sync: a newer core migrated this host's board; giving up the sync role")
            runstate.record_down("gave up the sync role: " + "; ".join(f"board {r.board}: {r.error}" for r in ahead))
            return
        runstate.clear()  # a completed round: the loop runs
        for r in results:
            work.finished(r.board, not r.error)
            if r.push and (r.push.sent or r.push.rejected):
                _log.info("kanban sync: %s %s pulled=%d pushed=%d rejected=%d", r.board, r.mode, r.pulled,
                          r.push.sent, r.push.rejected)
        if time.monotonic() >= next_links:
            reconcile_links(boards, ident.gateway, skip_unchanged=True)
            next_links = time.monotonic() + LINKS_EVERY_S
        sync_timing.report(metrics.report_due)
        remaining = max(0.0, work.deadline - time.monotonic())
        deferred_due = next_deferred_repair(boards)
        if deferred_due is not None:
            remaining = min(remaining, max(0.0, deferred_due - _repair_clock()))
        sync_timing.wait(wait_for_work, wake, [p for _s, p in boards], remaining, keep_running)
    runstate.record_down("stopped (gateway stopping, or the host sync lease went to another gateway)")


def _schema_ahead_boards(boards: list[tuple[str, Path]]) -> list[tuple[str, str]]:
    """``(slug, refusal)`` per board a newer core migrated (logged at ERROR). An unreadable board is left to the
    round's own errors."""
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
        # Rows the service refused before the import (edits of cards it had not seen) are superseded by the full rows.
        counts["cleared_rejected"] = (0 if only is not None else
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
        _pull_replica(slug, hub, ident.gateway)
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
                if mode == "primary":
                    candidates = [r for r in rows if str(r["uid"]) not in live_uids[table]]
                    # Rows written locally since live_uids was read reached the service meanwhile: re-check.
                    now_live = _live_uids(live, table) if candidates else set()
                    lost = own_partition(live, [_row_change(table, r) for r in candidates
                                                if str(r["uid"]) not in now_live
                                                and (table, str(r["uid"])) not in held_back], ident.gateway)
                    if lost:
                        cursor = int((sync.sync_state(live) or {}).get("cursor") or 0)
                        sync.apply_remote_changes(live, lost, new_cursor=cursor, board=slug, observers=False)
                        restored[table] = restored.get(table, 0) + len(lost)
                if not page.get("more"):
                    break
                after = str(page["next"])
        missing = {table: live_uids[table] - service_uids[table] - tombstoned.get(table, set())
                   for table in IMPORT_ORDER}
        queued = import_board(live, only=missing)
        queued.pop("cleared_rejected", None)
        pushed = push(live, hub, slug, ident.gateway, primary=mode == "primary")
        _pull_replica(slug, hub, ident.gateway)
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
    out = {"mode": mode, "board_id": st.get("board_id"), "gateway": st.get("gateway_id"),
           "cursor": st.get("cursor"), "pending": counts.get("pending", 0), "rejected": counts.get("rejected", 0),
           "last_error": last_error}
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
    """This gateway's live cards against the shadow copy of the service. Rows with an unanswered local edit are
    reported as pending, not as differences; rejected outbox rows are differences until fixed."""
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
    rejected = live.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE state = 'rejected'").fetchone()[0]
    clean = not (missing or extra or fields or children or rejected)
    return {"clean": clean, "checked": len(checked), "pending": len(pending_tasks & set(live_tasks)),
            "missing_in_shadow": missing, "extra_in_shadow": extra, "field_mismatch": fields,
            "child_mismatch": children, "rejected_ops": rejected, "foreign_cards": foreign}


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
        held = {(t, u) for t, u in live.execute(
            "SELECT table_name, row_uid FROM kanban_sync_outbox WHERE state IN ('pending', 'rejected')")}
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
    """Queue the local values of this gateway's cards that the service never received.

    A card whose live ``sync_version`` equals the replica's, with no outbox op in flight, has seen every service
    write. Any synced field that still differs was then written locally with no capture trigger, e.g. in the
    window while another process re-installed the triggers. The install heal only finds NULL-uid children;
    ``repair_missing_children`` handles settled missing links. A task row has no missing-row marker, so this
    field comparison is the only place that finds its uncaptured edits.
    Hub incident 2026-10-28: a ``scheduled -> ready`` wake reached the service as its event but not as its
    status, and the service refused every lease as ``not_claimable`` for 3 h. Returns {task id: fields queued}."""
    from hermes_cli.kanban_db_connect import write_txn
    held = {r[0] for r in live.execute(
        "SELECT DISTINCT row_uid FROM kanban_sync_outbox WHERE table_name = 'tasks' AND state IN ('pending', 'rejected')")}
    seen = {r[0]: r[1] for r in shadow.execute(
        "SELECT id, sync_version FROM tasks WHERE (created_on = ? OR created_on IS NULL) AND sync_version IS NOT NULL",
        (gateway,))}
    repaired: dict[str, list[str]] = {}
    rows = live.execute("SELECT * FROM tasks WHERE sync_version IS NOT NULL").fetchall()
    for row in rows:
        tid = row["id"]
        if tid in held or seen.get(tid) != row["sync_version"]:
            continue
        theirs = shadow.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        mine, remote = _row_fields(live, "tasks", row), _row_fields(shadow, "tasks", theirs)
        fields = {k: v for k, v in mine.items() if k in remote and remote[k] != v}
        if not fields:
            continue
        runs = _diverged_runs(live, shadow, tid)
        with write_txn(live):
            # [daveey] The same uncaptured transaction usually ended the card's run too (``complete_task`` writes
            # both). Queue those run rows FIRST: a card arriving ``done`` before its run makes the Board end the
            # lease's run generically (rows.ts ``endLeaseRun``: ``released``, "card left running (done)"), and
            # with no later holder write that row came back over the real ``done/completed`` run. The owner then
            # published the card unverified, and no origin ever released it (hub t_01a185075c59bd52, 10-29).
            for run_uid, run_fields, run_version in runs:
                live.execute(
                    "INSERT INTO kanban_sync_outbox (op_id, table_name, row_uid, op, fields, base_version, hlc, "
                    "created_at) VALUES (?, 'task_runs', ?, 'upsert', ?, ?, '', ?)",
                    (sync.uuid7(), run_uid, json.dumps(run_fields), None, int(time.time() * 1000)))
            live.execute(
                "INSERT INTO kanban_sync_outbox (op_id, table_name, row_uid, op, fields, base_version, hlc, created_at) "
                "VALUES (?, 'tasks', ?, 'upsert', ?, ?, '', ?)",
                (sync.uuid7(), tid, json.dumps(fields), row["sync_version"], int(time.time() * 1000)))
        repaired[tid] = sorted(fields) + [f"task_runs:{uid}" for uid, _f, _v in runs]
        _log.warning("kanban sync: %s %s differed from the service with nothing in flight (an uncaptured write); "
                     "re-queued", tid, ", ".join(repaired[tid]))
    return repaired


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


# rows.ts ``upsertTask``: the holder moved its card out of ``running`` before its own end of the run arrived, so the
# Board ended the lease's run with this placeholder. Its comment: "the holder's own end of the run normally follows".
_LEASE_END_PLACEHOLDER = "card left running (done)"


def heal_clobbered_completions(live: sqlite3.Connection) -> list[str]:
    """Restore completed runs the Board's placeholder lease end overwrote; returns the task ids healed.

    Guarded to the one shape that write leaves: the card is ``done``; the run is its latest; the run reads exactly
    ``released``/``released`` with the Board's placeholder error; and the card's latest ``completed`` event names
    that run (``complete_task`` writes the event and the ``done/completed`` run end in one transaction, so the
    event proves how this holder ended the run). The run is set back to ``done``/``completed`` with the event's
    summary, through the capture triggers, so the Board's copy is corrected by the next push too. Anything else is
    left alone: a run released for any other reason is not a completion. [daveey]"""
    from hermes_cli.kanban_db_connect import write_txn
    rows = live.execute(
        "SELECT r.id AS run_id, r.task_id, r.ended_at, t.completed_at FROM task_runs r JOIN tasks t ON t.id = r.task_id "
        "WHERE r.status = 'released' AND r.outcome = 'released' AND r.error = ? AND t.status = 'done' "
        "AND r.id = (SELECT MAX(id) FROM task_runs WHERE task_id = r.task_id)", (_LEASE_END_PLACEHOLDER,)).fetchall()
    healed = []
    for row in rows:
        event = live.execute(
            "SELECT run_id, payload FROM task_events WHERE task_id = ? AND kind = 'completed' ORDER BY id DESC LIMIT 1",
            (row["task_id"],)).fetchone()
        if event is None or event["run_id"] != row["run_id"]:
            continue
        try:
            payload = json.loads(event["payload"] or "{}")
        except ValueError:
            payload = {}
        summary = (payload.get("summary") or payload.get("result")) if isinstance(payload, dict) else None
        ended = max(int(row["ended_at"] or 0), int(row["completed_at"] or 0)) or None
        with write_txn(live):
            changed = live.execute(
                "UPDATE task_runs SET status = 'done', outcome = 'completed', error = NULL, "
                "summary = COALESCE(summary, ?), ended_at = ? "
                "WHERE id = ? AND status = 'released' AND outcome = 'released' AND error = ?",
                (summary, ended, row["run_id"], _LEASE_END_PLACEHOLDER)).rowcount
        if changed:
            healed.append(row["task_id"])
            _log.warning("kanban sync: %s run %s was ended by the Board's lease placeholder over its completion; "
                         "restored done/completed", row["task_id"], row["run_id"])
    return healed


# --- leases (primary boards) ----------------------------------------------------------------------

class HubLeaseClient:
    """:class:`kanban_db_sync.LeaseClient` over ``POST /v2/boards/<id>/leases``."""

    def __init__(self, hub: Hub) -> None:
        self.hub = hub

    def _lease(self, board_id: str, body: dict, run_uid: str) -> sync.Lease:
        try:
            res = self.hub.leases(board_id, {**body, "client_op_id": sync.uuid7()})
        except (HubError, OSError) as exc:
            raise sync.LeaseUnavailable(str(exc)) from exc
        lease = res.get("lease") or {}
        granted = res.get("status") == "applied" and lease.get("run_uid") == run_uid
        return sync.Lease(granted=granted, run_uid=run_uid, expires_ms=lease.get("expires"),
                          holder=lease.get("holder"), reason=None if granted else res.get("reason"),
                          version=res.get("version"))

    def acquire(self, *, board_id: str, task_uid: str, run_uid: str, gateway_id: str, ttl_s: int,
                lane: str = "ready") -> sync.Lease:
        return self._lease(board_id, {"action": "grant", "task": task_uid, "run_uid": run_uid, "ttl_s": ttl_s,
                                      "lane": lane}, run_uid)

    def renew(self, *, board_id: str, task_uid: str, run_uid: str, gateway_id: str, ttl_s: int) -> sync.Lease:
        return self._lease(board_id, {"action": "renew", "task": task_uid, "run_uid": run_uid, "ttl_s": ttl_s}, run_uid)

    def release(self, *, board_id: str, task_uid: str, run_uid: str, gateway_id: str,
                unused_grant: bool = False) -> None:
        # ``unused_grant``: released before anything started locally (the Board does not count it as a start).
        self._lease(board_id, {"action": "release", "task": task_uid, "run_uid": run_uid,
                               **({"unused_grant": True} if unused_grant else {})}, run_uid)
