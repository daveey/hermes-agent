"""Open accounting intervals at a native-thread sampling boundary. [daveey]

The caller serializes entry, close and sampling with its accounting lock. This
registry does not book counters: its overlay is discarded after each snapshot.
"""
from __future__ import annotations

from typing import Any


class OpenIntervals:
    def __init__(self) -> None:
        self.by_tid: dict[int, dict[Any, tuple[str, float, float]]] = {}

    def enter(self, tid: int, token: object, name: str, cpu: float, accounted: float) -> None:
        self.by_tid.setdefault(tid, {})[token] = (name, cpu, accounted)

    def close(self, tid: int, token: object) -> None:
        entries = self.by_tid.get(tid)
        if entries is not None:
            entries.pop(token, None)
            if not entries:
                self.by_tid.pop(tid, None)

    def discard(self, tid: int) -> None:
        self.by_tid.pop(tid, None)

    def clear(self) -> None:
        self.by_tid.clear()

    def after_fork(self, old_tid: int, new_tid: int, cpu: float) -> None:
        inherited = self.by_tid.get(old_tid, {})
        self.by_tid.clear()
        for token, (name, _, _) in inherited.items():
            # Only context managers survive as usable child intervals. span tokens
            # deliberately retain their original native-thread identity.
            if isinstance(token, int):
                continue
            token._tid = new_tid
            if hasattr(token, "_acc0"):
                token._acc0 = 0.0
            self.enter(new_tid, token, name, cpu, 0.0)

    def apply(self, row: dict[str, Any]) -> None:
        entries = self.by_tid.get(row["tid"])
        if not entries:
            return
        # Context managers are nested on one native thread. The oldest interval
        # covers the union; subtract closed inner intervals, never count them twice.
        name, cpu0, acc0 = next(iter(entries.values()))
        row["open_accounting"] = {"name": name, "cpu_s": None, "depth": len(entries), "available": False}
        if row["cpu_s"] is None or not row.get("cpu_sample_precise", True):
            return
        pending = row["cpu_s"] - cpu0 - (row["accounted_cpu_s"] - acc0)
        row["open_accounting"]["cpu_s"] = round(pending, 6)
        if pending < 0:
            return  # inconsistent sample, not permission to reduce completed accounting
        row["open_accounting"]["available"] = True
        row["accounted_cpu_s"] = round(row["accounted_cpu_s"] + pending, 6)

    def complete(self, rows: list[dict[str, Any]]) -> bool:
        sampled = {row["tid"] for row in rows if row.get("open_accounting", {}).get("available")}
        return self.by_tid.keys() <= sampled
