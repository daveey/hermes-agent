"""Observation-side bookkeeping for replica rows that cannot reach this gateway. [daveey]

``kanban_remote_observations`` keeps a decoded summary per replica row and an ``Observed`` entry per terminal
row. A bystander (``remote_sweep_projection.compactable``) is never offered to a chat here, so its lane keeps only
``(hash(owner, key), seq, hash(ref, state))`` in three sorted ``array('q')`` columns, ~24 bytes a row. Its terminal
states still reach the observation ledger (a card linked later must keep main's ``first_seen``): a re-decoded
row whose ``(ref, state)`` was not yet queued goes to ``Lane.pending``, which ``observe`` persists with the
relevant cards. The table belongs to one ledger file; a second ledger path turns the lane off (full form, one
cold re-read), a replaced ledger file re-reads so every terminal bystander is written to it. The table also
belongs to one ``me`` (the replica's ``meta.gateway``): a renamed gateway re-classifies every row.
"""
from __future__ import annotations

import contextlib
import json
from array import array
from bisect import bisect_left
from itertools import pairwise

_INT64 = range(-(1 << 63), 1 << 63)
_MULTI = ("", None)  # identity sentinel: more than one ledger path observed this replica, every row in full form


class Table:
    """Sorted columns of compact rows; rebuilt (sorted once) from each read, so deleted rows drop out.
    ``trusted`` False: the seqs may belong to a replaced replica file, only the state hashes still hold."""

    __slots__ = ("hashes", "seqs", "states", "trusted")

    def __init__(self, entries=()):
        entries = sorted(entries)
        self.hashes = array("q", [entry[0] for entry in entries])
        self.seqs = array("q", [entry[1] for entry in entries])
        self.states = array("q", [entry[2] for entry in entries])
        self.trusted = True

    def get(self, row_hash):
        at = bisect_left(self.hashes, row_hash)
        if at < len(self.hashes) and self.hashes[at] == row_hash:
            return self.seqs[at], self.states[at]
        return None

    def __len__(self):
        return len(self.hashes)


class Lane:
    """One replica flight's compact rows. Guarded by the flight's lock."""

    def __init__(self):
        self.table = Table()
        self.home: tuple | None = None  # (ledger path, ledger file id) | _MULTI
        self.pending: list[tuple] = []  # (owner, key, summary) not yet written to the home ledger
        self.me = None  # the gateway the table's rows were classified for

    @property
    def enabled(self) -> bool:
        return self.home is not _MULTI

    def adopt(self, path: str, file_id) -> bool:
        """Bind to the ledger ``observe`` writes now. True when the flight must forget its decoded rows."""
        home = self.home
        if home is _MULTI:
            return False
        if home is None:
            self.home = (path, file_id)
            return False
        if home[0] != path:
            if len(self.table) or self.pending:
                self.home, self.table, self.pending = _MULTI, Table(), []
                return True
            self.home = (path, file_id)
            return False
        if home[1] is not None and file_id != home[1] and len(self.table):
            self.home, self.table = (path, file_id), Table()
            return True  # a new ledger file: every terminal bystander is re-read and re-queued
        self.home = (path, file_id if file_id is not None else home[1])
        return False


def _state_hash(owner, key, summary) -> int:
    from tui_gateway.kanban_remote_notice_state import state

    return hash((f"{owner}/{key}", state(summary))) or 1


def scan(conn, rows, tag: str, me, decoded: dict, lane: Lane, summarize) -> list[tuple]:
    """``_changed_rows_cards`` with a lane. ``rows`` is (canonical identity rows, legacy keys per owner read before
    ``_canonical``). Relevant rows keep today's ``decoded`` entries and cards."""
    from tui_gateway.remote_sweep_projection import compactable, has_twin

    canonical, legacy = rows
    hashes = [hash((owner, key)) for owner, key, _, _ in canonical]
    ordered = sorted(hashes)
    collide = {a for a, b in pairwise(ordered) if a == b}
    del ordered
    seen, cards, entries = set(), [], []
    if lane.me != me:  # a bystander for the old name may be linked to this one: re-read and re-classify every row
        lane.table.trusted = False  # its state hashes still hold, so unchanged terminal states are not re-queued
        for ident in [ident for ident in decoded if ident[0] == tag]:
            del decoded[ident]
        lane.me = me
    old = lane.table
    for (owner, key, seq, _), row_hash in zip(canonical, hashes):
        ident = (tag, owner, key)
        seen.add(ident)
        hit = decoded.get(ident)
        if hit is None or hit[0] != seq:
            eligible = row_hash not in collide and type(seq) is int and seq in _INT64
            known = old.get(row_hash) if eligible else None
            if known is not None and old.trusted and known[0] == seq and not has_twin(owner, key, legacy):
                entries.append((row_hash, seq, known[1]))
                continue
            raw = conn.execute("SELECT data FROM replica WHERE gw = ? AND coll = 'tasks' AND key = ?",
                               (owner, key)).fetchone()
            task = summary = None
            with contextlib.suppress(TypeError, ValueError):
                task = json.loads(raw[0]) if raw else None
                summary = summarize(task) if raw else None
            if eligible and compactable(task, owner, key, seq, me, legacy):
                decoded.pop(ident, None)
                state = 0
                if summary is not None:
                    state = _state_hash(owner, key, summary)
                    if known is None or known[1] != state:
                        lane.pending.append((owner, key, summary))
                entries.append((row_hash, seq, state))
                continue
            hit = decoded[ident] = (seq, summary)
        if hit[1] is not None:
            cards.append((owner, key, hit[1]))
    for ident in [ident for ident in decoded if ident[0] == tag and ident not in seen]:
        del decoded[ident]
    lane.table = Table(entries)
    return cards
