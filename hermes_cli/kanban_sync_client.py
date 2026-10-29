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
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from hermes_cli import kanban_db_sync as sync

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


class Resync(Exception):
    """The cursor is below the service's retention floor: rebuild from a snapshot."""


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


class Hub:
    """The Board service's HTTP API for one gateway."""

    def __init__(self, ident: Identity, timeout: float = HTTP_TIMEOUT_S) -> None:
        self.ident, self.timeout = ident, timeout

    def _call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.ident.url + path, data=data, method=method,
            headers={"authorization": f"Bearer {self.ident.key}", "content-type": "application/json",
                     "user-agent": "hermes-kanban-sync/1"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")[:500]
            if exc.code == 410:
                raise Resync(text) from exc
            raise HubError(exc.code, text) from exc

    def ops(self, board: str, ops: list[dict]) -> dict:
        return self._call("POST", f"/v2/boards/{board}/ops", {"ops": ops})

    def changes(self, board: str, since: int, limit: int = FEED_PAGE) -> dict:
        return self._call("GET", f"/v2/boards/{board}/changes?since={int(since)}&limit={int(limit)}")

    def snapshot_page(self, board: str, table: str, after: str = "", limit: int = SNAPSHOT_PAGE) -> dict:
        from urllib.parse import quote
        return self._call("GET", f"/v2/boards/{board}/snapshot?table={table}&after={quote(after)}&limit={limit}")

    def leases(self, board: str, body: dict) -> dict:
        return self._call("POST", f"/v2/boards/{board}/leases", body)

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
        """This machine's own load for the registry (display only; see hermes_cli.machine_metrics)."""
        return self._call("POST", "/v2/machines/me/metrics", metrics)

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
    return [(slug, path) for slug, path in found if sync.board_selected(str(path))]


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
        return row
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
    total = 0
    cursor = int((sync.sync_state(conn) or {}).get("cursor") or 0)
    while True:
        page = hub.changes(board, cursor)
        changes = [c for c in (adapt(ch) for ch in page.get("changes", [])) if c is not None]
        if partition is not None:
            changes = own_partition(conn, changes, partition)
        changes = without_pending(changes, pending_task_fields(conn))
        nxt = int(page.get("next", cursor))
        # [daveey] The pre-filter above can miss a local edit committed after it read the outbox (a claim in
        # flight): ``local_pending_wins`` checks again inside the apply transaction.
        sync.apply_remote_changes(conn, changes, new_cursor=nxt, board=board, observers=observers,
                                  local_pending_wins=True)
        total += len(page.get("changes", []))
        cursor = nxt
        if not page.get("more"):
            return total


def resync(conn: sqlite3.Connection, hub: Hub, board: str, *, observers: bool, partition: Optional[str] = None) -> int:
    """Rebuild from the service snapshot (every live row, paged per table), then resume the feed at its head.
    ``partition`` keeps only that gateway's cards (a primary board's live file)."""
    head: Optional[int] = None
    count = 0
    for table in IMPORT_ORDER:
        after = ""
        while True:
            page = hub.snapshot_page(board, table, after)
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


def open_shadow(slug: str, gateway: str) -> sqlite3.Connection:
    path = shadow_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(path)
    ensure_identity(conn, slug, gateway)
    return conn


def _pull_replica(slug: str, hub: Hub, gateway: str) -> int:
    """The whole board into the replica copy (no hooks); a cursor below the retention floor rebuilds it."""
    replica = open_shadow(slug, gateway)
    try:
        try:
            pulled = pull(replica, hub, slug, observers=False)
        except Resync:
            _log.warning("kanban sync: %s replica cursor below the retention floor; rebuilding from a snapshot", slug)
            replica.close()
            shadow_path(slug).unlink(missing_ok=True)
            replica = open_shadow(slug, gateway)
            pulled = resync(replica, hub, slug, observers=False)
        try:  # readers derive the replica's staleness from this (kanban_chat_links); a failed stamp only ages it
            sync.mark_pulled(replica)
        except sqlite3.Error as exc:
            _log.warning("kanban sync: %s replica pull not stamped (%s)", slug, exc)
        return pulled
    finally:
        replica.close()


def sync_board(slug: str, path: Path, hub: Hub, gateway: str) -> RoundResult:
    """One round for one board. Both modes keep the replica copy (the whole board) current and push the live
    outbox; ``primary`` also applies this gateway's own cards from the feed into the live board."""
    live = _connect(path)
    try:
        mode = sync.board_mode(live)
        result = RoundResult(board=slug, mode=mode)
        if mode == "off":
            return result
        ensure_identity(live, slug, gateway)
        result.pulled = _pull_replica(slug, hub, gateway)
        if mode == "primary":
            try:
                pull(live, hub, slug, observers=True, partition=gateway)
            except Resync:
                _log.warning("kanban sync: %s live cursor below the retention floor; rebuilding from a snapshot", slug)
                resync(live, hub, slug, observers=True, partition=gateway)
        replica = open_shadow(slug, gateway)
        try:
            from hermes_cli.kanban_sync_partition_repair import repair_partition_links
            repair_partition_links(live, replica, gateway)
            if mode == "primary" and sync.refresh_remote_parents(live, replica):
                sync.wake_dispatcher()  # a foreign parent moved: re-gate its children now
            result.repaired = repair_divergence(live, replica, gateway)
            repair_missing_children(live, replica, gateway)
            if mode == "primary":
                heal_clobbered_completions(live)
        finally:
            replica.close()
        result.push = push(live, hub, slug, gateway, primary=mode == "primary")
        return result
    finally:
        live.close()


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


def sync_all_once(hub: Optional[Hub] = None) -> list[RoundResult]:
    ident = load_identity() if hub is None else hub.ident
    hub = hub or Hub(ident)
    publish_pickup_if_changed(hub)
    results = []
    for slug, path in board_paths():
        try:
            results.append(sync_board(slug, path, hub, ident.gateway))
        except (HubError, OSError, Resync, sqlite3.Error, SyncUnavailable) as exc:
            _log.warning("kanban sync: round for %s failed (%s: %s)", slug, type(exc).__name__, exc)
            results.append(RoundResult(board=slug, mode="?", error=f"{type(exc).__name__}: {exc}"))
    return results


class FeedWatcher:
    """The Board's push channel for one board: ``GET /v2/boards/<id>/ws`` (WebSocket, gateway key). Any change frame
    sets ``wake`` so the sync loop pulls now instead of at its next poll. Keeps the socket alive with a ping every
    ``ping_s`` and reconnects with backoff; polling still covers a closed socket."""

    def __init__(self, ident: Identity, board: str, wake: threading.Event, keep_running: Callable[[], bool],
                 ping_s: float = 25.0) -> None:
        self.ident, self.board, self.wake, self.keep_running, self.ping_s = ident, board, wake, keep_running, ping_s
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
                            self.wake.set()
            except Exception as exc:  # health: allow BLE001 -- any socket failure: back off and reconnect; polling covers the gap
                _log.info("kanban sync: %s feed socket closed (%s: %s); reconnecting in %.0fs",
                          self.board, type(exc).__name__, exc, backoff)
                deadline = time.monotonic() + backoff
                while self.keep_running() and time.monotonic() < deadline:
                    time.sleep(0.2)
                backoff = min(backoff * 2, 60.0)


LINKS_EVERY_S = 60.0


def reconcile_links(boards: list[tuple[str, Path]], gateway: str) -> None:
    """Bring this gateway's chat links up to date in every synced board (kanban_chat_links.reconcile), creating a
    link-only board for each board this gateway's chats have cards on but this host does not have."""
    from hermes_cli import kanban_chat_links
    try:
        created = ensure_link_boards(kanban_chat_links.ledger_boards(gateway), gateway)
        if created:
            _log.info("kanban sync: link-only boards %s", created)
            boards = board_paths()
    except Exception as exc:  # health: allow BLE001 -- link-only boards are best effort; local links still reconcile
        _log.warning("kanban sync: link-only boards failed (%s: %s)", type(exc).__name__, exc)
    for slug, path in boards:
        try:
            conn = _connect(path)
            try:
                if sync.board_mode(conn) == "off":
                    continue
                counts = kanban_chat_links.reconcile(conn, slug, gateway)
            finally:
                conn.close()
            if any(counts.values()):
                _log.info("kanban sync: %s chat links %s", slug, counts)
        except Exception as exc:  # health: allow BLE001 -- one board's links must not stop the sync loop
            _log.warning("kanban sync: chat links for %s failed (%s: %s)", slug, type(exc).__name__, exc)


def outbox_pending(paths: Iterable[Path]) -> bool:
    """True when any synced board has a local change waiting to be pushed (one cheap read-only query each)."""
    for path in paths:
        try:
            conn = _kanban_lockguard.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1)
            try:
                if conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE state = 'pending' LIMIT 1").fetchone():
                    return True
            finally:
                conn.close()
        except sqlite3.Error:
            continue
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


