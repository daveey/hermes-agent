"""Worker nodes: run cards another gateway created (RFC "Worker nodes and transcripts", Placement). [daveey]

A primary board's live file holds only this gateway's own cards; the replica (``sync-shadow/<board>.db``) holds the
whole board. On a gateway with ``kanban.sync.pickup`` (``explicit``: only cards with ``runs_on``; true: any), each dispatcher tick with free slots copies eligible
``ready`` cards from the replica into the live file (rows applied with no capture, so nothing is pushed), and the
normal claim path then asks the Board for a lease. The Board is the authority: it checks the machine registry
(labels, ``pinned_only``, ``hold``, ``max_card_runs``) and ``runs_on`` on every grant, so the local check here only
avoids asking for cards the Board would refuse. A refused grant leaves the card ``ready`` and unclaimed, and the
next tick evicts it. A copied card that ends (or is leased elsewhere) is evicted once its outbox is drained.

Placement rules mirrored from the Board (``runs_on`` a JSON list of labels):

* ``runs_on`` set: every label must be on this machine (``[]`` = any machine).
* ``runs_on`` NULL (default): another machine may take the card only when its labels include the creator's (minus
  ``machine:*``), the workspace is not ``dir``/``worktree``, and the card has been ready for ``home_grace`` or the
  creator has gone silent. The Board decides the time part; locally only the label and workspace parts are checked.
* a ``pinned_only`` machine takes another gateway's card only when its ``runs_on`` names the machine's
  ``machine:`` label; its own cards are always its own.

``kanban_create(placement=<name>)`` turns the pin into ``runs_on=[machine:<name>]`` only when the machine's registry
entry advertises ``pickup`` (:func:`advertises_pickup`; a missing field means no pickup). Otherwise the pin keeps its
gateway meaning (``kanban.placement`` route), so a card is never parked for a machine that never copies it.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Optional

from hermes_cli import kanban_db_sync as sync

_log = logging.getLogger(__name__)

LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9:._-]{0,63}$")
MAX_LABELS = 32
REGISTRY_TTL_S = 60
REGISTRY_RETRY_S = 5
# Copy at most this many cards per tick when the dispatcher has no cap.
DEFAULT_PICKUP_BATCH = 4
# History copied with a card: enough for the worker's context, without the whole event log.
EVENT_TAIL = 200
_CHILD_TABLES = ("task_runs", "task_comments", "task_attachments", "task_events")

_REGISTRY: dict[str, Any] = {"at": None, "value": None, "retry": False}
_REGISTRY_LOCK = threading.Lock()
_FETCH: Optional[Callable[[], Mapping[str, Any]]] = None


# --- runs_on --------------------------------------------------------------------------------------

def normalize_runs_on(value: Any) -> Optional[list[str]]:
    """``runs_on`` from a CLI/tool argument: None (default placement), a list, or a comma string. Raises ValueError."""
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value) if value.strip().startswith("[") else [v for v in value.split(",") if v.strip()]
    if not isinstance(value, (list, tuple)):
        raise ValueError("runs_on must be a list of labels")
    labels = [str(v).strip() for v in value]
    bad = [v for v in labels if not LABEL_RE.match(v)]
    if bad or len(labels) > MAX_LABELS:
        raise ValueError(f"invalid runs_on label(s) {bad or labels!r}: lowercase [a-z0-9:._-], at most {MAX_LABELS}")
    return sorted(set(labels))


def set_runs_on(conn: sqlite3.Connection, task_id: str, runs_on: Optional[list[str]]) -> None:
    """Write ``runs_on`` on a card of a synced board (captured and pushed like any field)."""
    if "runs_on" not in sync.table_columns(conn, "tasks"):
        raise ValueError("runs_on needs a board that syncs to the Board service (kanban.sync)")
    conn.execute("UPDATE tasks SET runs_on = ? WHERE id = ?",
                 (None if runs_on is None else json.dumps(runs_on), task_id))


def advertises_pickup(entry: Optional[Mapping[str, Any]]) -> bool:
    """True when a registry entry says the machine copies other gateways' cards (``kanban.sync.pickup`` on).
    A missing field means no pickup: a ``runs_on=[machine:<name>]`` card for such a machine is never claimed."""
    value = (entry or {}).get("pickup")
    if isinstance(value, str):
        return value.strip().lower() in ("explicit", "all", "true", "on", "1", "yes")
    return value is True


class MachinePinRefused(Exception):  # not ValueError: label-syntax handlers must not swallow its code
    """An explicit ``runs_on`` names ``machine:<name>`` for a machine that never copies this board's cards.

    Such a card sits ``ready`` forever: only that machine may lease it, and it never looks (10-28: three hub cards
    with ``runs_on=[machine:macbook]`` were never claimed). Nothing was written; the working route is a
    cross-gateway create on the machine's own gateway (``placement="<name>"``)."""

    code = "placement_unavailable"

    def __init__(self, machine: str, why: str):
        self.machine = machine
        super().__init__(
            f"runs_on names machine:{machine}, but {why}, so no worker would ever claim the card on this board. "
            f"Nothing was written. Create it on that machine's own gateway instead (a cross-gateway create): "
            f"kanban_create(placement=\"{machine}\") (works for any live gateway, routed or not), or "
            f"`hermes-hub create-task {machine} <title> --body ...`.")

    @property
    def tool_error_fields(self) -> dict:
        return {"machine": self.machine, "placement": self.machine, "created": False}


