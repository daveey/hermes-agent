"""Operator release of one quarantined receipt: acceptance plus certified catch-up, never a bare delete. [daveey]

``hermes kanban sync quarantine --release RECEIPT --reason TEXT`` (``release_board``) runs under the board's sync
lock, the replica (shadow) first and then the live file, so a sync round can neither copy the hold back nor run
between the two. For each file, :func:`release`:

a. fetches the Board's current evidence for the receipt (and, for a release receipt, its stop receipt) and refuses
   unless it is live and certifies: a non-live or unavailable copy keeps the hold;
b. appends a ``containment_sync_acceptances`` row (receipt, sha256 of the accepted certified identity, the evidence,
   reason, operator, time): the durable audit record. ``remember`` then treats exactly that evidence as matching;
   the first receipt row stays and nothing is erased;
c. replays, in seq order, the receipt's canonical event (built from that evidence) and every withheld row the
   release frees, through ``certify_page`` and ``apply_remote_changes`` (version/HLC fences, local pending edits
   win). Protected events are replayed from the Board's current evidence, not the stale feed copy. The holds are
   deleted inside that apply transaction, so a refused certify or a failed apply leaves every hold in place. A
   freed event whose task another receipt still holds stays withheld for that receipt's release.

The live file of a primary board replays its own partition; any other live file only lifts the holds (it holds
no feed rows of its own and takes the replica's state through pickup, which certifies against the acceptance).
"""
from __future__ import annotations

import getpass
import hashlib
import json
import logging
import time
from pathlib import Path

from hermes_cli.kanban_containment_lifecycle import ContainmentRefused

log = logging.getLogger(__name__)
_TABLE = "containment_sync_acceptances"


class ReleaseRefused(ContainmentRefused):
    """The receipt stays quarantined: the Board's evidence is not live and certified, or the catch-up failed."""


def digest(raw: str) -> str:
    """sha256 of the certified identity of one evidence response (row bookkeeping and transport keys excluded)."""
    from hermes_cli.kanban_containment_replica import _certified
    return hashlib.sha256(json.dumps(_certified(raw)).encode()).hexdigest()


def _has_table(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)).fetchone() is not None


def accepted(conn, uid: str, raw: str) -> bool:
    """An operator accepted exactly this evidence for ``uid``."""
    return _has_table(conn) and conn.execute(f"SELECT 1 FROM {_TABLE} WHERE receipt_uid=? AND sha256=? LIMIT 1",
                                             (uid, digest(raw))).fetchone() is not None


def accepted_evidence(conn, uid: str) -> dict | None:
    """The newest accepted evidence for ``uid``, or None."""
    if not _has_table(conn):
        return None
    row = conn.execute(f"SELECT evidence FROM {_TABLE} WHERE receipt_uid=? ORDER BY accepted_at DESC, rowid DESC "
                       "LIMIT 1", (uid,)).fetchone()
    return json.loads(row[0]) if row else None


def _raw(evidence: dict) -> str:
    return json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _fetch(hub, board: str, uid: str) -> dict:
    try:
        return hub.row_evidence(board, "task_events", uid)
    except Exception as exc:  # health: allow BLE001 -- any fetch failure means "unavailable": the hold stays
        raise ReleaseRefused(f"Board evidence for {uid} is unavailable ({type(exc).__name__}: {exc})") from exc


def _live_chain(hub, board: str, change) -> list[tuple[str, dict]]:
    """``[(uid, evidence)]`` for the receipt (its stop receipt first), each live and certified now."""
    from hermes_cli import kanban_containment_canonical as canonical
    from hermes_cli.kanban_containment_state import _payload
    task = change.fields.get("task_id")
    evidence = _fetch(hub, board, change.uid)
    try:
        if change.fields.get("kind") == "containment_released":
            episode = str((_payload(change.fields) or {}).get("episode"))
            stop = _fetch(hub, board, episode)
            canonical.certified_release(stop, evidence, board=board, task=task, episode=episode, receipt=change.uid)
            return [(episode, stop), (change.uid, evidence)]
        canonical.certified_stop(evidence, board=board, task=task, uid=change.uid)
    except ReleaseRefused:
        raise
    except ContainmentRefused as exc:
        raise ReleaseRefused(f"Board evidence for {change.uid} is not live and certified: {exc}") from exc
    return [(change.uid, evidence)]


def _accept(conn, chain, receipt: str, reason: str, operator: str, now: float) -> None:
    from hermes_cli import kanban_containment_replica as replica
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_sync_quarantine as quarantine
    with kb.write_txn(conn):
        quarantine.install(conn)
        for uid, evidence in chain:
            raw = _raw(evidence)
            first = conn.execute("SELECT evidence FROM containment_canonical_receipts WHERE uid=?", (uid,)).fetchone()
            if uid != receipt and (first is None or replica._certified(first[0]) == replica._certified(raw)):
                continue  # the released receipt always; a stop receipt only when its identity changed too
            conn.execute(f"INSERT OR IGNORE INTO {_TABLE} VALUES (?,?,?,?,?,?)",
                         (uid, digest(raw), raw, reason, operator, int(now)))


def _free(conn, change, blocked: set[str]) -> bool:
    from hermes_cli import kanban_sync_quarantine as quarantine
    try:
        names = quarantine.subjects(conn, change.table, change.uid, change.fields)
    except ContainmentRefused:
        return False
    return bool(names and not names & blocked) or (not names and not blocked)


