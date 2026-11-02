"""Cross-process exclusion between a sync round and explicit board detachment."""
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def board_sync_lock(path: Path):
    """Never wait for network I/O while detaching: a busy round must finish first.

    Reuse the native portable nonblocking flock implementation with a separate
    key, so ordinary dispatcher ticks are not serialized with network polling.
    """
    from hermes_cli.kanban_db_connect import _dispatch_tick_lock
    canonical = Path(path).resolve()
    with _dispatch_tick_lock(canonical.with_name(canonical.name + ".sync")) as held:
        if not held:
            raise RuntimeError("board sync round is busy; retry detach after it finishes")
        yield
