"""[daveey] Gateway processes a service unit supervises below its ``MainPID``.

A unit whose ``ExecStart`` is a non-exec wrapper (a worker node's ``hermes-gateway-broker`` runs
``token-broker exec … -- github_quota.py … -- hermes gateway run``) reports the wrapper as its
``MainPID``; the real ``gateway run`` process is a child of it, in the same cgroup. Callers of
``_get_service_pids`` exclude service gateways from "manual gateway" sweeps, so a MainPID-only set
let ``hermes update`` classify the unit's own gateway as manual: it armed a detached
``gateway run --replace`` watcher inside the updater's cgroup (``hermes-core-update.service``),
drained the unit's gateway, and the replacement then held the gateway lock outside the unit while
the unit crash-looped on ``already_running`` (exit 75) (hermes-worker-2, 2026-11-12).

These PIDs are not only protected. The update and reaper sweeps skip them, and ``gateway status`` and
inventory label them service-managed. Default-scope stops (``_stop_host_multiplexer``, uninstall) also
TARGET them through ``find_gateway_pids`` → ``kill_gateway_processes``. So the set must be exact in
both directions:

* A descendant that is not a ``gateway run`` command line is left out, so a wrapper's other children
  and card workers never become "gateway PIDs".
* The walk stops at the unit's gateway. Kanban workers and agent terminals run below it, and a
  ``gateway run`` an agent starts there is a manual gateway. Counted as service-managed, it was skipped
  by the update sweep, drained by the stale-survivor pass with no relaunch owed, and labelled systemd
  by inventory.

The wrapper's own command line can match as well: ``github_quota.py … -- hermes gateway run`` carries
the gateway argv as data. So a matching process with a matching DIRECT child is a wrapper, and the walk
goes through it. The first matching process without one is the gateway, and its subtree is not walked
(a gateway's own children are workers and shells, never a direct ``gateway run``).
"""
from __future__ import annotations


def gateway_descendant_pids(main_pid: int) -> set[int]:
    """The unit's ``gateway run`` process below *main_pid*; empty when unreadable. Never raises.

    Empty when *main_pid* itself is the gateway: it is in the caller's set already, and nothing below it
    belongs to the unit. The walk never descends below a gateway.
    """
    if main_pid <= 1:
        return set()
    try:
        import psutil  # type: ignore

        root = psutil.Process(main_pid)
    except Exception:  # health: allow BLE001 -- no psutil / gone / denied: nothing provable to add
        return set()
    try:
        from gateway.status import _read_process_cmdline, looks_like_gateway_command_line
    except Exception:  # health: allow BLE001 -- identity matcher unavailable: add nothing
        return set()

    def is_gateway(proc) -> bool:
        try:
            return looks_like_gateway_command_line(_read_process_cmdline(int(proc.pid)))
        except Exception:  # health: allow BLE001 -- a process that exits mid-scan is not a gateway
            return False

    def children(proc) -> list:
        try:
            return list(proc.children())
        except Exception:  # health: allow BLE001 -- gone / denied: no children to walk
            return []

    def is_wrapper(proc) -> bool:
        return any(is_gateway(child) for child in children(proc))

    if is_gateway(root) and not is_wrapper(root):
        return set()
    found: set[int] = set()
    seen: set[int] = {main_pid}
    stack = children(root)
    while stack:
        proc = stack.pop()
        pid = int(proc.pid)
        if pid in seen:
            continue
        seen.add(pid)
        if is_gateway(proc):
            found.add(pid)
            if not is_wrapper(proc):
                continue  # the unit's gateway: what runs below it is not the unit's
        stack.extend(children(proc))
    return found
