"""Cross-process exclusion between a sync round and explicit board detachment."""
from contextlib import contextmanager
from pathlib import Path


class SyncRoundBusy(RuntimeError):
    """A sync round, sender or detach already owns this board's fence."""


@contextmanager
def sender_lock(path: Path | str):
    """Own the full send/session, including direct callers outside sync rounds.

    Separate from the round lock: a round calls push while holding its own lock.
    Detach acquires round then sender, never the reverse. No nested admission or
    process-local bypass is supported. Old processes still require explicit drain.
    """
    from hermes_cli.kanban_db_connect import _dispatch_tick_lock
    canonical = Path(path).resolve()
    with _dispatch_tick_lock(canonical.with_name(canonical.name + ".sender")) as held:
        if not held:
            raise SyncRoundBusy("board sender is busy; retry after it finishes")
        yield


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
            raise SyncRoundBusy("board sync round is busy; retry after it finishes")
        yield
