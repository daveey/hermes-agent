"""Pre-swap gateway pause for ``hermes update`` on Linux and macOS.

Windows pauses its gateways before the fetch (``update_cmd_windows``) because they lock venv
files. On POSIX nothing is locked, but a gateway that keeps running while git rewrites the tree
loads new-tree modules into an old process on its next lazy import. So every gateway running
from this install is stopped at the commit point, immediately before the first checkout move
(``update_cmd_commit.arm_commit_point``), and the same set is restarted once the dependencies
are prepared and the launch files rewritten, before the product builds
(``source_build.build_update_products``'s ``after_dependencies``).

The set rides the Windows pause contract: one token in ``update_pause_record`` (identity = pid
+ start time; supervisor unit, or argv + home for bare processes), written before the first stop,
resumed by ``_resume_windows_gateways_after_update`` (which dispatches here on
``platform == "posix"``), adopted by the next ``hermes update`` and resumed by the next launch
when this process dies.

* a gateway of this home that loads ANOTHER checkout (a unit pinned to its own core, e.g. an Ask
  Eve pinned owner) is not in the set at all: the move cannot reach its code, so it is named and
  left running, neither paused here nor restarted afterwards (``_systemd_unit_owned_by_update``).
* each gateway accepts ``pause-if-idle-for-update`` only with no active work, checking and
  closing admission in one loop callback. Busy gateways defer the update without starting a
  drain. Unsupported gateways refuse explicitly until restarted on the installed code; a lost
  reply retains conservative recovery debt. The updater waits at most 30 seconds for accepted
  stops, not the post-restart after-turn budget. No planned-stop marker is sent.
* supervised gateways that exit are disarmed through systemd/launchd before mutation and
  restarted through their supervisor after dependencies are ready.
* bare gateways are replayed from their recorded argv + home. Refusal settles already-accepted
  stops before rollback; still-live identities retain durable restart debt rather than twins.
* the updater never kills itself: a unit whose cgroup holds this process (``/update`` from
  chat: the update runs in the gateway unit's cgroup, which ``systemctl stop`` kills) is
  stopped only after this process moved into a transient scope of its own. A launchd job
  whose coalition holds it, or any gateway whose stop would take the updater down, is left
  running for the post-commit restart.
"""

from __future__ import annotations

import copy
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

#: This run's token: armed in ``_cmd_update_impl``, filled at the commit point. Same dict object
#: the completion request carries, so the paused set reaches the completion child.
_RUN: dict | None = None
#: Runtimes the early resume already restarted on the new code: the late fleet restart in the
#: same completion process must not restart them a second time.
_RESTARTED: dict = {"units": set(), "labels": set(), "pids": set()}
#: The replay environment of each paused bare gateway's home, built at the commit point: built after
#: the move, it would import the new tree's modules into this process.
_REPLAY_ENVS: dict[str, dict] = {}

_STOP_TIMEOUT_S = 120.0
_READY_TIMEOUT_S = 30.0
# Desktop cannot reconnect while the update lock is held, including to answer an
# approval the draining turn needs. A pre-swap wait must yield, not spend the
# post-restart after-turn budget behind that fence.
_PRE_SWAP_DRAIN_WAIT_S = 30.0


class PauseRefused(RuntimeError):
    """The pause could not be completed; everything it stopped was started again. ``units``
    names the gateways that refused (``update_pause_deferrals`` counts their streaks)."""

    def __init__(self, message: str = "", units=()):
        super().__init__(message)
        self.units = list(units)


def _empty_token() -> dict:
    return {"platform": "posix", "resume_needed": False, "profiles": {}, "unmapped_pids": [],
            "unmapped": [], "posix_units": []}


def arm_pause(*, no_gateway_restart: bool) -> dict | None:
    """Arm this run's pause (no stop yet) and adopt any set a killed update left paused.

    ``None`` on Windows (its own pause) and with ``--no-gateway-restart`` (the user manages
    gateways, nothing is stopped). An adopted orphan set is recorded under this process now, so
    even an update that never moves the checkout restarts it at exit."""
    global _RUN
    _REPLAY_ENVS.clear()
    _RUN = None
    if sys.platform == "win32" or no_gateway_restart:
        return None
    from hermes_cli import update_pause_record as pause_record
    token = _empty_token()
    try:
        adopted, claims = pause_record.adopt_orphans()
        if adopted is not None:
            token = pause_record.record_pause({**token, "resume_needed": True}, adopted, claims)
            token["platform"] = "posix"
    except Exception as exc:  # health: allow BLE001 -- an unreadable record must not block the update; recovery retries it
        print(f"  ⚠ Could not adopt gateways an earlier update paused: {exc}")
    _RUN = token
    return token


def gateways_paused() -> bool:
    """True while this run holds gateways stopped (no gateway can answer a chat prompt)."""
    return bool(_RUN and _RUN.get("posix_stopped") and _RUN.get("resume_needed"))


