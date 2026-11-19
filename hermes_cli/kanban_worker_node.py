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
from hermes_cli.kanban_db_dispatch_queries import DISPATCH_ORDER
from hermes_cli.kanban_workspace_source import pickup_batch

_log = logging.getLogger(__name__)

LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9:._-]{0,63}$")
MAX_LABELS = 32
REGISTRY_TTL_S = 60
REGISTRY_RETRY_S = 5
# Copy at most this many cards per tick when the dispatcher has no cap.
DEFAULT_PICKUP_BATCH = 4
# Import runs before events so portable run UIDs resolve to the copied runs.
# [daveey] Copies truncated by the old newest-200 event tail (t_01a1ccf95e85a6f1 lost its early ``owner_admitted``
# rows, so ship's land gate refused) are refilled by kanban_pickup_history.
_CHILD_TABLES = ("task_runs", "task_comments", "task_attachments", "task_events")

_REGISTRY: dict[str, Any] = {"at": None, "value": None, "retry": False, "ok_at": None}
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
    cross-gateway create on the machine's own gateway (``placement="<name>"``).

    ``held`` is the other way a pin strands: the machine copies cards, but an operator set ``hold`` on it in the
    Board registry, so its pickup imports nothing and the Board refuses its leases (11-11: worker-1 was held for
    an AZ migration, a MacBook ``placement="worker-1"`` create returned ok and the card sat unclaimed for 48 min).
    Its own gateway is held too, so the remedy is another machine (``others``), not a cross-gateway create."""

    code = "placement_unavailable"

    def __init__(self, machine: str, why: str, *, held: bool = False, others: Iterable[str] = ()):
        self.machine = machine
        self.held = held
        self.others = sorted(others)
        if held:
            alt = (f"Pin a machine that is not held instead ({', '.join(self.others)})" if self.others
                   else "No other machine that picks up cards is unheld right now")
            super().__init__(
                f"The pin names machine:{machine}, but {why}, so no worker would claim the card until an operator "
                f"releases the hold. Nothing was written. {alt}, or leave placement and runs_on out so the card "
                f"takes the default route. `hermes-mail admin machine-audit {machine}` shows who set the hold and "
                "when; a hold is deliberate (a drain or a migration), so do not clear it to place a card.")
            return
        super().__init__(
            f"runs_on names machine:{machine}, but {why}, so no worker would ever claim the card on this board. "
            f"Nothing was written. Create it on that machine's own gateway instead (a cross-gateway create): "
            f"kanban_create(placement=\"{machine}\") (works for any live gateway, routed or not), or "
            f"`hermes-hub create-task {machine} <title> --body ...`.")

    @property
    def tool_error_fields(self) -> dict:
        if self.held:
            return {"machine": self.machine, "created": False, "rejection": "machine_held", "unheld": self.others}
        return {"machine": self.machine, "placement": self.machine, "created": False}


HELD_WHY = "that machine is on hold in the Board registry (hold=true: it copies and leases no new cards)"


def unheld_pickup_machines(pool: Mapping[str, Mapping[str, Any]], *, skip: str = "") -> list[str]:
    """Active registry machines that pick up other gateways' cards and are not on hold (``skip`` left out)."""
    return sorted(n for n, e in pool.items() if n != skip and e and e.get("state", "active") == "active"
                  and not e.get("hold") and advertises_pickup(e))


def check_machine_pins(conn: sqlite3.Connection, runs_on: Any, *,
                       machines: Optional[Mapping[str, Mapping[str, Any]]] = None,
                       me: Optional[str] = None) -> None:
    """Refuse (:class:`MachinePinRefused`) a ``runs_on`` naming ``machine:<x>`` (x not this machine) unless x is an
    active registry machine that advertises pickup (:func:`advertises_pickup`; null or missing = no pickup).
    Labels other than ``machine:*`` are not checked. A no-op off a Board primary (``set_runs_on`` refuses there).
    The registry unreachable fails closed: pickup cannot be confirmed. A machine on ``hold`` is refused too
    (``held``): placement-derived pins (``placement=<machine>``) are passed through here for that check."""
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
        if entry.get("hold"):  # its pickup imports nothing and the Board refuses its leases while held
            raise MachinePinRefused(name, HELD_WHY, held=True, others=unheld_pickup_machines(pool, skip=name))


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
        _REGISTRY.update(at=None, value=None, retry=False, ok_at=None)