def run_forever(keep_running: Callable[[], bool], interval: Optional[float] = None,
                sleep: Callable[[float], None] = time.sleep) -> None:
    """The gateway's sync loop (call from a worker thread). Installs the lease client for primary boards, holds one
    feed socket per synced board, and runs a round when the Board reports a change, a local change is waiting, or
    the poll period passes."""
    try:
        ident = load_identity()
    except SyncUnavailable as exc:
        _log.warning("%s; kanban sync not started", exc)
        return
    hub = Hub(ident)
    sync.set_lease_client(HubLeaseClient(hub))
    period = float(interval or sync.sync_config().get("interval_seconds") or DEFAULT_INTERVAL_S)
    _log.info("kanban sync: running as gateway %s against %s (feed sockets; poll every %.1fs)", ident.gateway, ident.url, period)
    wake = threading.Event()
    watchers: dict[str, threading.Thread] = {}
    next_links = 0.0
    from hermes_cli.machine_metrics import Reporter
    metrics = Reporter(hub.report_metrics)
    while keep_running():
        boards = board_paths()
        for slug, _path in boards:
            if slug not in watchers or not watchers[slug].is_alive():
                watcher = FeedWatcher(ident, slug, wake, keep_running)
                watchers[slug] = threading.Thread(target=watcher.run, daemon=True, name=f"kanban-feed-{slug}")
                watchers[slug].start()
        for r in sync_all_once(hub):
            if r.push and (r.push.sent or r.push.rejected):
                _log.info("kanban sync: %s %s pulled=%d pushed=%d rejected=%d", r.board, r.mode, r.pulled,
                          r.push.sent, r.push.rejected)
        if time.monotonic() >= next_links:
            reconcile_links(boards, ident.gateway)
            next_links = time.monotonic() + LINKS_EVERY_S
        metrics.report_due()
        wait_for_work(wake, [p for _s, p in boards], period, keep_running)