# --------------------------------------------------------------------------- discovery

def _unit_cgroup(scope_cmd: list, unit: str) -> str | None:
    from hermes_cli.update_cmd_fleet import _systemctl
    with suppress(OSError, subprocess.TimeoutExpired):
        result = _systemctl(scope_cmd + ["show", unit, "--property=ControlGroup", "--value"], timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return None


def _pid_cgroup(pid: int | str = "self") -> str | None:
    """The systemd cgroup path of *pid*: the unified (v2) line, else the v1 ``name=systemd``
    hierarchy, which systemd names units in identically. ``None``: not Linux or unreadable, which
    callers must treat as unknown, never as outside a unit."""
    with suppress(OSError):
        lines = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").splitlines()
        unified = next((line[3:].strip() for line in lines if line.startswith("0::")), "")
        legacy = next((line.split(":", 2)[2].strip() for line in lines if ":name=systemd:" in line), "")
        return unified or legacy or None
    return None


def _inside(cgroup: str | None, unit_cgroup: str | None) -> bool:
    if not cgroup or not unit_cgroup:
        return False
    return cgroup == unit_cgroup or cgroup.startswith(unit_cgroup.rstrip("/") + "/")


def _discover_systemd() -> list[dict]:
    """Active ``hermes-gateway*`` units of this install, one per live MainPID."""
    from hermes_cli.gateway import _ensure_user_systemd_env, supports_systemd_services
    from hermes_cli.update_cmd_fleet import (
        _for_each_systemd_gateway_unit, _systemd_gateway_unit_listings, _systemd_unit_owned_by_update,
        _unit_main_pid,
    )
    if not supports_systemd_services():
        return []
    with suppress(Exception):
        _ensure_user_systemd_env()
    units: list[dict] = []
    seen_pids: set[int] = set()
    for scope, scope_cmd, listing in _systemd_gateway_unit_listings():
        names: list[str] = []
        _for_each_systemd_gateway_unit(listing.stdout, process_unit=names.append,
                                       on_unit_timeout=lambda name, exc: None)
        for name in names:
            if not name.startswith("hermes-gateway"):
                continue  # serve/dashboard units are not gateways: the post-commit restart owns them
            pid = _unit_main_pid(scope_cmd, name)
            if pid <= 0 or pid in seen_pids or not _systemd_unit_owned_by_update(scope_cmd, name):
                continue
            seen_pids.add(pid)  # a legacy per-profile unit sharing the multiplexer's PID is one process
            units.append({"kind": "systemd", "scope": scope, "unit": name, "pid": pid,
                          "cgroup": _unit_cgroup(scope_cmd, name), "home": _home_of(pid)})
    return units


def _discover_launchd() -> list[dict]:
    """Loaded ``ai.hermes.gateway*`` jobs of this install with a live process (macOS)."""
    if sys.platform != "darwin":
        return []
    from hermes_cli.gateway import (
        _locate_launchd_gateway_service, get_launchd_plist_path, launchd_gateway_labels_for_install,
        legacy_launchd_labels_for_install,
    )
    from hermes_cli.update_fleet_scope import launchd_label_foreign_home
    derived = launchd_gateway_labels_for_install()
    jobs = []
    for label in derived + legacy_launchd_labels_for_install(exclude=set(derived)):
        if launchd_label_foreign_home(label) is not None:
            continue
        domain, pid = _locate_launchd_gateway_service(label)
        if domain is None or not pid or pid <= 0:
            continue
        plist = get_launchd_plist_path().with_name(f"{label}.plist")
        jobs.append({"kind": "launchd", "label": label, "domain": domain, "plist": str(plist), "pid": int(pid),
                     "home": _home_of(int(pid))})
    return jobs


def _home_of(pid: int) -> str | None:
    """The Hermes home *pid* serves (its control socket lives there), or ``None``."""
    from hermes_cli.update_fleet_scope import gateway_pid_home
    with suppress(Exception):
        if home := gateway_pid_home(pid):
            return str(home)
    return None


def _supervisor_markers(pid: int) -> str | None:
    """Why *pid* answers to a supervisor other than a hermes unit/job (it would respawn it on old
    code), or ``None``."""
    from hermes_cli.gateway import _capture_gateway_argv, argv_declares_external_supervisor
    if argv_declares_external_supervisor(_capture_gateway_argv(pid)):
        return "--external-supervisor"
    try:
        import psutil  # type: ignore
        env = psutil.Process(pid).environ()
    except Exception:  # health: allow BLE001 -- unreadable environment: no marker proven
        return None
    if env.get("HERMES_S6_SUPERVISED_CHILD"):
        return "s6"
    if unit := _respawning_service(pid):
        return f"systemd unit {unit}"
    # launchd names terminal apps' shells application.<bundle id>; those are not KeepAlive jobs.
    xpc = env.get("XPC_SERVICE_NAME", "0")
    if xpc not in ("", "0") and not xpc.startswith("application."):
        return f"launchd job {xpc}"
    return None


def _respawning_service(pid: int) -> str | None:
    """The systemd service that would respawn *pid* when it exits, or ``None``.

    systemd restarts a unit only when its main process exits, so the gateway must BE the
    MainPID or its direct child (a non-exec wrapper that exits with it). The inherited
    INVOCATION_ID is no evidence: a terminal tab's shell carries one (its .scope is never
    respawned), and so does anything deep under a CI runner's service."""
    from hermes_cli.update_cmd_fleet import _systemctl, _unit_main_pid
    cgroup = _pid_cgroup(pid) or ""
    leaf = cgroup.rsplit("/", 1)[-1]
    if not leaf.endswith(".service"):
        return None
    scope_cmd = ["systemctl", "--user"] if "/user@" in cgroup else ["systemctl"]
    with suppress(OSError, subprocess.SubprocessError):
        main = _unit_main_pid(scope_cmd, leaf)
        if main > 0 and main in (pid, _ppid(pid)):
            restart = _systemctl(scope_cmd + ["show", leaf, "--property=Restart", "--value"], timeout=10)
            return leaf if (restart.stdout or "").strip() not in ("", "no") else None
    return None


def _discover_bare(service_pids: set[int]) -> tuple[list[dict], list[str]]:
    """``(bare gateways, notices)``: this install's ``gateway run`` processes outside any unit."""
    from gateway.status import get_process_start_time
    from hermes_cli.gateway import _capture_gateway_argv, _get_service_pids, find_gateway_pids
    from hermes_cli.update_cmd_fleet import _scoped_manual_gateway_pids
    from hermes_cli.update_fleet_scope import gateway_pid_home
    exclude = set(service_pids) | set(_get_service_pids(all_profiles=True))
    pids = _scoped_manual_gateway_pids(find_gateway_pids(exclude_pids=exclude, all_profiles=True))
    bare, notices = [], []
    for pid in pids:
        pid = int(pid)
        marker = _supervisor_markers(pid)
        argv, home = _capture_gateway_argv(pid), gateway_pid_home(pid)
        if marker or not argv or not home:
            why = f"supervised by {marker}" if marker else "its command line could not be read" if not argv \
                else "its Hermes home could not be proved"
            notices.append(f"  ↷ gateway PID {pid} keeps running until after the update ({why})")
            continue
        bare.append({"pid": pid, "argv": list(argv), "home": str(home), "ct": get_process_start_time(pid)})
    return bare, notices


# --------------------------------------------------------------------------- self-preservation

def _ppid(pid: int) -> int:
    with suppress(OSError, ValueError, IndexError):
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return int(stat.rsplit(")", 1)[1].split()[1])
    return 0


def _escape_cgroup(unit: dict) -> bool:
    """Move this process (and its ancestors in the same unit, e.g. ``/update``'s bash wrapper)
    into a transient scope so stopping *unit* cannot kill it. True only when proven out: a
    membership that cannot be read (either side) keeps the unit running, since its stop would take
    this update down with it."""
    own, unit_cgroup = _pid_cgroup(), unit.get("cgroup")
    if own is None or not unit_cgroup:
        return False
    if not _inside(own, unit_cgroup):
        return True
    from gateway.status import looks_like_gateway_command_line
    pids, pid = [], os.getpid()
    while pid > 1 and pid != unit["pid"] and _inside(_pid_cgroup(pid), unit["cgroup"]):
        with suppress(OSError):
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            if pid != os.getpid() and looks_like_gateway_command_line(cmdline):
                break
        pids.append(pid)
        pid = _ppid(pid)
    bus = "--user" if unit["scope"] == "user" else "--system"
    cmd = ["busctl", bus, "call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
           "org.freedesktop.systemd1.Manager", "StartTransientUnit", "ssa(sv)a(sa(sv))",
           f"hermes-update-{os.getpid()}.scope", "fail", "2", "PIDs", "au", str(len(pids)), *map(str, pids),
           "Description", "s", f"hermes update (outside {unit['unit']} while it is paused)", "0"]
    with suppress(OSError, subprocess.SubprocessError):
        subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                   stdin=subprocess.DEVNULL, timeout=15, check=False)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if (now := _pid_cgroup()) is not None and not _inside(now, unit_cgroup):
            return True
        time.sleep(0.1)
    return False


