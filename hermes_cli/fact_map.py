"""Path-copy hash trie for immutable generations without whole-store copies. [daveey]"""
from collections.abc import Mapping

_BITS = 5
_MASK = 31
_DEPTH = 13


def _get(node, key, code, depth):
    if node is None:
        raise KeyError(key)
    if isinstance(node, tuple):
        if node[0] == key:
            return node[1]
        raise KeyError(key)
    if depth == _DEPTH:
        return node[key]
    return _get(node.get(code & _MASK), key, code >> _BITS, depth + 1)


def _put(node, key, value, code, depth, delete):
    if node is None:
        return None if delete else (key, value)
    if isinstance(node, tuple):
        if node[0] == key:
            return None if delete else (key, value)
        if delete:
            return node
        if depth == _DEPTH:
            return {node[0]: node[1], key: value}
        old_code = hash(node[0]) >> (_BITS * depth)
        node = {old_code & _MASK: node}
    result = dict(node)
    if depth == _DEPTH:
        if delete:
            result.pop(key, None)
        else:
            result[key] = value
    else:
        slot = code & _MASK
        child = _put(node.get(slot), key, value, code >> _BITS, depth + 1, delete)
        if child is None:
            result.pop(slot, None)
        else:
            result[slot] = child
    return result or None


def _keys(node, depth=0):
    if node is None:
        return
    if isinstance(node, tuple):
        yield node[0]
    elif depth == _DEPTH:
        yield from node
    else:
        for child in node.values():
            yield from _keys(child, depth + 1)


class FactMap(Mapping):
    __slots__ = ("_root", "_size")

    def __init__(self, root=None, size=0):
        self._root, self._size = root, size

    def __getitem__(self, key):
        return _get(self._root, key, hash(key), 0)

    def __iter__(self):
        return _keys(self._root)

    def __len__(self):
        return self._size

    def put(self, key, value):
        exists = key in self
        return FactMap(_put(self._root, key, value, hash(key), 0, False), self._size + (not exists))

    def discard(self, key):
        exists = key in self
        return FactMap(_put(self._root, key, None, hash(key), 0, True), self._size - exists)
