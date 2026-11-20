"""Operator release of one quarantined receipt: acceptance plus certified catch-up, never a bare delete. [daveey]

``hermes kanban sync quarantine --release RECEIPT --reason TEXT`` (``release_board``) runs under the board's sync
lock, the replica (shadow) first and then the live file, so a sync round can neither copy the hold back nor run
between the two. For each file, :func:`release`:

a. fetches the Board's current evidence for the receipt (and, for a release receipt, its stop receipt) and refuses
   unless it is live and certifies: a non-live or unavailable copy keeps the hold;
b. selects the catch-up: the receipt's canonical event (built from that evidence) and every withheld row the release
   frees, in withhold order (epoch, then seq). Protected events replay from the Board's current evidence, not the
   stale feed copy. Every evidence response is fetched here, before any write transaction. A freed protected event
   (other than this receipt's own chain) whose evidence no longer certifies is quarantined under its own receipt
   (its task stays held, its owner card is filed, the CLI names it) and the selection is redone;
c. in ONE apply transaction: appends the ``containment_sync_acceptances`` row (receipt, sha256 of the accepted
   certified identity, the evidence, reason, operator, time; the durable audit record that ``remember`` honours
   while the first receipt row stays), certifies the catch-up against the prefetched evidence, applies it through
   ``apply_remote_changes`` (version/HLC fences, local pending edits win) and deletes the receipt's holds. A refused
   certify or a failed apply rolls all of it back: no acceptance, every hold intact.

A primary board's live file replays its own partition. Any other live file holds no feed rows: it takes the
replica's current certified rows of each freed task it holds (the pickup path, ``adopt_receipts``) in that same
transaction, so its copy is never claimable between the lift and the next repair; the replica must be released
first (``kanban_sync_quarantine_release_live``). Its withheld rows of the freed tasks are deleted in that lift, never
replayed. A freed event whose task another receipt still holds stays withheld for that receipt's release. A primary
live file quarantines a newly uncertifiable event under the replica's first-seen stamp: one owner card per host.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path

from hermes_cli.kanban_containment_lifecycle import ContainmentConflict, ContainmentRefused

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


class _Evidence:
    """The Board's evidence, fetched once per uid before the write transaction; ``frozen`` refuses a new fetch."""

    def __init__(self, hub):
        self.hub, self.seen, self.frozen = hub, {}, False

    def row_evidence(self, board: str, table: str, uid: str) -> dict:
        if (table, uid) not in self.seen:
            if self.frozen:
                raise ReleaseRefused(f"evidence for {uid} was not fetched before the catch-up transaction")
            try:
                self.seen[(table, uid)] = self.hub.row_evidence(board, table, uid)
            except Exception as exc:  # health: allow BLE001 -- any fetch failure means "unavailable": the hold stays
                raise ReleaseRefused(f"Board evidence for {uid} is unavailable ({type(exc).__name__}: {exc})") from exc
        return json.loads(json.dumps(self.seen[(table, uid)]))


def _episode(change) -> str | None:
    from hermes_cli.kanban_containment_state import _payload
    return str((_payload(change.fields) or {}).get("episode")) if change.fields.get("kind") == "containment_released" else None


def _live_chain(evidence: _Evidence, board: str, change) -> list[tuple[str, dict]]:
    """``[(uid, evidence)]`` for the receipt (its stop receipt first), each live and certified now."""
    from hermes_cli import kanban_containment_canonical as canonical
    task = change.fields.get("task_id")
    own = evidence.row_evidence(board, "task_events", change.uid)
    try:
        if (episode := _episode(change)) is not None:
            stop = evidence.row_evidence(board, "task_events", episode)
            canonical.certified_release(stop, own, board=board, task=task, episode=episode, receipt=change.uid)
            return [(episode, stop), (change.uid, own)]
        canonical.certified_stop(own, board=board, task=task, uid=change.uid)
    except ReleaseRefused:
        raise
    except ContainmentRefused as exc:
        raise ReleaseRefused(f"Board evidence for {change.uid} is not live and certified: {exc}") from exc
    return [(change.uid, own)]


