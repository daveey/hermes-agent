"""Cheap, profile-context-aware ordering for serial annotation holder work."""
from __future__ import annotations

import logging
import math

from hermes_cli import kanban_annotation_disk as disk

log = logging.getLogger(__name__)


def stalest_first(scopes, *, asleep=None):
    """Demanded scopes precede idle ones; missing publications are oldest.

    Only read the published copy and demand metadata, never source counts. Keep
    each read in its profile context. Sorting does not certify freshness or skip
    existing demand/lock/watch gates, and one broken copy cannot starve peers.
    """
    from hermes_cli.kanban_annotation_host import DEMAND_WINDOW_S

    def priority(item):
        scope, ctx = item

        def read_priority():
            age = disk.demand_age(scope)
            demanded = age is not None and 0 <= age <= DEMAND_WINDOW_S
            copy = disk.load_host(scope, shared=True)
            read_at = copy[1] if copy is not None else float('-inf')
            if not isinstance(read_at, (int, float)) or not math.isfinite(read_at):
                read_at = float('-inf')
            return (not demanded, read_at)

        try:
            return ctx.run(read_priority)
        except Exception:  # health: allow BLE001 -- unavailable metadata is oldest, never a fresh copy
            log.debug('annotation holder priority unavailable', exc_info=True)
            return (False, float('-inf'))

    awake = []
    for scope, ctx in scopes:
        try:
            if asleep is None or not ctx.run(asleep, scope):
                awake.append((scope, ctx))
        except Exception:  # health: allow BLE001 -- a broken scope gate must not stop peers
            log.debug('annotation holder gate unavailable', exc_info=True)
    return sorted(awake, key=priority)