def check_machine_pins(conn: sqlite3.Connection, runs_on: Any, *,
                       machines: Optional[Mapping[str, Mapping[str, Any]]] = None,
                       me: Optional[str] = None) -> None:
    """Refuse (:class:`MachinePinRefused`) a ``runs_on`` naming ``machine:<x>`` (x not this machine) unless x is an
    active registry machine that advertises pickup (:func:`advertises_pickup`; null or missing = no pickup).
    Labels other than ``machine:*`` are not checked. A no-op off a Board primary (``set_runs_on`` refuses there).
    The registry unreachable fails closed: pickup cannot be confirmed."""
    if runs_on is None:
        return
    labels = normalize_runs_on(runs_on if isinstance(runs_on, str) else list(runs_on))
    names = [lab.split(":", 1)[1] for lab in labels or [] if lab.startswith("machine:")]
    if not names or sync.board_mode(conn) != "primary":
        return
    me = me or _own(conn)
    names = [n for n in names if n != me]
    if not names:
        return
    pool = machines
    if pool is None:
        reg = registry()
        if reg is None:
            raise MachinePinRefused(names[0], "the Board machine registry is unreachable, so its pickup is unknown")
        pool = reg.get("machines") or {}
    for name in names:
        entry = pool.get(name)
        if not entry:
            raise MachinePinRefused(name, "no machine of that name is enrolled in the Board registry")
        if entry.get("state", "active") != "active":
            raise MachinePinRefused(name, f"that machine is {entry.get('state')!r}, not active")
        if not advertises_pickup(entry):
            raise MachinePinRefused(name, "that machine does not pick up other gateways' cards (its /v2/machines "
                                          f"entry has pickup={entry.get('pickup')!r})")


def explicit_runs_on(conn: sqlite3.Connection, value: Any) -> Optional[list[str]]:
    """A caller-supplied ``runs_on`` (create tool / CLI): :func:`normalize_runs_on`, then :func:`check_machine_pins`.
    Placement-derived ``runs_on`` (``placement_runs_on``, ``heavy_route``) keeps its own pickup rule."""
    labels = normalize_runs_on(value)
    check_machine_pins(conn, labels)
    return labels


def placement_runs_on(conn: sqlite3.Connection, placement: Any) -> Optional[list[str]]:
    """``placement=<gateway>`` on a primary board, naming another active machine in the registry that picks up other
    gateways' cards -> ``[machine:<gateway>]``. None for ``auto``/``local``/this gateway/unknown names/machines
    without pickup (today's placement applies, e.g. the ``kanban.placement`` route)."""
    name = str(placement or "").strip()
    if not name or name in ("auto", "local") or sync.board_mode(conn) != "primary" or name == _own(conn):
        return None
    reg = registry()
    entry = (reg or {}).get("machines", {}).get(name)
    if not entry or entry.get("state", "active") != "active" or not advertises_pickup(entry):
        return None
    return [f"machine:{name}"]


def _parse(value: Any) -> Optional[list[str]]:
    if value is None or value == "":
        return None
    try:
        out = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return []  # unreadable: treat as "no machine matches" rather than "default"
    return [str(v) for v in out] if isinstance(out, list) else []


# --- registry -------------------------------------------------------------------------------------

def set_registry_fetch(fn: Optional[Callable[[], Mapping[str, Any]]]) -> None:
    """Tests (and the sync loop) inject how ``GET /v2/machines`` is read; resets the cache."""
    global _FETCH
    with _REGISTRY_LOCK:
        _FETCH = fn
        _REGISTRY.update(at=None, value=None, retry=False)


