"""One bounded immutable membership derivation; payloads are never retained. [daveey]"""
from types import MappingProxyType

# Exact ordered inventories, not clocks, connection versions or pathname guesses.
# A single tuple publication prevents readers from pairing different generations.
_MAX_KEYS = 25_000
_MAX_CHARS = 2_000_000
_memo = None


def build(groups):
    result = {}
    for gateway, keys in groups:
        for key in keys:
            parts = key.split('/')
            if len(parts) == 3:
                result.setdefault((parts[1], parts[2]), []).append((gateway, key))
    return MappingProxyType({key: tuple(value) for key, value in result.items()})


def derive(groups):
    """Reuse only an identical complete inventory; keep one generation under churn.

    Admission limits retained state, not the caller's existing unbounded scan.
    Instances can retain their borrowed immutable index until that projection ends.
    """
    global _memo
    inventory = tuple((gateway, tuple(keys)) for gateway, keys in groups)
    memo = _memo
    typed = all(type(gateway) is str for gateway, _ in inventory)
    if typed and memo is not None and memo[0] == inventory:
        return memo[1]
    index = build(inventory)
    admitted = (typed and sum(len(keys) for _, keys in inventory) <= _MAX_KEYS
                and sum(len(gateway) + sum(map(len, keys)) for gateway, keys in inventory) <= _MAX_CHARS)
    _memo = (inventory, index) if admitted else None
    return index