def registry(*, require_fresh: bool = False) -> Optional[Mapping[str, Any]]:
    """``{me, machines: {name: entry}}``, cached for :data:`REGISTRY_TTL_S`; None when unreachable."""
    # Several board/executor ticks can reach the same expired entry concurrently.
    # Failed attempts also consume the TTL: an outage must not turn every tick
    # into another HTTP request. The Board still authorizes every actual lease.
    with _REGISTRY_LOCK:
        value = _registry_locked()
        # Last-good discovery is useful offline, but is not evidence for moving a stale pin.
        return None if require_fresh and _REGISTRY.get("retry") else value


def registry_fresh(max_age_s: float) -> Optional[Mapping[str, Any]]:
    """:func:`registry`, but None unless its last successful fetch is within ``max_age_s``.

    ``registry()`` keeps the last good snapshot through an outage (the Board still
    authorizes every lease). A caller that must not act on a frozen membership,
    such as the carried-input exemption, reads this instead and fails closed. [daveey]
    """
    with _REGISTRY_LOCK:
        value = _registry_locked()
        ok_at = _REGISTRY.get("ok_at")
    if ok_at is None or time.monotonic() - ok_at > max_age_s:
        return None
    return value


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
        machines = raw.get("machines") if isinstance(raw, dict) else None
        if (not isinstance(machines, list) or any(
                not isinstance(m, dict) or not isinstance(m.get("name"), str) or not m["name"].strip()
                for m in machines) or len({m["name"] for m in machines}) != len(machines)):
            raise ValueError("malformed machine registry")
        value = {"me": raw.get("me"), "machines": {m["name"]: m for m in machines}}
    except Exception as exc:  # health: allow BLE001 -- registry unreachable: pick up nothing this tick
        _REGISTRY.update(at=time.monotonic(), retry=True)
        _log.info("kanban worker node: machine registry unavailable (%s: %s)", type(exc).__name__, exc)
        return _REGISTRY["value"]
    now = time.monotonic()
    _REGISTRY.update(at=now, value=value, retry=False, ok_at=now)
    return value


def effective_labels(entry: Optional[Mapping[str, Any]], name: str) -> set[str]:
    return set((entry or {}).get("labels") or []) | {f"machine:{name}"}


def eligible(card: Mapping[str, Any], me: str, machines: Mapping[str, Mapping[str, Any]]) -> Optional[str]:
    """None when this machine may ask for the card's lease, else the Board's refusal reason it would get."""
    from hermes_cli.kanban_pickup_alarm_identity import refusal
    if reason := refusal(card, me):
        return reason
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
    # [daveey] A copy adopted here as an origin owner (``kanban_origin_adopt``) holds an ``origin_creation_intents``
    # row: machine-local, never synced, and the only copy of the binding secret. The collector publishes a card's
    # ``origin_bindings`` only from that table, and the origin ledger advances (or releases, once terminal) its
    # obligation only from an owner record that still carries the marker and the card's completion events/runs.
    # So the copy stays, terminal ones included: evicting it would strand the origin chat's obligation, and its
    # FK row made the DELETE raise and roll back every eviction and the whole pickup (worker-4, 11-13,
    # t_01a1d4f144e2f2a0).
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='origin_creation_intents'").fetchone():
        keep += " AND NOT EXISTS (SELECT 1 FROM origin_creation_intents i WHERE i.task_id = tasks.id)"
    rows = conn.execute(
        "SELECT id FROM tasks WHERE created_on IS NOT NULL AND created_on != ? AND claim_lock IS NULL "
        "AND status != 'running'" + keep, (own,)).fetchall()
    gone: list[str] = []
    if not rows:
        return gone
    held = _guard_held(conn, [tid for (tid,) in rows])
    from hermes_cli import kanban_replica_lag as lag  # never hand a held copy to a replica behind our push
    held |= lag.held_copies(conn, _replica_path(conn), [tid for (tid,) in rows if tid not in held])
    present = {t for t in (*_CHILD_TABLES, "task_chat_links")
               if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()}
    from hermes_cli.kanban_db_connect import write_txn
    with write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET applying = 1")
        for (tid,) in rows:
            if tid in held:
                continue
            if not conn.execute(
                    "SELECT 1 FROM tasks WHERE id=? AND created_on IS NOT NULL AND created_on != ? "
                    "AND claim_lock IS NULL AND status != 'running'" + keep, (tid, own)).fetchone():
                continue
            if conn.execute(
                    "SELECT 1 FROM kanban_sync_outbox WHERE state != 'rejected' AND (row_uid = ? OR "
                    "json_extract(fields, '$.task_id') = ?) LIMIT 1", (tid, tid)).fetchone():
                continue
            if conn.execute(
                    "SELECT 1 FROM task_attachments WHERE task_id=? AND stored_path!='' "
                    "AND coalesce(blob_key,'')='' LIMIT 1", (tid,)).fetchone():
                continue  # retain the only byte custodian until private storage acknowledges it
            # [daveey] Each copy in its own savepoint: one row that cannot go (an FK child this list does not know)
            # is kept and logged, and never rolls back the other evictions or stops the pickup after it.
            try:
                with write_txn(conn, allow_nested=True):
                    _evict_one(conn, tid, present)
            except sqlite3.Error:
                _log.warning("kanban worker node: could not evict copy %s; kept", tid, exc_info=True)
                continue
            gone.append(tid)
        conn.execute("UPDATE kanban_sync_state SET applying = 0")
    return gone


