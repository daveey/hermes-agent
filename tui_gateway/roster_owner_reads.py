"""Roster membership memo; never caches live work or execution observations.

One memo per SessionDB object and its actual writer connection generation. Same-
connection writes and external commits invalidate it; a reopened/replaced handle
cannot share it. Retain only this request's owner keys, not historical sessions.
"""
import threading
import weakref

_lock = threading.RLock()
_memos = weakref.WeakKeyDictionary()


def resolve_owners(db, owners, namespaces, resolve):
    """Map owner candidate tuples to stored ids; project activity separately each poll.

    No time-based membership predicates exist in the resolver. Namespace fences
    are part of the key; fresh live ownership inputs select the keys each time.
    Stores without a generation protocol retain the uncached behavior.
    """
    keys = dict.fromkeys(ids for _kind, ids, _item in owners)
    generation = getattr(db, "_write_generation", None)
    if not callable(generation):
        return {ids: resolve(db, ids, namespaces) for ids in keys}
    with _lock:
        before = generation()
        # A read that can see pending writes must never survive their rollback.
        if not isinstance(before, tuple) or before[0].in_transaction:
            _memos.pop(db, None)
            return {ids: resolve(db, ids, namespaces) for ids in keys}
        fence = frozenset(namespaces)
        previous = _memos.get(db)
        saved = previous[2] if previous and previous[:2] == (before, fence) else {}
        result = {ids: saved[ids] if ids in saved else resolve(db, ids, namespaces) for ids in keys}
        if generation() == before:
            _memos[db] = (before, fence, result)
        else:
            _memos.pop(db, None)
        return result