def _left_running(units: list[dict], jobs: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    """Drop the runtimes whose stop would kill this updater; name each one."""
    from hermes_cli.gateway import _is_pid_ancestor_of_current_process
    keep_units, keep_jobs, notices = [], [], []
    for unit in units:
        if _manage_cmd(unit["scope"]) is None:
            notices.append(f"  ↷ {unit['unit']} keeps running until after the update (stopping a system "
                           "unit needs root or passwordless sudo)")
        elif _escape_cgroup(unit):
            keep_units.append(unit)
        else:
            notices.append(f"  ↷ {unit['unit']} keeps running until after the update (this update could "
                           "not prove it runs outside that unit's cgroup)")
    for job in jobs:
        if (_is_pid_ancestor_of_current_process(job["pid"])
                or os.environ.get("XPC_SERVICE_NAME") == job["label"]):
            notices.append(f"  ↷ {job['label']} keeps running until after the update (this update runs "
                           "inside its launchd job)")
        else:
            keep_jobs.append(job)
    return keep_units, keep_jobs, notices


# --------------------------------------------------------------------------- stop / start

def _manage_cmd(scope: str) -> list | None:
    from hermes_cli.update_cmd_fleet import _needs_sudo, _sudo_noninteractive_ok
    cmd = ["systemctl", "--user"] if scope == "user" else ["systemctl"]
    cmd = cmd + ["--no-ask-password"]
    if scope == "system" and _needs_sudo(scope):
        sudo = ["sudo", "-n", *cmd]
        return sudo if _sudo_noninteractive_ok(cmd + ["show", "-p", "Id", "hermes-gateway"]) else None
    return cmd


def _scope_cmd(unit: dict) -> list:
    return ["systemctl", "--user"] if unit["scope"] == "user" else ["systemctl"]


def _stop_unit(unit: dict) -> None:
    """Stop *unit* through systemd. A stop job systemd is still running holds the pause unless
    the gateway is provably still up: its job outlives our client timeout (a cgroup child that
    ignores SIGTERM keeps it in ``deactivating`` for up to twice TimeoutStopSec), it keeps the
    supervisor from respawning the gateway, and a rollback ``start`` would only queue behind it
    and boot the gateway seconds before the checkout moves."""
    from hermes_cli.update_cmd_fleet import _unit_main_pid
    from hermes_cli.update_cmd_fleet_unit_budget import UNIT_JOB_STILL_RUNNING, run_unit_job
    cmd = _manage_cmd(unit["scope"])
    if cmd is None:
        raise PauseRefused(f"no privilege to stop system unit {unit['unit']}", [unit["unit"]])
    result = run_unit_job(cmd, _scope_cmd(unit), "stop", unit["unit"], timeout=_STOP_TIMEOUT_S)
    if result.returncode == UNIT_JOB_STILL_RUNNING and not _alive(unit["pid"], unit.get("ct")) \
            and _unit_main_pid(_scope_cmd(unit), unit["unit"]) <= 0:
        return
    if result.returncode != 0:
        raise PauseRefused(f"systemctl stop {unit['unit']} failed: {(result.stderr or '').strip()}", [unit["unit"]])


def _start_unit(unit: dict) -> int:
    """Start *unit* through systemd; the new MainPID (raises when it does not come up). The unit
    gets the definition repair the restart it replaces ran first (``_restart_one_systemd_gateway_unit``)."""
    from hermes_cli.update_cmd_fleet import (
        _repair_unit_without_fatal_exit_park, _systemctl, _unit_main_pid, _wait_for_service_active,
    )
    from hermes_cli.update_cmd_fleet_unit_budget import run_unit_job
    cmd = _manage_cmd(unit["scope"])
    if cmd is None:
        raise RuntimeError(f"no privilege to start system unit {unit['unit']}")
    _repair_unit_without_fatal_exit_park(unit["unit"], unit["scope"])
    _systemctl(cmd + ["reset-failed", unit["unit"]], timeout=10)
    scope_cmd = _scope_cmd(unit)
    result = run_unit_job(cmd, scope_cmd, "start", unit["unit"], timeout=_STOP_TIMEOUT_S)
    if result.returncode != 0 or not _wait_for_service_active(scope_cmd, unit["unit"], timeout=15.0):
        raise RuntimeError(f"{unit['unit']} did not start: {(result.stderr or '').strip() or 'not active'}")
    return _unit_main_pid(scope_cmd, unit["unit"])


def _stop_job(job: dict, *, drained: bool = False) -> None:
    """Boot *job* out of launchd. *drained*: its gateway already exited through the drain; the
    bootout only keeps KeepAlive from serving the update."""
    from hermes_cli.gateway import _locate_launchd_gateway_service
    from hermes_cli.update_cmd_windows import _write_update_planned_stop_marker
    if not drained and job.get("home"):
        _write_update_planned_stop_marker(Path(job["home"]), job["pid"])
    result = subprocess.run(["launchctl", "bootout", f"{job['domain']}/{job['label']}"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                   stdin=subprocess.DEVNULL, timeout=90, check=False)
    _wait_gone([job["pid"]], 30.0)
    # KeepAlive may have respawned the drained gateway already: the job, not the old PID, decides.
    if result.returncode != 0 and (_locate_launchd_gateway_service(job["label"])[1] or 0) > 0:
        raise PauseRefused(f"launchctl bootout {job['label']} failed: {(result.stderr or '').strip()}", [job["label"]])


def _start_job(job: dict) -> None:
    """Load and start *job*. The invoking profile's plist is regenerated first, as the restart it
    replaces does (``launchd_restart``); that refresh also loads it."""
    from hermes_cli.gateway import (
        _launchctl_bootstrap, _wait_for_launchd_service_pid, get_launchd_label, refresh_launchd_plist_if_needed,
    )
    if not (job["label"] == get_launchd_label() and refresh_launchd_plist_if_needed()):
        _launchctl_bootstrap(job["domain"], job["plist"], job["label"])
    subprocess.run(["launchctl", "kickstart", f"{job['domain']}/{job['label']}"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                   stdin=subprocess.DEVNULL, timeout=30, check=False)
    if not _wait_for_launchd_service_pid(job["label"], old_pid=job["pid"], timeout=15.0, domain=job["domain"]):
        raise RuntimeError(f"{job['label']} did not come back under launchd")


def _alive(pid: int, ct) -> bool:
    from gateway.status import _pid_exists, get_process_start_time
    try:
        if not _pid_exists(int(pid)):
            return False
    except Exception:  # health: allow BLE001 -- unknown liveness counts as alive (never relaunch a twin)
        return True
    return ct is None or get_process_start_time(int(pid)) in (None, ct)


def _wait_gone(pids, timeout: float, born: dict | None = None) -> set[int]:
    deadline = time.monotonic() + timeout
    live = set(pids)
    while live and time.monotonic() < deadline:
        live = {pid for pid in live if _alive(pid, (born or {}).get(pid))}
        if live:
            time.sleep(0.2)
    return live


def _ask_to_drain(entry: dict) -> bool | None:
    """Ask the gateway to exit only if idle; True on its ACK. Busy/old gateways refuse rather
    than starting an uncancellable pre-swap drain. SIGTERM,
    ``systemctl stop`` and ``launchctl bootout`` enter ``stop()`` at once and cut a turn off. No
    planned-stop marker on this path: the gateway's marker watcher takes that same immediate
    signal-stop path, and could fire before the request lands. The caller records the request
    (``mark_stop_sent``) BEFORE sending it, so a request that reaches the gateway after this update
    died still leaves the evidence that keeps the drained gateway's restart debt."""
    from gateway.control_socket import pause_gateway_for_update
    if not entry.get("home"):
        return False  # no request was sent
    try:
        ack = pause_gateway_for_update(Path(entry["home"]), idle_only=True)
    except Exception:  # health: allow BLE001 -- unknown/busy gateways must not authorize a tree mutation
        return None
    if ack and (ack.get("pausing") or ack.get("already_stopping")):
        return True
    if ack and ack.get("unsupported"):
        entry["pause_unsupported"] = True
    return False if ack and (ack.get("busy") or ack.get("unsupported")) else None


def _stop_at_once(entry: dict) -> None:
    """The stop for a gateway that did not take the drain: its supervisor's, else marker + SIGTERM."""
    from hermes_cli.update_cmd_windows import _write_update_planned_stop_marker
    if entry.get("kind") == "systemd":
        _stop_unit(entry)
    elif entry.get("kind") == "launchd":
        _stop_job(entry)
    else:
        _write_update_planned_stop_marker(Path(entry["home"]), entry["pid"])
        with suppress(ProcessLookupError, PermissionError):
            os.kill(entry["pid"], signal.SIGTERM)


def _drain_budget() -> float:
    """The post-update restart's wait for a drained gateway (after-turn wait + stop drain + headroom)."""
    from hermes_cli.update_cmd_fleet import _gateway_drain_budget
    return _gateway_drain_budget()


def _stop_gateways(token: dict, entries: list[dict]) -> None:
    """Request idle-only stops, then wait within one bounded pre-swap budget.
    A supervised gateway that exited through the drain is stopped through
    its supervisor at once: its exit 75 asks for a restart, which systemd honours after
    ``RestartSec=5`` and launchd immediately, still on the old tree. Past the budget the update
    defers without forcing active work to stop; the checkout must not move."""
    from hermes_cli import update_pause_record as pause_record
    from hermes_cli.update_cmd_drain_report import drain_progress_reporter
    budget, waiting = min(_drain_budget(), _PRE_SWAP_DRAIN_WAIT_S), []
    refusals, refused = [], []
    for entry in entries:
        pause_record.mark_stop_sent(token, entry["pid"])  # before the request: see _ask_to_drain
        acknowledged = _ask_to_drain(entry)
        if acknowledged:
            waiting.append(entry)
        else:
            if acknowledged is False:
                pause_record.mark_stop_refused(token, entry["pid"])
            reason = ("does not support idle pause; restart that gateway on the installed code when idle"
                      if entry.get("pause_unsupported") else "is busy or cannot acknowledge an idle pause; retry when idle")
            refusals.append(f"{_names([entry])} {reason}")
            refused.append(_names([entry]))
            if acknowledged is None:
                waiting.append(entry)  # a lost ACK may already be exiting; settle it before rollback
            break
    if not waiting:
        if refusals:
            raise PauseRefused("; ".join(refusals), refused)
        return
    print(f"  ⏳ Waiting up to {budget:.0f}s for paused gateways {_names(waiting)} to exit before the update...")
    ticks = [drain_progress_reporter(Path(e["home"]) if e.get("home") else None, budget_s=budget) for e in waiting]
    deadline = time.monotonic() + budget
    while waiting and time.monotonic() < deadline:
        for entry in [e for e in waiting if not _alive(e["pid"], e.get("ct"))]:
            waiting.remove(entry)
            _disarm(entry)
        for tick in ticks if waiting else ():
            tick()
        time.sleep(0.2)
    if waiting:
        raise PauseRefused(f"{_names(waiting)} still draining after {budget:.0f}s; retry once in-flight turns finish",
                           [_names([e]) for e in waiting])
    if refusals:
        raise PauseRefused("; ".join(refusals), refused)


def _disarm(entry: dict) -> None:
    """A drained gateway exited: keep its supervisor from bringing it back during the update."""
    if entry.get("kind") == "systemd":
        _stop_unit(entry)
    elif entry.get("kind") == "launchd":
        _stop_job(entry, drained=True)


def _names(entries: list[dict]) -> str:
    return ", ".join(e.get("unit") or e.get("label") or f"gateway PID {e['pid']}" for e in entries)


def _gateway_on_home(home: str, exclude: set[int]) -> int | None:
    """The gateway serving *home*, by the canonical identity reader (PID file + runtime lock,
    then the runtime status record), never a process scan: a replayed ``gateway run`` matches the
    process matcher for the moment before it finds the home already served and exits."""
    from gateway.status import live_gateway_pid_for_home
    try:
        pid = live_gateway_pid_for_home(Path(home))
    except Exception:  # health: allow BLE001 -- unreadable identity files: nothing proven to adopt
        return None
    return int(pid) if pid and int(pid) not in exclude else None


def _replay_env(home: str) -> dict:
    """The environment a replayed gateway starts with, as the restart watcher it replaces chose it
    (``_spawn_gateway_restart_watcher``): a named profile's gateway, replayed by an update launched
    under that same profile, inherits the launcher's environment (an exported bot token may be its
    only credential). The multiplexing root, and any home other than the launcher's, get that home's
    own secrets and none of the launcher's (``served_profile_child_env``)."""
    if (prepared := _REPLAY_ENVS.get(home)) is not None:
        return dict(prepared)
    from hermes_constants import get_default_hermes_root, get_routing_process_hermes_home
    from tools.environments.local import build_subprocess_env, served_profile_child_env
    target = Path(home).resolve()
    if target != get_default_hermes_root().resolve() and target == get_routing_process_hermes_home().resolve():
        return build_subprocess_env(scrub_secrets=False, inherit_profile_home=False, extra={"HERMES_HOME": home})
    return served_profile_child_env(target_home=home, inherit_credentials=True)


def _relaunch_bare(entry: dict) -> int:
    """Replay a bare gateway from its recorded argv under its recorded home; the new PID once it
    is serving. A gateway already serving that home (an earlier attempt's) is adopted instead."""
    if (existing := _gateway_on_home(entry["home"], {int(entry["pid"])})) is not None:
        return existing
    logs = Path(entry["home"]) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    env = _replay_env(entry["home"])
    for leaked in ("INVOCATION_ID", "JOURNAL_STREAM", "HERMES_UPDATE_PAUSED", "_HERMES_GATEWAY"):
        env.pop(leaked, None)
    with open(logs / "gateway-update-resume.log", "ab") as log:
        proc = subprocess.Popen(list(entry["argv"]), env=env, stdin=subprocess.DEVNULL, stdout=log,
                                # detached-spawn: ok — resumes the paused gateway after an update; it must outlive the updater
                                stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    deadline = time.monotonic() + _READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if (pid := _gateway_on_home(entry["home"], {int(entry["pid"])})) is not None:
            return pid  # the replay, or a gateway someone started meanwhile (the replay then exits 0)
        if proc.poll() is not None:
            raise RuntimeError(f"replayed gateway for {entry['home']} exited {proc.returncode} "
                               f"(see {logs / 'gateway-update-resume.log'})")
        time.sleep(0.5)
    raise RuntimeError(f"replayed gateway for {entry['home']} did not come up within {int(_READY_TIMEOUT_S)}s")


def _describe(token: dict) -> str:
    names = [u.get("unit") or u.get("label") for u in token.get("posix_units") or []]
    names += [f"gateway PID {u['pid']}" for u in token.get("unmapped") or [] if u.get("argv")]
    return ", ".join(names)


# --------------------------------------------------------------------------- the commit point

def pause_at_commit_point() -> str | None:
    """Stop this install's gateways before the first checkout move; once per run.

    A failed or overlong drain refuses the move. The sticky verdict also fences later
    commit points in the same update; retrying requires a new run."""
    token = _RUN
    if token is None:
        return None
    if "posix_pause_verdict" not in token:
        token["posix_pause_verdict"] = _pause(token)
    for entry in token.get("unmapped") or []:
        if entry.get("argv") and entry["home"] not in _REPLAY_ENVS:
            try:
                _REPLAY_ENVS[entry["home"]] = _replay_env(entry["home"])
            except Exception as exc:  # health: allow BLE001 -- the replay builds it again when it runs
                print(f"  ⚠ Could not prepare the restart environment for {entry['home']}: {exc}")
    return token["posix_pause_verdict"]


def _pause(token: dict) -> str | None:
    from hermes_cli import update_pause_record as pause_record
    try:
        units, jobs = _discover_systemd(), _discover_launchd()
        units, jobs, notices = _left_running(units, jobs)
        bare, more = _discover_bare({u["pid"] for u in units} | {j["pid"] for j in jobs})
    except Exception as exc:  # health: allow BLE001 -- a discovery that cannot finish stops nothing
        return f"could not list running gateways ({exc}); the checkout was not changed"
    for line in notices + more:
        print(line)
    supervised = units + jobs
    if not supervised and not bare:
        _deferral_streak()
        return None
    from gateway.status import get_process_start_time
    from hermes_cli.update_cmd_windows import _planned_stop_marker_path
    before = copy.deepcopy(token)  # what this attempt may add; a failed record puts it back
    for entry in supervised + bare:
        entry["ct"] = get_process_start_time(entry["pid"])  # native birth stamp: what _alive compares
    token["posix_units"] = [*token.get("posix_units", []), *supervised]
    token["unmapped"] = [*token.get("unmapped", []), *bare]
    token["unmapped_pids"] = [*token.get("unmapped_pids", []), *(e["pid"] for e in bare)]
    # Recovery judges liveness by the canonical identity (``ct:<unix seconds>``), never the native stamp.
    token["identities"] = {**token.get("identities", {}),
                           **{str(e["pid"]): pause_record.identity(e["pid"])["ct"] for e in supervised + bare}}
    token["resume_needed"] = True
    try:
        pause_record.record_pause(token, None, [])
        pause_record.mark_stop_requested(
            token, [e["pid"] for e in supervised + bare],
            markers={e["pid"]: _planned_stop_marker_path(Path(e["home"])) for e in supervised + bare if e.get("home")})
    except Exception as exc:  # health: allow BLE001 -- no durable record, no stop
        # Nothing was stopped: this attempt's id must not arm the checkout's tree gate, whose
        # required record write would then refuse the update. An adopted obligation stays as it was.
        if token.get("pause_id") != before.get("pause_id"):
            with suppress(Exception):
                pause_record.discharge(token)
        token.clear()
        token.update(before)
        return f"could not record gateways to pause ({exc}); the checkout was not changed"
    # From here a refusal or a crash leaves the set owed: the command's own exit resume (registered
    # in ``_cmd_update_impl`` for this token), else the next launch's recovery, restarts it.
    token["posix_stopped"] = True
    try:
        _stop_gateways(token, supervised + bare)
    except BaseException as exc:  # health: allow BLE001 -- interruption must settle before cmd_update releases its fence
        from hermes_cli.update_cmd_windows import _resume_windows_gateways_after_update
        print(f"  ↷ Update deferred: {exc or type(exc).__name__}; the checkout was not changed")
        try:
            _resume_windows_gateways_after_update(token)
        except Exception as again:  # health: allow BLE001 -- still owed: completion restarts them after the deps
            print(f"  ⚠ {again}; restart debt is retained for the next launch")
        for restarted in _RESTARTED.values():
            restarted.clear()  # back on the OLD code: the post-commit fleet restart must refresh them
        pause_record.sync(token)
        if not isinstance(exc, Exception):
            raise
        _deferral_streak(exc)
        return f"gateway pause deferred ({exc}); the checkout was not changed"
    pause_record.sync(token)
    _deferral_streak()
    print(f"  ⏸ Paused for the update: {_describe(token)} (restarted once the dependencies are synced)")
    return None


def _deferral_streak(refusal: BaseException | None = None) -> None:
    """Count this run on each refusing gateway's streak of deferred updates (a third in a row files
    a repair card), or end every streak when the pause went through. Never changes the verdict."""
    try:
        from hermes_cli import update_pause_deferrals as deferrals
        if refusal is None:
            deferrals.note_pause_ok()
        else:
            deferrals.note_deferral(getattr(refusal, "units", ()), str(refusal))
    except Exception as exc:  # health: allow BLE001 -- bookkeeping must not turn a deferral into a crash
        print(f"  ⚠ Could not record the deferral streak: {exc}")


# --------------------------------------------------------------------------- resume

def _root_first(entries: list[dict]) -> tuple[list[dict], dict | None]:
    """*entries* with the root gateway's systemd unit first, plus that entry when profile gateways follow it.

    The host-wide Kanban dispatcher/sync locks go to whichever gateway reaches them first, and a pinned
    profile gateway on an older core never hands them back: on hermes-hub (2026-11-02) askeve-reviewer
    won every joint restart by ~20 s and synced/dispatched for the whole host with its own caps and
    columns. Starting the root first and waiting for its locks is the one ordering that core can't undo. [daveey]"""
    from gateway.kanban_singleton import is_root_home
    root = [e for e in entries if e.get("kind") != "launchd" and is_root_home(e.get("home"))]
    if not root:
        return list(entries), None
    rest = [e for e in entries if e is not root[0]]
    return [root[0], *rest], (root[0] if rest else None)


def _await_root_locks(name: str, pid: int) -> None:
    """Wait (bounded) for the just-started root gateway to hold the host Kanban locks it takes."""
    from gateway.kanban_singleton import ROOT_LOCK_WAIT_SECONDS, wait_for_root_locks
    with suppress(Exception):
        missing = wait_for_root_locks(pid)
        if missing:
            print(f"  ⚠ {name} (PID {pid}) does not hold {', '.join(missing)} after "
                  f"{int(ROOT_LOCK_WAIT_SECONDS)}s; starting the profile gateways anyway "
                  "(`hermes kanban stats` names the holder)")


def resume_paused_set(token: dict) -> None:
    """Start every paused runtime again; each leaves *token* only on its own verified start."""
    from hermes_cli import update_pause_record as pause_record
    # `systemctl start` on a live draining unit succeeds with its OLD PID. That
    # is not a restart: retain its obligation until the recorded incarnation exits.
    pause_record.drop_never_stopped(token)
    draining = pause_record.split_draining(token)
    try:
        _resume_exited_set(token)
    finally:
        token["posix_units"] = [*token.get("posix_units", []), *draining["posix_units"]]
        token["unmapped"] = [*token.get("unmapped", []), *draining["unmapped"]]
        token["unmapped_pids"] = [e["pid"] for e in token["unmapped"]]
        token["profiles"] = {**token.get("profiles", {}), **draining["profiles"]}
        token["resume_needed"] = bool(token["posix_units"] or token["unmapped"] or token["profiles"])


def _resume_exited_set(token: dict) -> None:
    """Resume only exited incarnations; the caller keeps live drain debt even on interruption."""
    failures: list[str] = []
    restarted: list[str] = []
    kept_units = []
    entries, root_pending = _root_first(token.get("posix_units") or [])
    for entry in entries:
        try:
            if entry.get("kind") == "launchd":
                _start_job(entry)
                _RESTARTED["labels"].add(entry["label"])
                name = entry["label"]
            else:
                new_pid = _start_unit(entry)
                _RESTARTED["pids"].add(new_pid)
                _RESTARTED["units"].add(f"{entry['scope']}/{entry['unit']}")
                name = entry["unit"]
                if entry is root_pending:
                    _await_root_locks(name, new_pid)
            restarted.append(name)
            token.setdefault("restarted_services", []).append(name)
        except Exception as exc:  # health: allow BLE001 -- one failed unit never keeps the others stopped
            failures.append(str(exc))
            kept_units.append(entry)
    token["posix_units"] = kept_units
    kept_bare = []
    for entry in token.get("unmapped") or []:
        if not entry.get("argv"):
            continue
        try:
            new_pid = _relaunch_bare(entry)
            _RESTARTED["pids"].add(new_pid)
            restarted.append(f"gateway PID {entry['pid']} → {new_pid}")
        except Exception as exc:  # health: allow BLE001 -- one failed replay never keeps the others stopped
            failures.append(str(exc))
            kept_bare.append(entry)
    token["unmapped"] = kept_bare
    token["unmapped_pids"] = [e["pid"] for e in token["unmapped"]]
    token["profiles"] = {}
    if restarted:
        print(f"  ▶ Restarted paused gateway(s): {', '.join(restarted)}")
    if failures:
        raise RuntimeError("Could not restart every paused gateway: " + "; ".join(failures))
    token["resume_needed"] = bool(token["posix_units"] or token["unmapped"] or token["profiles"])
    token["posix_stopped"] = False


def already_restarted() -> dict:
    """What the early resume restarted on the new code in this process (for the fleet restart)."""
    return _RESTARTED