def registry() -> Optional[Mapping[str, Any]]:
    """``{me, machines: {name: entry}}``, cached for :data:`REGISTRY_TTL_S`; None when unreachable."""
    # Several board/executor ticks can reach the same expired entry concurrently.
    # Failed attempts also consume the TTL: an outage must not turn every tick
    # into another HTTP request. The Board still authorizes every actual lease.
    with _REGISTRY_LOCK:
        return _registry_locked()


def _registry_locked() -> Optional[Mapping[str, Any]]:
    now = time.monotonic()
    ttl = REGISTRY_RETRY_S if _REGISTRY.get('retry') else REGISTRY_TTL_S
    if _REGISTRY["at"] is not None and now - _REGISTRY["at"] < ttl:
        return _REGISTRY["value"]
    fetch = _FETCH
    if fetch is None:
        from hermes_cli import kanban_sync_client as ksc
        hub = ksc.Hub(ksc.load_identity())
        fetch = hub.machines
    try:
        raw = fetch()
    except Exception as exc:  # health: allow BLE001 -- registry unreachable: pick up nothing this tick
        _REGISTRY.update(at=time.monotonic(), retry=True)
        _log.info("kanban worker node: machine registry unavailable (%s: %s)", type(exc).__name__, exc)
        return _REGISTRY["value"]
    value = {"me": raw.get("me"), "machines": {m["name"]: m for m in raw.get("machines") or [] if m.get("name")}}
    _REGISTRY.update(at=time.monotonic(), value=value, retry=False)
    return value


def effective_labels(entry: Optional[Mapping[str, Any]], name: str) -> set[str]:
    return set((entry or {}).get("labels") or []) | {f"machine:{name}"}


def eligible(card: Mapping[str, Any], me: str, machines: Mapping[str, Mapping[str, Any]]) -> Optional[str]:
    """None when this machine may ask for the card's lease, else the Board's refusal reason it would get."""
    entry = machines.get(me) or {}
    if entry.get("hold"):
        return "held"
    mine = effective_labels(entry, me)
    runs_on = _parse(card.get("runs_on"))
    creator = card.get("created_on")
    own = creator in (None, me)
    # pinned_only keeps other gateways' work off a personal machine; its own cards stay its own.
    if entry.get("pinned_only") and not own and f"machine:{me}" not in (runs_on or []):
        return "pinned_only"
    if runs_on is not None:
        return None if set(runs_on) <= mine else "labels_mismatch"
    if own:
        return None
    if card.get("workspace_kind") in ("dir", "worktree"):
        return "labels_mismatch"
    wanted = {lb for lb in effective_labels(machines.get(creator), creator) if not lb.startswith("machine:")}
    return None if wanted <= mine else "labels_mismatch"


# --- pickup ---------------------------------------------------------------------------------------

def pickup_mode() -> Optional[str]:
    """``kanban.sync.pickup``: unset/false = off; ``explicit`` = only cards whose ``runs_on`` is set (placement
    asked for a machine like this one); true/``all`` = also default-placement cards the Board lets it take."""
    value = sync.sync_config().get("pickup")
    if value in (None, False, "off", "false"):
        return None
    return "explicit" if value == "explicit" else "all"


def _replica_path(conn: sqlite3.Connection):
    from hermes_cli import kanban_sync_client as ksc
    return ksc.shadow_path(sync.board_slug_for_path(sync._db_key(conn) or ""))


def _own(conn: sqlite3.Connection) -> str:
    return str(sync._state(conn)["gateway_id"])