def _evict_one(conn: sqlite3.Connection, tid: str, present: set[str]) -> None:
    from hermes_cli.kanban_run_tombstones import retain_ended_runs
    from hermes_cli.kanban_scratch_custody import retain
    retain(conn, tid)
    retain_ended_runs(conn, tid)
    from hermes_cli.kanban_containment_connections import released_history_delete
    with released_history_delete(conn, tid):
        for table in present:
            conn.execute(f"DELETE FROM {table} WHERE task_id = ?", (tid,))
    # Edges to retained children belong to those children, not the evicted copy.
    conn.execute("DELETE FROM task_links WHERE child_id = ? OR (parent_id = ? AND child_id NOT IN "
                 "(SELECT id FROM tasks WHERE id != ?))", (tid, tid, tid))
    conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))


def _guard_held(conn: sqlite3.Connection, ids: list[str]) -> set[str]:
    """[daveey] Copies the local dispatcher holds in its ready/review lane (respawn guard: ``active_pr``, cooldowns,
    ...): a recorded ``respawn_guarded`` hold that the guard still confirms. Eviction keeps them. Dropping one
    re-imported it the next tick, where it took the pickup budget again and was held again: on worker-3 at 9/10
    in progress (budget 1) a held ``active_pr`` card took the only slot for ~8 min, 77 imports, while two cards
    pinned to the machine waited ``budget_exhausted`` (t_01a1ced8ec470e59). A kept copy is already here, so pickup
    spends no budget on it. It holds no lease: the Board still lets any machine lease it, and the feed keeps the
    copy current, so it is evicted once it ends, blocks or parks, and its home owns hold maintenance
    (``foreign_copy``). Computed outside the eviction write: the guard may read PR state."""
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli.kanban_db_dispatch_guard_notice import guard_hold_reason
    held: set[str] = set()
    for tid in ids:
        try:
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
            if row is None or row["status"] not in ("ready", "review"):
                continue
            if guard_hold_reason(conn, tid, include_replies=False) is None:
                continue  # the local dispatcher never held it (or a later event broke the hold)
            if dispatch.check_respawn_guard(conn, tid, lane=row["status"]) is not None:
                held.add(tid)
        except Exception:  # health: allow BLE001 -- a failed check evicts as before; the lease still decides
            _log.warning("kanban worker node: guard check for copy %s failed", tid, exc_info=True)
    return held


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
        "SELECT id, runs_on, created_on, created_by, idempotency_key, workspace_kind FROM tasks "
        "WHERE status = ? AND claim_lock IS NULL "
        "AND (runs_on IS NOT NULL OR (created_on IS NOT NULL AND created_on != ?) "
        "OR created_by = 'pickup-stall-detector' OR idempotency_key LIKE 'pickup-stall:%' "
        "OR idempotency_key LIKE 'worker-resources-unknown:%')",
        (status, own)).fetchall()
    if not rows:
        return out
    from hermes_cli.kanban_pickup_alarm_identity import refusal
    for row in rows:
        if reason := refusal(dict(row), own):
            out[row["id"]] = reason
    reg = registry()
    if not reg:
        return out
    machines = reg["machines"]
    from hermes_cli.kanban_hub_admission_pickup import refusals as unjudged
    out.update(unjudged(conn, rows, own, machines))  # [daveey] never claim an unjudged hub-bound foreign copy
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
        # Same lane order as the hub dispatcher (``DISPATCH_ORDER``): a waited-on parent first. [daveey]
        f"ORDER BY status = 'review' DESC, {DISPATCH_ORDER}, id",
        (own, now_ms)).fetchall()