def _canonical(hub, board: str, chosen: list) -> list:
    """Protected events from the Board's current evidence (one per uid), the rest as withheld; in seq order."""
    from hermes_cli import kanban_sync_client as ksc
    from hermes_cli import kanban_sync_quarantine as quarantine
    events, rows = {}, []
    for ch in sorted(chosen, key=lambda c: c.seq):
        if quarantine.protected(ch) and ch.op == "upsert":
            events[ch.uid] = ksc._row_change("task_events", _fetch(hub, board, ch.uid)["row"], seq=ch.seq)
        else:
            rows.append(ch)
    return sorted(rows + list(events.values()), key=lambda c: c.seq)


def _apply(conn, hub, board: str, receipt: str, changes: list, replayed: list[str], deferred: list[str],
           observers: bool) -> None:
    from hermes_cli import kanban_containment_replica as replica
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_quarantine as quarantine
    if changes:
        if sync.board_mode(conn) == "off":
            raise ReleaseRefused("kanban sync is not enabled on this file, so nothing can replay; the hold stays")
        try:
            replica.certify_page(conn, hub, board, changes)
        except ContainmentRefused as exc:
            raise ReleaseRefused(f"the catch-up did not certify ({exc}); the hold stays") from exc
        lifted: list[bool] = []

        def lift_first(c, _change) -> bool:  # runs inside the apply transaction, before the first row applies
            if not lifted:
                quarantine.lift(c, receipt, replayed, deferred)
                lifted.append(True)
            return True

        cursor = int((sync.sync_state(conn) or {}).get("cursor") or 0)
        sync.apply_remote_changes(conn, changes, new_cursor=cursor, board=board, observers=observers,
                                  local_pending_wins=True, keep=lift_first)
        if lifted:
            return
    with kb.write_txn(conn):
        quarantine.lift(conn, receipt, replayed, deferred)


def release(conn, hub, board: str, receipt: str, *, reason: str, operator: str, partition: str | None = None,
            replay: bool = True, observers: bool = False, now: float | None = None) -> dict | None:
    """Release ``receipt`` in this file (module docstring). None when this file does not quarantine it; raises
    :class:`ReleaseRefused` (every hold kept) when the Board's evidence is not live or the catch-up fails."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as ksc
    from hermes_cli import kanban_sync_quarantine as quarantine
    if receipt not in quarantine.receipts(conn):
        return None
    raw = conn.execute("SELECT wire FROM containment_sync_quarantine WHERE uid=?", (receipt,)).fetchone()[0]
    change = sync.RemoteChange(**json.loads(raw))
    chain = _live_chain(hub, board, change)
    _accept(conn, chain, receipt, reason, operator, time.time() if now is None else now)
    own = ksc._row_change("task_events", chain[-1][1]["row"], seq=change.seq)
    blocked = quarantine.tasks(conn, without=receipt)
    replayed, chosen, deferred = [], [], []
    if replay:
        for (text,) in conn.execute("SELECT wire FROM containment_sync_withheld").fetchall():
            ch = sync.RemoteChange(**json.loads(text))
            if _free(conn, ch, blocked):
                replayed.append(text)
                chosen.append(ch)
        if _free(conn, own, blocked):
            chosen.append(own)
        else:
            deferred.append(quarantine.wire(own))
    changes = _canonical(hub, board, chosen)
    if partition is not None:
        changes = ksc.own_partition(conn, changes, partition)
    _apply(conn, hub, board, receipt, changes, replayed, deferred, observers)
    log.warning("kanban sync: released quarantine receipt %s on %s by %s (%s): %d row(s) replayed, %d deferred",
                receipt, board, operator, reason, len(changes), len(deferred))
    return {"receipt": receipt, "released": True, "replayed": len(changes), "deferred": len(deferred),
            "still_held": sorted(quarantine.tasks(conn))}


def _operator(gateway: str) -> str:
    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        user = "unknown"
    return f"{user}@{gateway}"


def release_board(slug: str, live_path: Path, receipt: str, reason: str) -> tuple[int, str]:
    """The CLI's release: (exit code, message). 0 released, 1 no such receipt here, 2 refused or busy."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as ksc
    from hermes_cli import kanban_sync_guard as guard
    ident = ksc.load_identity()
    hub, operator = ksc.Hub(ident), _operator(ident.gateway)
    done: list[tuple[str, dict]] = []
    try:
        with guard.board_sync_lock(live_path):
            shadow = ksc.shadow_path(slug)
            if shadow.exists():
                conn = ksc.open_shadow(slug, ident.gateway)
                try:
                    if result := release(conn, hub, slug, receipt, reason=reason, operator=operator):
                        done.append(("replica", result))
                finally:
                    conn.close()
            conn = kbc.connect(live_path)
            try:
                primary = sync.board_mode(conn) == "primary"
                if result := release(conn, hub, slug, receipt, reason=reason, operator=operator,
                                     partition=ident.gateway if primary else None, replay=primary, observers=primary):
                    done.append(("live", result))
            finally:
                conn.close()
    except guard.SyncRoundBusy as exc:
        return 2, f"kanban sync: {receipt} not released: a sync round holds board {slug} ({exc}); retry shortly"
    except ReleaseRefused as exc:
        partial = f" (already released in: {', '.join(name for name, _ in done)}; rerun to finish)" if done else ""
        return 2, f"kanban sync: {receipt} not released: {exc}{partial}"
    if not done:
        return 1, f"kanban sync: no quarantined receipt {receipt} on board {slug}"
    replayed = sum(r["replayed"] for _, r in done)
    still = sorted({t for _, r in done for t in r["still_held"]})
    return 0, (f"kanban sync: released {receipt} ({reason}) in {', '.join(name for name, _ in done)}: accepted the "
               f"Board's live evidence (recorded in {_TABLE}) and applied {replayed} withheld row(s) through "
               "certify+apply" + (f"; still held by other receipts: {', '.join(still)}" if still else ""))
