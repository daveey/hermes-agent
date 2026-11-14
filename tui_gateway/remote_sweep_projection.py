"""Retain remote-notice projections across unrelated replica commits.

The replica writer publishes a new seq for each changed row. Read identity rows
and changed payloads in one transaction; never cache time or claim decisions.
Only one replica/gateway generation is retained, with deleted rows discarded.

[daveey] A row that cannot reach this gateway (``compactable``: a bystander by
``hermes_cli.kanban_replica_relevance.is_bystander`` with no linked chat here) keeps
only ``"<seq>\\0<state>"``, never its cards or sessions: on the hub replica (18.6k rows
of 11 gateways) those were most of the serve's retained heap. ``states`` is one live
view per scope, updated in place by changed rows rather than rebuilt per call.
"""
from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Mapping

_lock = threading.RLock()
_identity = None
_rows: dict = {}  # ref -> "seq\0state" (compact) | (seq, cards, (state, {"sessions": ...}) | None)
_view = None


def reset() -> None:
    global _identity, _rows, _view
    with _lock:
        _identity, _rows, _view = None, {}, None


class CompactTask:
    """Stands in for a compact row's task in ``states``; ``load`` reads it back when a stale claim needs it."""

    __slots__ = ("key", "owner", "seq")

    def __init__(self, owner, key, seq):
        self.owner, self.key, self.seq = owner, key, seq

    def __eq__(self, other):
        if not isinstance(other, CompactTask):
            return NotImplemented
        return (self.owner, self.key, self.seq) == (other.owner, other.key, other.seq)

    def __hash__(self):
        return hash((self.owner, self.key, self.seq))

    def __repr__(self):
        return f"CompactTask({self.owner!r}, {self.key!r}, {self.seq!r})"

    def load(self, path):
        """The row's task while the replica still holds this seq, else None (the caller skips this pass)."""
        from tui_gateway.kanban_remote_notices import _ro

        with contextlib.closing(_ro(path)) as conn:
            row = conn.execute("SELECT seq, data FROM replica WHERE coll='tasks' AND gw=? AND key=?",
                               (self.owner, self.key)).fetchone()
        if row is None or row[0] != self.seq:
            return None
        try:
            task = json.loads(row[1])
        except (TypeError, ValueError):
            return None
        return task if isinstance(task, dict) else None


class StatesView(Mapping):
    """``{ref: (state, {"sessions": ...})}`` over one scope's ``_rows`` (a compact row's second item is a
    ``CompactTask``). It shows the newest snapshot of its scope; a new scope binds a new ``_rows``."""

    def __init__(self, rows: dict):
        self._data = rows

    def __getitem__(self, ref):
        with _lock:
            value = self._data[ref]
        if type(value) is str:
            seq, _, state = value.partition("\0")
            owner, _, key = ref.partition("/")
            return state, CompactTask(owner, key, int(seq))
        if value[2] is None:
            raise KeyError(ref)
        return value[2]

    def __iter__(self):
        with _lock:
            refs = [ref for ref, value in self._data.items() if type(value) is str or value[2] is not None]
        return iter(refs)

    def __len__(self):
        return sum(1 for _ in self)


def legacy_keys(rows) -> dict:
    """``{owner: {key}}`` of keys with fewer than two ``/`` (pre-profile rows), read before ``_canonical``."""
    out: dict = {}
    for owner, key, *_ in rows:
        if str(key).count("/") < 2:
            out.setdefault(owner, set()).add(key)
    return out


def compactable(task, owner, key, seq, me, legacy) -> bool:
    """The row can affect no card, notice or observation of ``me`` beyond its state scalar."""
    from hermes_cli.kanban_replica_relevance import is_bystander
    from tui_gateway.kanban_remote_notices import _linked_chats

    return (isinstance(task, dict) and bool(task.get("status")) and type(seq) is int
            and isinstance(owner, str) and isinstance(key, str) and "/" not in owner
            and is_bystander(task, key, me, legacy.get(owner, ())) and not _linked_chats(task, me))


def has_twin(owner, key, legacy) -> bool:
    """A legacy ``<board>/<id>`` row of the same owner exists: the qualified row is relevant again."""
    keys = legacy.get(owner)
    return bool(keys) and str(key).split("/", 1)[-1] in keys


def _project(rows, me, remote):
    cards = list(remote._sweep_cards(rows, me))
    states = {ref: (state, {"sessions": task.get("sessions")})
              for ref, (state, task) in remote._current_states(rows).items()}
    return cards, states


def _value(row, me, legacy, remote):
    owner, key, seq, _ = row
    cards = list(remote._sweep_cards([row], me))
    current = remote._current_states([row])
    if not current:
        return seq, cards, None
    state, task = next(iter(current.values()))
    if not cards and compactable(task, owner, key, seq, me, legacy):
        return f"{seq}\0{state}"
    return seq, cards, (state, {"sessions": task.get("sessions")})


def _uncached(conn, me, remote):
    global _identity, _rows, _view
    _identity, _rows, _view = None, {}, None
    return _project(remote._canonical(remote._task_rows(conn, me)), me, remote)


def snapshot(conn, identity, me):
    """Caller owns a snapshot transaction; unknown identities/schemas do not memoize."""
    from tui_gateway import kanban_remote_notices as remote

    global _identity, _rows, _view
    with _lock:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(replica)")}
        if identity is None or "seq" not in cols:
            return _uncached(conn, me, remote)
        scope = (identity, me)
        if _identity != scope:
            _identity, _rows, _view = None, {}, None
        sql = "SELECT gw, key, seq, NULL FROM replica WHERE coll = 'tasks'"
        raw_rows = conn.execute(sql + " AND gw != ?", (me,)).fetchall() if me else conn.execute(sql).fetchall()
        legacy = legacy_keys(raw_rows)
        rows = remote._canonical(raw_rows)
        del raw_rows
        current = _rows
        try:
            # A cold pass reads payloads in one paged query, rather than thousands of seeks.
            payloads = ({(row[0], row[1]): row for row in remote._canonical(remote._task_rows(conn, me))}
                        if not current else {})
            cards = []
            for owner, key, seq, _ in rows:
                ref = f"{owner}/{key}"
                value = current.get(ref)
                if type(value) is str:
                    stale = value.partition("\0")[0] != str(seq) or has_twin(owner, key, legacy)
                else:
                    stale = value is None or value[0] != seq
                if stale:
                    row = payloads.get((owner, key))
                    if row is None:
                        raw = conn.execute("SELECT data FROM replica WHERE coll='tasks' AND gw=? AND key=?",
                                           (owner, key)).fetchone()
                        row = (owner, key, seq, raw[0])
                    value = current[ref] = _value(row, me, legacy, remote)
                if type(value) is not str:
                    cards.extend(value[1])
            del payloads
            if len(current) != len(rows):  # deleted rows, or two rows sharing one ref
                refs = {f"{owner}/{key}" for owner, key, _, _ in rows}
                if len(refs) != len(rows):
                    return _uncached(conn, me, remote)
                for ref in [ref for ref in current if ref not in refs]:
                    del current[ref]
        except BaseException:
            _identity, _rows, _view = None, {}, None
            raise
        _identity = scope
        view = _view
        if view is None or view._data is not current:
            view = _view = StatesView(current)
        return cards, view