def _changes_for(replica: sqlite3.Connection, tid: str, card: sqlite3.Row) -> list[sync.RemoteChange]:
    from hermes_cli import kanban_sync_client as ksc
    fields = ksc._row_fields(replica, "tasks", card)
    for column in sync.SERVER_COLUMNS["tasks"]:
        if column in card.keys():
            fields[column] = card[column]
    out = [sync.RemoteChange(0, "tasks", tid, "upsert", fields, int(card["sync_version"] or 0), "", "")]
    for table in _CHILD_TABLES:
        # Events are policy evidence, not a display tail: queue diagnostics can
        # bury an authenticated continuation (or its invalidation) arbitrarily
        # far back. Carry the whole history, in source insertion order, just as
        # runs/comments travel in full. Descending import also reverses local
        # event ids and changes consumers that read the latest event by id.
        for row in replica.execute(f"SELECT * FROM {table} WHERE task_id = ? ORDER BY id", (tid,)):
            if row["uid"] is None:
                continue
            version = int(row["sync_version"] or 0) if "sync_version" in row.keys() else 0
            # A comment's sender is machine-local (never a wire field): carry the replica's as the change's writer.
            sender = (row["origin_gateway"] or "") if table in ("task_comments", "task_events") and "origin_gateway" in row.keys() else ""
            out.append(sync.RemoteChange(0, table, row["uid"], "upsert", ksc._row_fields(replica, table, row),
                                         version, "", sender))
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
        from hermes_cli.kanban_admission_origin import record_pickup_origins
        record_pickup_origins(conn, replica, done)
    return done


def open_replica(conn: sqlite3.Connection) -> Optional[sqlite3.Connection]:
    """Read-only connection to this board's full replica, or None when there is none yet."""
    path = _replica_path(conn)
    if not path.exists():
        return None
    from hermes_cli import kanban_lockguard
    replica = kanban_lockguard.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    replica.row_factory = sqlite3.Row
    return replica


