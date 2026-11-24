"""Cadence at the sync-loop caller only; housekeeping's sweep stays unconditional."""
_LAST_BOARDS = None
_LAST_AT = 0.0


def due(boards, now):
    """First sweep, changed board list, or 30 seconds since the previous attempt.

    Record before the sweep. A busy close retries on the timer or housekeeping,
    not the next unchanged round. State survives loop tenures in this process.
    """
    global _LAST_BOARDS, _LAST_AT
    current = tuple(sorted((slug, str(path)) for slug, path in boards))
    if current == _LAST_BOARDS and now - _LAST_AT < 30.0:
        return False
    _LAST_BOARDS, _LAST_AT = current, now
    return True


def reset():
    """Tests only: never reset on a production loop restart."""
    global _LAST_BOARDS, _LAST_AT
    _LAST_BOARDS, _LAST_AT = None, 0.0
