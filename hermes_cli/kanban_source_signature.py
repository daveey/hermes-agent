"""Optional cache tokens must not make raw headers a SQLite read requirement."""
from hermes_cli import kanban_annotation_watch


def signature(scope, dbs, files):
    """Return None when safe header observation is unavailable; never reuse it."""
    try:
        return kanban_annotation_watch.signature(scope, dbs, files)
    except (OSError, AttributeError, NotImplementedError):
        # SQL can still succeed without pread or access to a raw header. Do not
        # substitute a stat-only token: same-stat WAL commits must remain visible.
        return None
