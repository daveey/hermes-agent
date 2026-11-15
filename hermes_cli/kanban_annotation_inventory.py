"""Shared display-only replica inventory; never a strict renewal certificate."""
from __future__ import annotations

import threading
from collections import OrderedDict, deque

from hermes_cli.kanban_annotation_reader import ReplicaReader
from hermes_cli.kanban_annotation_semantic import TaskTokens

_CAP = 8
_HISTORY = 64
_lock = threading.Lock()
_inventories: OrderedDict[str, Inventory] = OrderedDict()


class Inventory:
    """Serialized raw reads and semantic deltas, bounded by path and generation.

    History holds changed keys, not old O(N) token maps. A missing cursor or a
    rebuilt reader returns an unknown delta, forcing the scope's full selector.
    Each read still checks the connection/file fences; no TTL certifies content.
    """

    def __init__(self):
        self.reader = ReplicaReader()
        self.tokens = TaskTokens()
        self.epoch = object()
        self.generation = 0
        self.history = deque(maxlen=_HISTORY)

    def read(self, conn, path, cursor):
        snapshot = self.reader.read(conn, path)
        previous = self.tokens._tokens
        tokens = self.tokens.advance(snapshot.rows)
        if tokens is not previous:
            if self.tokens.changed is None:
                self.epoch = object()
                self.history.clear()
            self.generation += 1
            self.history.append((self.generation, self.tokens.changed))
        changed = self.since(cursor)
        return snapshot, tokens, (self.epoch, self.generation), changed

    def since(self, cursor):
        if cursor is None or cursor[0] is not self.epoch:
            return None
        generation = cursor[1]
        if generation == self.generation:
            return frozenset()
        if not self.history or not self.history[0][0] - 1 <= generation < self.generation:
            return None
        changed = set()
        for current, keys in self.history:
            if current > generation:
                if keys is None:
                    return None
                changed.update(keys)
        return frozenset(changed)


def read(conn, path, cursor):
    """Return one atomic raw/token snapshot and changes since this scope's cursor."""
    key = str(path)
    with _lock:
        inventory = _inventories.get(key)
        if inventory is None:
            inventory = _inventories[key] = Inventory()
        _inventories.move_to_end(key)
        while len(_inventories) > _CAP:
            _inventories.popitem(last=False)
        try:
            return inventory.read(conn, path, cursor)
        except Exception:  # health: allow BLE001 -- discard every shared derivative before caller's exact fallback
            _inventories.pop(key, None)
            raise


def reset():
    with _lock:
        _inventories.clear()