def _accept(conn, chain, receipt: str, reason: str, operator: str, now: float) -> None:
    """Inside the catch-up transaction: the acceptance rows (the receipt always; a stop receipt only when its
    identity changed too)."""
    from hermes_cli import kanban_containment_replica as replica
    from hermes_cli import kanban_sync_quarantine as quarantine
    quarantine.install(conn)
    for uid, evidence in chain:
        raw = _raw(evidence)
        first = conn.execute("SELECT evidence FROM containment_canonical_receipts WHERE uid=?", (uid,)).fetchone()
        if uid != receipt and (first is None or replica._certified(first[0]) == replica._certified(raw)):
            continue
        conn.execute(f"INSERT OR IGNORE INTO {_TABLE} VALUES (?,?,?,?,?,?)",
                     (uid, digest(raw), raw, reason, operator, int(now)))


def _free(conn, change, blocked: set[str]) -> bool:
    from hermes_cli import kanban_sync_quarantine as quarantine
    try:
        names = quarantine.subjects(conn, change.table, change.uid, change.fields)
    except ContainmentRefused:
        return False
    return bool(names and not names & blocked) or (not names and not blocked)


def _select(conn, evidence: _Evidence, board: str, receipt: str, own, partition: str | None):
    """(catch-up changes, replayed wires, deferred wires): freed rows in withhold order, protected ones canonical."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as ksc
    from hermes_cli import kanban_sync_quarantine as quarantine
    blocked = quarantine.tasks(conn, without=receipt)
    chosen, replayed, deferred, own_at = [], [], [], 0
    for text, epoch in conn.execute("SELECT wire,ord FROM containment_sync_withheld").fetchall():
        ch = sync.RemoteChange(**json.loads(text))
        own_at = epoch if ch.uid == own.uid and not own_at else own_at
        if _free(conn, ch, blocked):
            replayed.append(text)
            chosen.append((epoch, ch))
    if _free(conn, own, blocked):
        chosen.append((own_at, own))
    else:
        deferred.append(quarantine.wire(own))
    events, rows = {}, []
    for epoch, ch in sorted(chosen, key=lambda item: (item[0], item[1].seq)):
        if quarantine.protected(ch) and ch.op == "upsert":
            row = evidence.row_evidence(board, "task_events", ch.uid)["row"]
            if (episode := _episode(ch)) is not None:
                evidence.row_evidence(board, "task_events", episode)
            events.pop(ch.uid, None)
            events[ch.uid] = (epoch, ksc._row_change("task_events", row, seq=ch.seq))
        else:
            rows.append((epoch, ch))
    changes = [ch for _, ch in sorted(rows + list(events.values()), key=lambda item: (item[0], item[1].seq))]
    if partition is not None:
        changes = ksc.own_partition(conn, changes, partition)
    return changes, replayed, deferred


def _quarantine_uncertifiable(conn, evidence: _Evidence, board: str, chain_uids: set[str], changes,
                              seen=None) -> list[str]:
    """Certify each freed protected event outside this receipt's chain; one that no longer certifies is
    quarantined under its own receipt. Its uids, in order."""
    from hermes_cli import kanban_containment_replica as replica
    from hermes_cli import kanban_sync_quarantine as quarantine
    from hermes_cli.kanban_sync_quarantine_release_live import first_seen
    found = []
    for ch in changes:
        if not quarantine.protected(ch) or ch.uid in chain_uids or _episode(ch) in chain_uids:
            continue  # this receipt's chain certifies only after its acceptance, inside the transaction
        try:
            replica.certify_page(conn, evidence, board, [ch])
        except ReleaseRefused:
            raise
        except ContainmentRefused as exc:
            conflict = exc if isinstance(exc, ContainmentConflict) else ContainmentConflict(
                f"no longer certifiable at release: {exc}", kind="uncertifiable", uid=ch.uid)
            quarantine.quarantine_change(conn, board, ch, conflict, at=first_seen(seen, ch.uid))
            found.append(ch.uid)
    return found


def _commit(conn, evidence: _Evidence, board: str, receipt: str, changes, replayed, deferred, accept,
            source, observers: bool) -> None:
    """One transaction: acceptance, certification, apply and lift; any failure rolls back all of it."""
    from hermes_cli import kanban_containment_replica as replica
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_quarantine as quarantine
    evidence.frozen = True

    def commit_first(c) -> None:
        accept(c)
        if source is not None:
            replica.adopt_receipts(c, source, changes)  # the pickup path: certified from the replica's receipts
        else:
            replica.certify_page(c, evidence, board, changes)
        quarantine.lift(c, receipt, replayed, deferred)

    if not changes:
        with kb.write_txn(conn):
            commit_first(conn)
        return
    if sync.board_mode(conn) == "off":
        raise ReleaseRefused("kanban sync is not enabled on this file, so nothing can replay; the hold stays")
    done: list[bool] = []

    def keep(c, _change) -> bool:  # inside the apply transaction, before the first row applies
        if not done:
            commit_first(c)
            done.append(True)
        return True

    cursor = int((sync.sync_state(conn) or {}).get("cursor") or 0)
    try:
        sync.apply_remote_changes(conn, changes, new_cursor=cursor, board=board, observers=observers,
                                  local_pending_wins=True, keep=keep)
    except ReleaseRefused:
        raise
    except ContainmentRefused as exc:
        raise ReleaseRefused(f"the catch-up did not certify ({exc}); the hold stays") from exc


def release(conn, hub, board: str, receipt: str, *, reason: str, operator: str, partition: str | None = None,
            replay: bool = True, source=None, observers: bool = False, now: float | None = None,
            seen=None) -> dict | None:
    """Release ``receipt`` in this file (module docstring). None when this file does not quarantine it; raises
    :class:`ReleaseRefused` (every hold and no acceptance kept) when the evidence is not live or the catch-up is
    refused; an apply-time sqlite3.Error/RuntimeError propagates after the same rollback. ``replay=False`` with
    ``source`` (the replica) is a non-primary live file; ``seen`` (the replica, already released) lends its
    first-seen stamps to events this file newly quarantines."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as ksc
    from hermes_cli import kanban_sync_quarantine as quarantine
    if receipt not in quarantine.receipts(conn):
        return None
    raw = conn.execute("SELECT wire FROM containment_sync_quarantine WHERE uid=?", (receipt,)).fetchone()[0]
    change = sync.RemoteChange(**json.loads(raw))
    evidence = _Evidence(hub)
    chain = _live_chain(evidence, board, change)
    own = ksc._row_change("task_events", chain[-1][1]["row"], seq=change.seq)
    stamp = time.time() if now is None else now
    newly: list[str] = []
    replayed, deferred = [], []
    if replay:
        while True:
            changes, replayed, deferred = _select(conn, evidence, board, receipt, own, partition)
            found = _quarantine_uncertifiable(conn, evidence, board, {uid for uid, _ in chain}, changes, seen)
            if not found:
                break
            newly += found  # their tasks are held now: select again
    else:
        from hermes_cli.kanban_sync_quarantine_release_live import replica_rows
        changes, replayed = replica_rows(conn, source, receipt, board, ReleaseRefused)
    _commit(conn, evidence, board, receipt, changes, replayed, deferred,
            lambda c: _accept(c, chain, receipt, reason, operator, stamp), None if replay else source, observers)
    log.warning("kanban sync: released quarantine receipt %s on %s by %s (%s): %d row(s) applied, %d deferred, "
                "newly quarantined %s", receipt, board, operator, reason, len(changes), len(deferred), newly)
    return {"receipt": receipt, "released": True, "replayed": len(changes), "deferred": len(deferred),
            "quarantined": newly, "still_held": sorted(quarantine.tasks(conn))}