def evict(conn: sqlite3.Connection) -> list[str]:
    """Drop copied foreign cards this gateway is not running (no claim, not ``running``) and has nothing left to
    push. Deleted with capture suppressed, so the Board never sees a delete."""
    own = _own(conn)
    probes = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_wake_checks'").fetchone()
    # [daveey] A scheduled copy whose wake_check this gateway runs stays until it wakes: the probe row is
    # machine-local, and evicting the copy let the stale sweep delete it, so the card slept until its clock
    # (t_01a18c12ba6895b8). Its wake is a status write the Board carries to the home board.
    # A blocked copy whose ask resolved_check this gateway runs (it filed the ask) stays the same way.
    keep = (" AND NOT EXISTS (SELECT 1 FROM task_wake_checks w WHERE w.task_id = tasks.id AND "
            "((tasks.status = 'scheduled' AND w.purpose IS NOT 'ask_resolved') "
            "OR (tasks.status = 'blocked' AND w.purpose = 'ask_resolved')))" if probes else "")
    rows = conn.execute(
        "SELECT id FROM tasks WHERE created_on IS NOT NULL AND created_on != ? AND claim_lock IS NULL "
        "AND status != 'running'" + keep, (own,)).fetchall()
    gone: list[str] = []
    if not rows:
        return gone
    present = {t for t in (*_CHILD_TABLES, "task_chat_links")
               if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()}
    from hermes_cli.kanban_db_connect import write_txn
    with write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET applying = 1")
        for (tid,) in rows:
            if not conn.execute(
                    "SELECT 1 FROM tasks WHERE id=? AND created_on IS NOT NULL AND created_on != ? "
                    "AND claim_lock IS NULL AND status != 'running'" + keep, (tid, own)).fetchone():
                continue
            if conn.execute(
                    "SELECT 1 FROM kanban_sync_outbox WHERE state != 'rejected' AND (row_uid = ? OR "
                    "json_extract(fields, '$.task_id') = ?) LIMIT 1", (tid, tid)).fetchone():
                continue
            from hermes_cli.kanban_run_tombstones import retain_ended_runs
            from hermes_cli.kanban_scratch_custody import retain
            retain(conn, tid)
            retain_ended_runs(conn, tid)
            for table in present:
                conn.execute(f"DELETE FROM {table} WHERE task_id = ?", (tid,))
            # The copy's own edges go with it; an edge from it to a card still in this file belongs to that child
            # (own_partition keeps it, the Board still holds it). Dropping it un-gated a hub child of a copy evicted
            # between review hand-off and re-pickup: 51 promoted / 97 claim_rejected parents_not_done (2026-10-29).
            conn.execute("DELETE FROM task_links WHERE child_id = ? OR (parent_id = ? AND child_id NOT IN "
                         "(SELECT id FROM tasks WHERE id != ?))", (tid, tid, tid))
            conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
            gone.append(tid)
        conn.execute("UPDATE kanban_sync_state SET applying = 0")
    return gone


def dispatch_refusals(conn: sqlite3.Connection, status: str) -> dict[str, str]:
    """Advisory prefilter; the direct claim gate remains authoritative on errors."""
    try:
        return local_refusals(conn, status)
    except Exception:  # health: allow BLE001 -- eligibility is advisory; the Board lease still decides
        from hermes_cli import kanban_db
        kanban_db._log.debug("kanban dispatch: local eligibility check failed", exc_info=True)
        return {}


def local_refusals(conn: sqlite3.Connection, status: str) -> dict[str, str]:
    """``{task_id: reason}`` for unclaimed ``status`` cards this gateway cannot lease: the Board's refusal
    (:func:`eligible`) computed before the claim, so local dispatch never spends a slot on them. Only cards with
    ``runs_on`` or another creator can be refused (a held machine is left to the lease). Empty when the board is
    not primary or the registry is unreachable: the lease stays the authority and fails closed."""
    from hermes_cli.kanban_worker_placement import local_refusals as policy_refusals
    out = policy_refusals(conn, status)
    if sync.board_mode(conn) != "primary":
        return out
    own = _own(conn)
    rows = conn.execute(
        "SELECT id, runs_on, created_on, workspace_kind FROM tasks WHERE status = ? AND claim_lock IS NULL "
        "AND (runs_on IS NOT NULL OR (created_on IS NOT NULL AND created_on != ?))", (status, own)).fetchall()
    if not rows:
        return out
    reg = registry()
    if not reg:
        return out
    machines = reg["machines"]
    for row in rows:
        reason = eligible(dict(row), own, machines)
        if reason is not None and reason != "held":
            out[row["id"]] = reason
    return out


