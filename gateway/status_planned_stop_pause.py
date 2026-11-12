"""Update-pause bookkeeping for planned-stop consumption, split from :mod:`gateway.status`."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_PLANNED_STOP_MUTEX_WAIT_S = 2.0


def _update_pause_on_disk() -> bool:
    """A Windows update pause of this checkout is on disk (its record or a recovery's claim), or the
    record directory cannot be listed. Only then does a consume need the pause lock and a checkpoint,
    and only then can accepted-stop evidence beside a request still be owed: with none, every pause
    was settled by recovery or the updater (or never made: every non-Windows host)."""
    from hermes_cli import update_pause_record
    record = update_pause_record.record_path()
    try:
        with os.scandir(record.parent) as entries:
            return any(e.name == record.name or (e.name.startswith(record.name + ".") and e.name.endswith(".claim"))
                       for e in entries)
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _checkpoint_planned_stop(path: Path, record: dict[str, Any]) -> None:
    """``on_consume``, decided after the request was read: an update writes its record before its
    request, so a pause that appeared since the consumer first looked is checkpointed too."""
    from hermes_cli import update_pause_record
    if _update_pause_on_disk():
        with update_pause_record._mutex(_PLANNED_STOP_MUTEX_WAIT_S):
            update_pause_record.mark_stop_consumed(path, record)
