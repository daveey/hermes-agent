"""Allocator housekeeping shared by the gateway's periodic resource chores."""

import time


def wait_for_resource_tick(stop_event, next_chores, queue_watch, chore):
    from hermes_cli.resource_limits import fd_headroom_delay
    from gateway.run_delivery_queue_watch import wait_for_next_tick
    delay = fd_headroom_delay(max(0.0, next_chores - time.monotonic()))
    wait_for_next_tick(stop_event, delay, queue_watch, chore)


def run_due_chores(tick_count, next_chores, chores, interval, chore):
    """A shorter FD timer must not accelerate the existing housekeeping cadences."""
    if time.monotonic() >= next_chores:
        tick_count += 1
        for every, label, fn in chores:
            if tick_count % every == 0:
                chore(label, fn)
        next_chores = time.monotonic() + interval
    return tick_count, next_chores


def housekeeping_memory_trim() -> None:
    """Messaging-gateway timer lane: full GC only once idle (#139763)."""
    from hermes_cli.mem_trim import trim_memory_when_idle
    trim_memory_when_idle(reason="messaging gateway housekeeping")