def candidates(replica: sqlite3.Connection, own: str, *, now_ms: Optional[int] = None) -> list[sqlite3.Row]:
    """Foreign ``ready`` and ``review`` cards without a live lease, reviews first (they finish work in flight).
    A review runs where its ``runs_on`` says, like its implementation run did: before 10-28 only ``ready`` was
    picked up, so a ``runs_on=[machine:hub]`` card that asked for review sat in the MacBook's review column,
    which the MacBook could never lease (``labels_mismatch``)."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return replica.execute(
        "SELECT * FROM tasks WHERE status IN ('ready', 'review') AND created_on IS NOT NULL AND created_on != ? "
        "AND (lease_holder IS NULL OR COALESCE(lease_expires, 0) < ?) "
        # The replica holds the whole board: a card with an open (or missing) parent would only be refused
        # parents_not_done by the Board, and that refusal demotes it on every machine. [daveey]
        "AND NOT EXISTS (SELECT 1 FROM task_links l LEFT JOIN tasks p ON p.id = l.parent_id "
        "WHERE l.child_id = tasks.id AND COALESCE(p.status, '') NOT IN ('done', 'archived')) "
        "ORDER BY status = 'review' DESC, priority DESC, created_at, id",
        (own, now_ms)).fetchall()


def _changes_for(replica: sqlite3.Connection, tid: str, card: sqlite3.Row) -> list[sync.RemoteChange]:
    from hermes_cli import kanban_sync_client as ksc
    fields = ksc._row_fields(replica, "tasks", card)
    for column in sync.SERVER_COLUMNS["tasks"]:
        if column in card.keys():
            fields[column] = card[column]
    out = [sync.RemoteChange(0, "tasks", tid, "upsert", fields, int(card["sync_version"] or 0), "", "")]
    for table in _CHILD_TABLES:
        order = f"id DESC LIMIT {EVENT_TAIL}" if table == "task_events" else "id"
        for row in replica.execute(f"SELECT * FROM {table} WHERE task_id = ? ORDER BY {order}", (tid,)).fetchall():
            if row["uid"] is None:
                continue
            version = int(row["sync_version"] or 0) if "sync_version" in row.keys() else 0
            out.append(sync.RemoteChange(0, table, row["uid"], "upsert", ksc._row_fields(replica, table, row),
                                         version, "", ""))
    # Its parent edges too: once the copy is here the feed applies its later links (own_partition), so a copy
    # without the earlier ones disagreed with the Board for as long as it ran.
    for row in replica.execute("SELECT * FROM task_links WHERE child_id = ? ORDER BY parent_id", (tid,)).fetchall():
        out.append(sync.RemoteChange(0, "task_links", f"{row['parent_id']}>{tid}", "upsert",
                                     ksc._row_fields(replica, "task_links", row), 0, "", ""))
    return out


def import_cards(conn: sqlite3.Connection, replica: sqlite3.Connection, ids: Iterable[str]) -> list[str]:
    """Copy cards (and their history) from the replica into the live file, without capture and without hooks."""
    changes: list[sync.RemoteChange] = []
    done: list[str] = []
    for tid in ids:
        card = replica.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        if card is None:
            continue
        changes.extend(_changes_for(replica, tid, card))
        done.append(tid)
    if changes:
        cursor = int((sync.sync_state(conn) or {}).get("cursor") or 0)
        sync.apply_remote_changes(conn, changes, new_cursor=cursor, observers=False)
        # The copy's foreign parents are gated by the mirror; filled only in the sync round, a parent read as
        # missing in this tick would demote the ready copy to todo and push that to the Board.
        sync.refresh_remote_parents(conn, replica)
    return done


def pickup(conn: sqlite3.Connection, *, budget: Optional[int],
           profile_exists: Optional[Callable[[str], bool]] = None) -> list[str]:
    """One tick's pickup: evict finished copies, then copy up to ``budget`` eligible foreign cards. Returns the
    ids copied. A no-op unless the board is primary and ``kanban.sync.pickup`` is on."""
    mode = pickup_mode()
    if sync.board_mode(conn) != "primary" or mode is None:
        return []
    evict(conn)
    limit = DEFAULT_PICKUP_BATCH if budget is None else budget
    if limit <= 0:
        return []
    reg = registry()
    if not reg:
        return []
    own = _own(conn)
    machines = reg["machines"]
    path = _replica_path(conn)
    if not path.exists():
        return []
    from hermes_cli import kanban_lockguard
    replica = kanban_lockguard.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    replica.row_factory = sqlite3.Row
    from hermes_cli.kanban_db_dispatch import review_dispatch_enabled
    review_enabled = review_dispatch_enabled()
    try:
        picked: list[str] = []
        for card in candidates(replica, own):
            if len(picked) >= limit:
                break
            if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (card["id"],)).fetchone():
                continue
            if (mode == "explicit" or card["status"] == "review") and card["runs_on"] is None:
                continue  # a default-placement review stays with its creator
            if card["status"] == "review" and not review_enabled:
                continue
            if eligible(dict(card), own, machines) is not None:
                continue
            if profile_exists is not None and card["assignee"] and not profile_exists(card["assignee"]):
                continue
            # [daveey] Never claim a card this machine's profile cannot satisfy: it would run, then fail its own
            # handoffs here (t_01a184ac93d31979). Left on the Board for a machine that can.
            from hermes_cli.kanban_runs_on_capabilities import claim_refusal
            if claim_refusal(dict(card), me=own) is not None:
                continue
            picked.append(card["id"])
        imported = import_cards(conn, replica, picked)
    finally:
        replica.close()
    if imported:
        _log.info("kanban worker node: picked up %d card(s) from other gateways: %s", len(imported), imported)
    return imported
