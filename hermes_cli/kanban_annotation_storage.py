"""Nonblocking publication custody for split annotation copies. [daveey]"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path


def signature(path: Path) -> tuple[int, ...] | None:
    try:
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    except OSError:
        return None


@contextmanager
def custody(path: Path, *, writer: bool):
    # Fail closed on unsupported hosts or contention: this is a cache, never a
    # reason to block a request. A stable lock inode is never removed by pruning.
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.host.lock').open('a') as lock:
        mode = fcntl.LOCK_EX if writer else fcntl.LOCK_SH
        fcntl.flock(lock, mode | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def body_matches(path: Path, parts: list) -> bool:
    """A new publisher may reuse a prior publisher's body only after verification."""
    try:
        body = json.loads(path.read_text(encoding='utf-8-sig'))
        return (isinstance(body, dict) and
                json.dumps(body.get('parts'), sort_keys=True, separators=(',', ':')) ==
                json.dumps(parts, sort_keys=True, separators=(',', ':')))
    except (OSError, ValueError):
        return False


def previous_digest(path: Path) -> str | None:
    try:
        body = json.loads(path.with_suffix('.host.json').read_text(encoding='utf-8-sig'))
        value = body.get('parts_digest')
        return value if isinstance(value, str) else None
    except (OSError, ValueError, AttributeError):
        return None


def prune(path: Path, keep: set[str | None]) -> None:
    """Under exclusive custody, retain current and preceding published bodies."""
    for body in path.parent.glob(f'{path.stem}.*.body.json'):
        digest = body.name[len(path.stem) + 1:-len('.body.json')]
        if digest not in keep:
            try:
                body.unlink()
            except OSError:
                pass  # best effort; a later successful publisher retries
