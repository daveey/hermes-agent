"""Bounded, snapshot-local throughput contributions; unsupported data uses SQL.

No connections survive a read. The caller serializes this memo and drops a path
on any failure, including an interrupted in-place delta application.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from hermes_cli import session_throughput_revision as revision

MAX_IDS = 250_000
MAX_MODELS = 4096
MAX_ROWS = 1_000_000
MAX_PATHS = 32
INT64_MAX = (1 << 63) - 1
_USAGE = ("SELECT session_id, model, input_tokens + output_tokens, "
          "cache_read_tokens + cache_write_tokens FROM session_model_usage")
_SESSIONS = "SELECT id, ended_at IS NOT NULL FROM sessions"


class FullOnly(ValueError):
    """The original SQL is required to preserve this snapshot's semantics."""


@dataclass
class Contribution:
    started: int = 0
    ended: int = 0
    groups: dict = field(default_factory=dict)


@dataclass
class Memo:
    identity: tuple
    stamp: tuple
    ids: dict = field(default_factory=dict)
    groups: dict = field(default_factory=dict)
    started: int = 0
    ended: int = 0
    rows: int = 0

    def apply(self, item: Contribution, sign: int) -> None:
        self.started += sign * item.started
        self.ended += sign * item.ended
        for model, values in item.groups.items():
            target = self.groups.setdefault(model, [0, 0, 0])
            for axis in range(3):
                target[axis] += sign * values[axis]
            self.rows += sign * values[2]
            if target[2] == 0:
                del self.groups[model]
        if len(self.groups) > MAX_MODELS or self.rows > MAX_ROWS:
            raise FullOnly("memory bound")

    def result(self):
        # Validate after all changed IDs are replaced: intermediate sums need not
        # describe a real database snapshot (a transfer may subtract later).
        if any(v[0] > INT64_MAX or v[1] > INT64_MAX for v in self.groups.values()):
            raise FullOnly("SQL sum overflow")
        return ([(m, v[0], v[1]) for m, v in self.groups.items()], self.started, self.ended)


_memos: dict[str, Memo] = {}


def reset() -> None:
    _memos.clear()


def prune(paths) -> None:
    keep = {str(p.resolve()) for p in paths}
    for key in list(_memos):
        if key not in keep:
            del _memos[key]


def forget(key: str) -> None:
    _memos.pop(key, None)


def _item(ids, key):
    if not isinstance(key, str):
        raise FullOnly("non-text session key")
    if key not in ids:
        if len(ids) >= MAX_IDS:
            raise FullOnly("ID bound")
        ids[key] = Contribution()
    return ids[key]


def _load(conn, key=None):
    ids = {}
    args = () if key is None else (key,)
    suffix = "" if key is None else " WHERE id = ?"
    for sid, ended in conn.execute(_SESSIONS + suffix, args):
        item = _item(ids, sid)
        item.started += 1
        item.ended += ended
    suffix = "" if key is None else " WHERE session_id = ?"
    rows = 0
    models = set()
    for sid, model, io, cache in conn.execute(_USAGE + suffix, args):
        rows += 1
        if rows > MAX_ROWS or not isinstance(model, (str, bytes)):
            raise FullOnly("usage shape or bound")
        models.add(model)
        if len(models) > MAX_MODELS:
            raise FullOnly("model bound")
        for value in (io, cache):
            if value is not None and (type(value) is not int or value < 0):
                raise FullOnly("nonintegral or negative SQL pair")
        target = _item(ids, sid).groups.setdefault(model, [0, 0, 0])
        target[0] += io or 0
        target[1] += cache or 0
        target[2] += 1
    return ids


def read(conn, key: str, identity: tuple):
    """Return raw groups/counts or None for a full SQL read, in caller's transaction."""
    stamp = revision.read_stamp(conn)
    old = _memos.pop(key, None)  # publish only after every operation succeeds
    if stamp is None:
        return None
    try:
        reusable = (old is not None and old.identity == identity
                    and old.stamp[:2] == stamp[:2]
                    and stamp[4] <= old.stamp[2] <= stamp[2]
                    and revision.anchor_matches(conn, old.stamp[2], old.stamp[3]))
        if reusable and old is not None:
            memo = old
            for sid in revision.changes_since(conn, old.stamp[2]):
                previous = memo.ids.pop(sid, None)
                if previous is not None:
                    memo.apply(previous, -1)
                incoming = _load(conn, sid).get(sid)
                if incoming is not None:
                    if len(memo.ids) >= MAX_IDS:
                        raise FullOnly("ID bound")
                    memo.ids[sid] = incoming
                    memo.apply(incoming, 1)
            memo.stamp = stamp
        else:
            memo = Memo(identity, stamp, ids=_load(conn))
            for item in memo.ids.values():
                memo.apply(item, 1)
        result = memo.result()
    except FullOnly:
        return None
    if len(_memos) >= MAX_PATHS:
        _memos.pop(next(iter(_memos)))
    _memos[key] = memo
    return result
