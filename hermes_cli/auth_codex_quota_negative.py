"""Cross-process *negative-only* Codex quota probe throttle.

An exhausted grant never becomes usable from this file. Missing, stale or invalid
records fall through to the live probe; positive results stay process-local.
"""
import hashlib
import json
import time

MAX_BYTES = 65536
MAX_ENTRIES = 128
TTL_SECONDS = 60.0


def _path():
    from hermes_constants import get_hermes_home
    return get_hermes_home() / 'cache' / 'codex-quota-negative.json'


def _records(path):
    try:
        if path.stat().st_size <= MAX_BYTES:
            records = json.loads(path.read_text(encoding='utf-8-sig'))
            if isinstance(records, dict) and len(records) <= MAX_ENTRIES:
                return records
    except (OSError, ValueError, TypeError):
        pass
    return {}


def key(token, url, reset_at, failure_at):
    raw = json.dumps([token, url, reset_at, failure_at], allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def hit(cache_key, interval):
    stamp = _records(_path()).get(cache_key)
    return (type(stamp) in (int, float)
            and 0 <= time.time() - stamp < min(interval, TTL_SECONDS))


def remember(cache_key):
    from utils import atomic_write_bytes
    path, now = _path(), time.time()
    records = {k: v for k, v in _records(path).items()
               if type(v) in (int, float) and 0 <= now - v < TTL_SECONDS}
    records[cache_key] = now
    records = dict(sorted(records.items(), key=lambda item: item[1])[-MAX_ENTRIES:])
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Concurrent last-writer-wins can only discard a negative memo (an extra live probe).
        atomic_write_bytes(path, json.dumps(records).encode(), mode=0o600)
    except (OSError, ValueError, TypeError):
        pass  # A throttle write never changes the live quota verdict.