def _operator(gateway: str) -> str:
    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        user = "unknown"
    return f"{user}@{gateway}"


def _release_files(slug, live_path, receipt, reason, ident, hub, done) -> None:
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as ksc
    from hermes_cli import kanban_sync_quarantine as quarantine
    operator = _operator(ident.gateway)
    shadow = ksc.shadow_path(slug)
    replica = ksc.open_shadow(slug, ident.gateway) if shadow.exists() else None
    try:
        if replica is not None and (result := release(replica, hub, slug, receipt, reason=reason, operator=operator)):
            done.append(("replica", result))
            if result["quarantined"]:
                quarantine.notify(replica, slug)
        conn = kbc.connect(live_path)
        try:
            primary = sync.board_mode(conn) == "primary"
            if result := release(conn, hub, slug, receipt, reason=reason, operator=operator,
                                 partition=ident.gateway if primary else None, replay=primary,
                                 source=None if primary else replica, observers=primary, seen=replica):
                done.append(("live", result))
                if result["quarantined"]:
                    quarantine.notify(conn, slug)
        finally:
            conn.close()
    finally:
        if replica is not None:
            replica.close()


def release_board(slug: str, live_path: Path, receipt: str, reason: str) -> tuple[int, str]:
    """The CLI's release: (exit code, message). 0 released, 1 no such receipt, any other containment refusal
    (e.g. a quarantine table shape, another board's file) or an apply-time database/runtime error (holds kept), 2 a
    refused release (``ReleaseRefused``) or busy."""
    from hermes_cli import kanban_sync_client as ksc
    from hermes_cli import kanban_sync_guard as guard
    from hermes_cli.kanban_sync_quarantine import release_command
    from hermes_cli.kanban_sync_quarantine_release_live import refused_suffix
    ident = ksc.load_identity()
    hub = ksc.Hub(ident)
    done: list[tuple[str, dict]] = []
    try:
        with guard.board_sync_lock(live_path):
            _release_files(slug, live_path, receipt, reason, ident, hub, done)
    except guard.SyncRoundBusy as exc:
        return 2, f"kanban sync: {receipt} not released: a sync round holds board {slug} ({exc}); retry shortly"
    except ReleaseRefused as exc:
        return 2, f"kanban sync: {receipt} not released: {exc}{refused_suffix(slug, done)}"
    except ContainmentRefused as exc:  # any other refusal (a quarantine table shape, another board's file, an
        # adopt or certify step): a clear error, never a traceback; its transaction rolled back, so the holds stay
        return 1, (f"kanban sync: {receipt} not released: {type(exc).__name__}: {exc}; every hold is kept"
                   f"{refused_suffix(slug, done)}")
    except (sqlite3.Error, RuntimeError) as exc:
        return 1, (f"kanban sync: {receipt} not released: the catch-up failed to apply ({type(exc).__name__}: "
                   f"{exc}); it was rolled back, every hold is kept{refused_suffix(slug, done)}")
    if not done:
        return 1, f"kanban sync: no quarantined receipt {receipt} on board {slug}"
    replayed = sum(r["replayed"] for _, r in done)
    still = sorted({t for _, r in done for t in r["still_held"]})
    newly = sorted({u for _, r in done for u in r["quarantined"]})
    text = (f"kanban sync: released {receipt} ({reason}) in {', '.join(name for name, _ in done)}: accepted the "
            f"Board's live evidence (recorded in {_TABLE}) and applied {replayed} row(s) through certify+apply")
    if newly:
        text += ("; the catch-up found evidence that no longer certifies and QUARANTINED: "
                 + "; ".join(f"{u} (release with `{release_command(slug, u)}`)" for u in newly))
    if still:
        text += f"; still held by other receipts: {', '.join(still)}"
    return 0, text
