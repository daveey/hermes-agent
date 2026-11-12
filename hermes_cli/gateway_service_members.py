"""[daveey] Gateway processes a service unit supervises below its ``MainPID``.

A unit whose ``ExecStart`` is a non-exec wrapper (a worker node's ``hermes-gateway-broker`` runs
``token-broker exec … -- github_quota.py … -- hermes gateway run``) reports the wrapper as its
``MainPID``; the real ``gateway run`` process is a child of it, in the same cgroup. Callers of
``_get_service_pids`` exclude service gateways from "manual gateway" sweeps, so a MainPID-only set
let ``hermes update`` classify the unit's own gateway as manual: it armed a detached
``gateway run --replace`` watcher inside the updater's cgroup (``hermes-core-update.service``),
drained the unit's gateway, and the replacement then held the gateway lock outside the unit while
the unit crash-looped on ``already_running`` (exit 75) (hermes-worker-2, 2026-11-12).

Over-inclusion is safe for every caller (these PIDs are only ever protected, never targeted); a
descendant that is not a ``gateway run`` command line is left out so wrappers and card workers
never become "gateway PIDs" through ``find_gateway_pids``.
"""
from __future__ import annotations


def gateway_descendant_pids(main_pid: int) -> set[int]:
    """``gateway run`` processes below *main_pid* (recursive); empty when unreadable. Never raises."""
    if main_pid <= 1:
        return set()
    try:
        import psutil  # type: ignore

        children = psutil.Process(main_pid).children(recursive=True)
    except Exception:  # health: allow BLE001 -- no psutil / gone / denied: nothing provable to add
        return set()
    try:
        from gateway.status import _read_process_cmdline, looks_like_gateway_command_line
    except Exception:  # health: allow BLE001 -- identity matcher unavailable: add nothing
        return set()
    found: set[int] = set()
    for child in children:
        try:
            pid = int(child.pid)
            if looks_like_gateway_command_line(_read_process_cmdline(pid)):
                found.add(pid)
        except Exception:  # health: allow BLE001 -- a child that exits mid-scan is simply not added
            continue
    return found
