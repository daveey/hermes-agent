#!/usr/bin/env python3
"""Worker count for scripts/run_tests_parallel.py on a CI runner (t_94f2c8ba).

The hermes-hub runners share one systemd slice (ci.slice) with an aggregate
CPU quota. Sizing workers from nproc (2 x 3 pinned CPUs = 6 per shard) put 36
test processes on an 8-core budget when six shards ran at once: load ~70 on 16
vCPU, timing tests failing, a slice taking 78 min. Size from the budget instead:

    per_shard = HERMES_CI_SLICE_CPUS / HERMES_CI_CONCURRENT_SHARDS
                (capped at HERMES_CI_RUNNER_CPUS, the container's own quota)
    workers   = max(2, round(per_shard * OVERSUBSCRIBE))

OVERSUBSCRIBE > 1 because a per-file pytest subprocess spends part of its life
in startup I/O, sleeps and subprocess waits. HERMES_TEST_WORKERS, when set,
wins (manual runs). Without the host variables it falls back to 2 x nproc, the
old behaviour, so runners that are not in a budgeted slice are unchanged.
The host sets the variables in /etc/systemd/system/gh-runner@.service.
"""
from __future__ import annotations

import os
import sys

OVERSUBSCRIBE = 1.5


def _positive(env: dict[str, str], name: str) -> float | None:
    raw = (env.get(name) or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"test_workers: {name}={raw!r} is not a number")
    if value <= 0:
        raise SystemExit(f"test_workers: {name}={raw!r} must be > 0")
    return value


def workers(env: dict[str, str], nproc: int) -> tuple[int, str]:
    explicit = (env.get("HERMES_TEST_WORKERS") or "").strip()
    if explicit:
        return int(explicit), "HERMES_TEST_WORKERS"
    cpus = _positive(env, "HERMES_CI_SLICE_CPUS")
    shards = _positive(env, "HERMES_CI_CONCURRENT_SHARDS")
    if cpus is None or shards is None:
        return max(1, nproc) * 2, f"2 x nproc ({nproc}); no CI slice budget in the environment"
    per_shard = cpus / shards
    cap = _positive(env, "HERMES_CI_RUNNER_CPUS")
    if cap is not None:
        per_shard = min(per_shard, cap)
    n = max(2, round(per_shard * OVERSUBSCRIBE))
    why = (f"slice {cpus:g} CPUs / {shards:g} concurrent shards = {per_shard:.2f} CPUs"
           f"{f' (runner cap {cap:g})' if cap is not None else ''} x {OVERSUBSCRIBE}")
    return n, why


def main() -> int:
    affinity = getattr(os, "sched_getaffinity", None)
    nproc = len(affinity(0)) if affinity else (os.cpu_count() or 1)
    n, why = workers(dict(os.environ), nproc)
    print(f"test_workers: {n} ({why})", file=sys.stderr)
    print(n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