def pinned_rows(replica: sqlite3.Connection, own: str, *, now_ms: Optional[int] = None,
                statuses: tuple[str, ...] = ("ready", "review")) -> list[sqlite3.Row]:
    """Foreign ``statuses`` cards without a live lease whose ``runs_on`` names ``machine:<own>`` (parents ignored)."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return replica.execute(
        f"SELECT * FROM tasks WHERE status IN ({','.join('?' * len(statuses))}) AND created_on IS NOT NULL "
        "AND created_on != ? AND (lease_holder IS NULL OR COALESCE(lease_expires, 0) < ?) AND json_valid(runs_on) "
        "AND EXISTS (SELECT 1 FROM json_each(tasks.runs_on) WHERE value = ?)",
        (*statuses, own, now_ms, f"machine:{own}")).fetchall()


def _unimported_pinned(conn: sqlite3.Connection, replica: sqlite3.Connection, own: str) -> set[str]:
    try:
        return {r["id"] for r in pinned_rows(replica, own)
                if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (r["id"],)).fetchone()}
    except Exception:  # health: allow BLE001 -- pickup evidence must never stop a pickup
        _log.warning("kanban worker node: could not list pinned cards for pickup evidence", exc_info=True)
        return set()


@pickup_batch()
def _evaluate(conn: sqlite3.Connection, replica: sqlite3.Connection, own: str, *, mode: str, limit: Any,
              profile_exists: Optional[Callable[[str], bool]], full_detail: str,
              held: Optional[dict[str, tuple]] = None,
              ) -> tuple[list[str], dict[str, tuple[str, Optional[str]]]]:
    """One pass over the candidates: ids to copy (at most ``limit``, in candidate order) and, for each card pinned
    to this machine that the pass leaves on the Board, why. ``budget_exhausted`` only for a card this pass would
    otherwise have copied, so a refusal is never hidden behind a full queue. A card the Board shows held by a
    respawn guard never takes the budget (``kanban_pickup_held``): with ``held`` it is copied outside ``limit``
    (its id and hold signature added to ``held``), else it is left as ``guard_held`` (never ``hold``: that
    one is this machine's registry hold)."""
    from hermes_cli import kanban_pickup_held as kph
    from hermes_cli.kanban_db_dispatch import review_dispatch_enabled
    mine = _unimported_pinned(conn, replica, own)
    reg = registry()
    if not reg:
        return [], {tid: ("other", "machine registry unavailable") for tid in mine}
    review_enabled = review_dispatch_enabled()
    from hermes_cli.kanban_dispatch_priority import order_with_remote_waiters
    listed = candidates(replica, own)
    # Rank the current ready/review episode before spending the import budget.
    # A hard pinned-first partition would starve pool work on a busy executor;
    # source-aware aging must compare both groups. Eligibility stays authoritative.
    listed = order_with_remote_waiters(conn, listed)
    skips: dict[str, tuple[str, Optional[str]]] = {
        tid: ("parents_incomplete", "a parent is not done") for tid in mine - {c["id"] for c in listed}}
    picked: list[str] = []
    free = 0  # guard-held copies take no budget: they `continue` before room.take
    paced: dict[str, int] = {}  # provider -> cards this pass took against its pacing limit (_paced)
    from hermes_cli.kanban_admission_pools import PoolBudget, row_class
    room = PoolBudget(limit.regular, limit.priority, limit.limit) if isinstance(limit, PoolBudget) else \
        PoolBudget(limit, 0)  # an int is a classless budget; a hub's pool budget never copies unstartable work
    for card in listed:
        full = not room.admits(row_class(card))
        if full and card["id"] not in mine:
            continue
        reason = _skip_reason(conn, card, own, reg["machines"], mode=mode, review_enabled=review_enabled,
                              profile_exists=profile_exists)
        if reason is None:
            reason = _quota_held(replica, card)
        admitted: list[str] = []
        if reason is None:
            reason = _paced(conn, replica, card, paced, admitted)
        hold = kph.board_hold(replica, card) if reason is None else None
        if hold is not None:
            if held is not None and free < kph.FREE_PER_TICK and not kph.recently_copied(card["id"], hold[1]):
                held[card["id"]] = hold[1]
                picked.append(card["id"])
                free += 1
                continue
            reason = ("guard_held", f"respawn_guarded: {hold[0]}")
        if reason is None and full:
            reason = ("budget_exhausted", full_detail)
        if reason is None:
            picked.append(card["id"])
            room.take(row_class(card))
            for provider in admitted:
                paced[provider] = paced.get(provider, 0) + 1
        elif reason[0] and card["id"] in mine:
            skips[card["id"]] = reason
    return picked, skips


def _paced(conn: sqlite3.Connection, replica: sqlite3.Connection, card: sqlite3.Row,
           picked: Optional[dict[str, int]] = None, admitted: Optional[list[str]] = None,
           ) -> Optional[tuple[str, Optional[str]]]:
    """[daveey] ``("other", detail)`` when this machine's dispatcher would pace-hold ``card`` (provider at or over its
    ``kanban.llm_pacing`` limit here) right now. Such a card is not offered, so it takes no pickup budget and the
    window fills with the next cards that can run; it is offered again on the tick its limit frees
    (t_01a1ddeb76e30be7: 9 paced codex cards hid a runnable anthropic card). ``picked`` counts the cards this pass
    already took per provider against that limit; ``admitted`` receives the limited provider that admits ``card``
    (t_01a1e14333fada79: limit 1, running 0, one pass copied 10 anthropic cards and a card pinned here below them
    read ``budget_exhausted``)."""
    from hermes_cli.kanban_llm_pacing_dispatch import pickup_hold
    held = pickup_hold(conn, replica, card, picked=picked, admitted=admitted)
    if held is not None:
        return "other", f"llm_paced: {held} (local dispatch would hold it)"
    from hermes_cli.kanban_start_gate import hold as start_gate_hold  # a plugin holds this machine's card starts
    gated = start_gate_hold(task_id=card["id"], lane=card["status"], assignee=card["assignee"], board=None)
    return None if gated is None else ("other", f"{gated} (local dispatch would hold it)")


def _quota_held(replica: sqlite3.Connection, card: sqlite3.Row) -> Optional[tuple[str, Optional[str]]]:
    """[daveey] ``("quota_hold", detail)`` when the local dispatcher would hold ``card`` on its quota episode, judged
    before the copy: the same read-only :func:`kanban_quota_episode.decision` ``check_respawn_guard`` returns for a
    card with an episode, on the card's own runs/events in the replica (a copy carries exactly those) and this
    machine's provider walls. Such a card would only be copied, held, evicted and copied again each tick, while
    taking the pickup budget: worker-3 2026-11-12, 4 cards walled until 11-17/18 filled the batch and two
    priority-0 cards pinned to it waited ``budget_exhausted`` (t_01a1cf9f2b838cbb). Not copied, it spends no
    budget; an episode with a free alternate route (``decision`` -> None) is copied as before. Any failure copies
    as before: the local guard still decides after the copy.

    With no episode (the card never ran), the pre-claim ``pinned_quota_wall`` hold
    (:func:`kanban_dispatch_skip.pinned_quota_hold`): worker-1 2026-11-14, a fresh mandatory pin on a walled model
    was copied, held and evicted ~700 times in 3 h while a card pinned here waited (t_01a1d95eabbd2b9d)."""
    from hermes_cli.kanban_quota_episode import decision, episode, release_at
    try:
        if not episode(replica, card["id"]):
            return _pin_walled(replica, card)
        from hermes_cli import kanban_db as kb
        task = kb.get_task(replica, card["id"])
        guard = decision(replica, task, lane=card["status"])[0] if task is not None else None
        if guard is None:
            return None
        until = release_at(replica, task, lane=card["status"])
    except Exception:  # health: allow BLE001 -- a hold prediction must never stop a pickup
        _log.warning("kanban worker node: quota hold check for %s failed", card["id"], exc_info=True)
        return None
    from datetime import UTC, datetime
    when = f" until {datetime.fromtimestamp(until, UTC):%Y-%m-%dT%H:%M:%SZ}" if until else ""
    # Not "hold": that groups with a registry hold of the whole machine (kanban_pickup_stall._HOST_GROUPS).
    return "quota_hold", f"{guard}{when} (local dispatch would hold it)"


def _pin_walled(replica: sqlite3.Connection, card: sqlite3.Row) -> Optional[tuple[str, Optional[str]]]:
    """[daveey] ``("quota_hold", detail)`` when local dispatch would hold the never-run ``card`` pre-claim on its
    pin's live quota wall (``hold_pinned_quota``: ready lane only, read-only, same inputs on the replica)."""
    from hermes_cli.kanban_dispatch_skip import PINNED_QUOTA_WALL, pinned_quota_hold
    held = pinned_quota_hold(replica, card["id"]) if card["status"] == "ready" else None
    if held is None:
        return None
    from datetime import UTC, datetime
    model, provider, until = held
    return "quota_hold", (f"{PINNED_QUOTA_WALL} {model} ({provider}) until "
                          f"{datetime.fromtimestamp(until, UTC):%Y-%m-%dT%H:%M:%SZ} (local dispatch would hold it)")


def note_capped(conn: sqlite3.Connection, profile_exists: Optional[Callable[[str], bool]] = None) -> None:
    """A tick with no spawn budget runs no pickup: record why this machine's pinned cards wait (``budget_exhausted``
    for the ones a slot would start). Evidence only (``kanban_pickup_stall``); copies nothing."""
    mode = pickup_mode()
    if sync.board_mode(conn) != "primary" or mode is None:
        return
    replica = open_replica(conn)
    if replica is None:
        return
    try:
        _, skips = _evaluate(conn, replica, _own(conn), mode=mode, limit=0, profile_exists=profile_exists,
                             full_detail="no spawn slot this tick")
    finally:
        replica.close()
    _record_skips(conn, skips)


def pickup(conn: sqlite3.Connection, *, budget: Any,
           profile_exists: Optional[Callable[[str], bool]] = None) -> list[str]:
    """One tick's pickup: evict finished copies, then copy up to ``budget`` eligible foreign cards. Returns the
    ids copied. A no-op unless the board is primary and ``kanban.sync.pickup`` is on. Each card pinned to this
    machine that the pass does not import gets its reason recorded (``kanban_pickup_stall.record``)."""
    mode = pickup_mode()
    if sync.board_mode(conn) != "primary" or mode is None:
        return []
    try:
        evict(conn)
    except Exception:  # health: allow BLE001 -- [daveey] a failed eviction must never stop the pickup that follows
        _log.warning("kanban worker node: evicting finished copies failed; picking up anyway", exc_info=True)
    _refill_copies(conn)
    limit = DEFAULT_PICKUP_BATCH if budget is None else budget
    if getattr(limit, "total", limit) <= 0:
        note_capped(conn, profile_exists)
        return []
    replica = open_replica(conn)
    if replica is None:
        return []
    from hermes_cli import kanban_hub_admission_pickup as admission
    from hermes_cli import kanban_pickup_held as kph
    own = _own(conn)
    held: dict[str, tuple] = {}
    try:
        picked, skips = _evaluate(conn, replica, own, mode=mode, limit=limit, profile_exists=profile_exists,
                                  full_detail="pickup batch full this tick", held=held)
        from hermes_cli import kanban_replica_lag as lag  # copied back next tick, once the replica holds our push
        behind = lag.lagging(_replica_path(conn), picked)
        skips.update({tid: ("other", "replica behind this gateway's push") for tid in behind})
        # [daveey] The hub judges a hub-bound foreign card before copying it (t_01a1cf65a941bf43).
        picked, plans, judged = admission.screen(conn, replica, own, [tid for tid in picked if tid not in behind])
        skips.update(judged)
        imported = import_cards(conn, replica, picked)
    finally:
        replica.close()
    denied = admission.apply(conn, own, plans, imported)
    skips.update({tid: ("other", "import failed") for tid in picked if tid not in imported})
    skips.update(denied)
    imported = [tid for tid in imported if tid not in denied]
    kph.remember({tid: sig for tid, sig in held.items() if tid in imported})
    _record_skips(conn, {tid: why for tid, why in skips.items() if tid not in imported})
    if imported:
        guarded = [tid for tid in imported if tid in held]
        _log.info("kanban worker node: picked up %d card(s) from other gateways: %s%s", len(imported), imported,
                  f" ({len(guarded)} held on the Board, outside the budget: {guarded})" if guarded else "")
    return imported


def _refill_copies(conn: sqlite3.Connection) -> None:
    """Refill the event prefix a pre-full-history pickup cut off each local copy (``kanban_pickup_history``)."""
    try:
        replica = open_replica(conn)
        if replica is None:
            return
        try:
            from hermes_cli.kanban_pickup_history import refill
            filled = {t: r for t, r in refill(conn, replica, _own(conn)).items() if r["filled"]}
        finally:
            replica.close()
    except Exception:  # health: allow BLE001 -- a history repair must never stop a pickup
        _log.warning("kanban worker node: could not refill copied card history", exc_info=True)
        return
    if filled:
        _log.info("kanban worker node: refilled truncated history from the replica: %s", filled)


def _record_skips(conn: sqlite3.Connection, skips: dict[str, tuple[str, Optional[str]]]) -> None:
    from hermes_cli import kanban_pickup_stall as kps
    try:
        kps.record(conn, skips)
    except Exception:  # health: allow BLE001 -- pickup evidence must never undo or stop a pickup
        _log.warning("kanban worker node: could not record pickup skips", exc_info=True)


def _skip_reason(conn: sqlite3.Connection, card: sqlite3.Row, own: str, machines: Mapping[str, Mapping[str, Any]], *,
                 mode: str, review_enabled: bool,
                 profile_exists: Optional[Callable[[str], bool]]) -> Optional[tuple[str, Optional[str]]]:
    """None when this machine may copy ``card``; else ``(reason, detail)`` (reason ``""``: nothing to record)."""
    from hermes_cli.kanban_pickup_gate import decide
    return decide(card, conn, own, machines, mode=mode, review_enabled=review_enabled,
                  profile_exists=profile_exists, placement=eligible)
