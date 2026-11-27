"""Allocator housekeeping shared by the gateway's periodic resource chores."""


def housekeeping_memory_trim() -> None:
    """Messaging-gateway timer lane: full GC only once idle (#139763)."""
    from hermes_cli.mem_trim import trim_memory_when_idle
    trim_memory_when_idle(reason="messaging gateway housekeeping")