# --- import ---------------------------------------------------------------------------------------

def _row_fields(conn: sqlite3.Connection, table: str, row: sqlite3.Row) -> dict:
    out: dict[str, Any] = {}
    for column in sync.captured_columns(conn, table):
        if column not in sync.SYNCED_COLUMNS[table]:
            continue
        value = row[column]
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
    return {"mode": sync.board_mode(conn), "board_id": st.get("board_id"), "gateway": st.get("gateway_id"),
            "cursor": st.get("cursor"), "pending": counts.get("pending", 0), "rejected": counts.get("rejected", 0),
            "last_error": last_error}


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


def _child_uids(conn: sqlite3.Connection, table: str, task_ids: set[str]) -> set[tuple[str, str]]:
    if table == "task_links":
        return {(p, f"{p}>{c}") for p, c in conn.execute("SELECT parent_id, child_id FROM task_links") if c in task_ids}
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
        lv = _child_uids(live, table, checked)
        sh = _child_uids(shadow, table, checked)
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
        held |= {(t, u) for t, u in live.execute("SELECT table_name, row_uid FROM kanban_sync_tombstones")}
        held |= {(t, u) for t, u in shadow.execute("SELECT table_name, row_uid FROM kanban_sync_tombstones")}
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

    def release(self, *, board_id: str, task_uid: str, run_uid: str, gateway_id: str) -> None:
        self._lease(board_id, {"action": "release", "task": task_uid, "run_uid": run_uid}, run_uid)
