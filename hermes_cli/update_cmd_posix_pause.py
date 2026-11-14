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
* an automated caller that already drained what it is willing to wait for (hermes-sync's apply)
  sets ``HERMES_UPDATE_PRESWAP_STOP_BUSY=1`` (Linux only; macOS ignores it and stays idle-only):
  every gateway in the set gets the ordinary drain request, which older gateways know too,
  and what is still up is stopped through its supervisor (a bare one is killed), so a busy host
  still converges. Discovery, drain requests and stops share ONE absolute 30 s deadline (every
  discovery subprocess and every control-socket exchange is clamped to what is left, nothing is
  issued once it passed). A gateway that cannot be listed, managed or proven stopped within it
  refuses the pause: the checkout does not move. The rollback that restarts what was already
  stopped is a separate, mandatory recovery phase and is NOT inside the 30 s.
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
import threading
import time
from contextlib import contextmanager, nullcontext, suppress
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
# Guarded checkpoint requests may already be claimed when their reply is lost.
# Both acknowledged drains and unknown-outcome settlement are bounded at 70s;
# settlement alone never authorizes mutation.
_CHECKPOINT_DRAIN_WAIT_S = 70.0


#: Opt-in for automated callers: stop busy gateways after the bounded wait instead of deferring.
STOP_BUSY_ENV = "HERMES_UPDATE_PRESWAP_STOP_BUSY"


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _stop_busy_requested() -> bool:
    return os.environ.get(STOP_BUSY_ENV, "").strip().lower() in ("1", "true", "yes")


def _stop_busy_opted_in() -> bool:
    """Linux only: macOS keeps the idle-only pause (a busy gateway defers the update) whatever the
    environment says, because its launchd stop and verification are not bounded by this transaction."""
    return _stop_busy_requested() and _is_linux()


class PauseRefused(RuntimeError):
    """The pause could not be completed; everything it stopped was started again. ``units``
    names the gateways that refused (``update_pause_deferrals`` counts their streaks)."""

    def __init__(self, message: str = "", units=()):
        super().__init__(message)
        self.units = list(units)


class StopUnsettled(PauseRefused):
    """A ``--force`` stop is still running (a supervisor's stop job, a gateway that outlived
    SIGKILL): nothing may be started behind it, so the update stops before the checkout moves."""


def _empty_token() -> dict:
    return {"platform": "posix", "resume_needed": False, "profiles": {}, "unmapped_pids": [],
            "unmapped": [], "posix_units": []}


def arm_pause(*, no_gateway_restart: bool, force: bool = False) -> dict | None:
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
    if force:
        token["force"] = True  # ``hermes update --force``: stop at once, no drain (update_cmd_force_stop)
    _RUN = token
    return token


def gateways_paused() -> bool:
    """True while this run holds gateways stopped (no gateway can answer a chat prompt)."""
    return bool(_RUN and _RUN.get("posix_stopped") and _RUN.get("resume_needed"))


# --------------------------------------------------------------------------- discovery

@contextmanager
def _subprocess_budget(deadline: float):
    """Clamp every ``subprocess.run`` of THIS thread to what *deadline* leaves (discovery drives many
    helpers, each with its own fixed timeout); yields the list of commands that hit the deadline or
    timed out, which callers must read as an unfinished listing, never as an empty one."""
    real_run, owner, timed_out = subprocess.run, threading.get_ident(), []

    def run(*args, **kwargs):
        if threading.get_ident() != owner:
            return real_run(*args, **kwargs)
        cmd, left = (args[0] if args else kwargs.get("args")), _left(deadline)
        if left <= 0:
            timed_out.append(cmd)
            raise subprocess.TimeoutExpired(cmd, 0)
        kwargs["timeout"] = left if kwargs.get("timeout") is None else min(kwargs["timeout"], left)
        try:
            return real_run(*args, **kwargs)
        except subprocess.TimeoutExpired:
            timed_out.append(cmd)
            raise
    subprocess.run = run
    try:
        yield timed_out
    finally:
        subprocess.run = real_run


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
    if _stop_busy_opted_in():
        from hermes_cli.update_preswap_discovery import systemd_units
        return systemd_units()
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
    from hermes_cli.update_fleet_scope import launchd_label_skip_notice
    derived = launchd_gateway_labels_for_install()
    jobs = []
    for label in derived + legacy_launchd_labels_for_install(exclude=set(derived)):
        if launchd_label_skip_notice(label) is not None:
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


def home_served_by_other(pid: int, home) -> int | None:
    """The verified gateway PID serving *home* when it is not *pid*, else None.

    A ``gateway run`` that is not its home's canonical gateway (PID file + runtime lock, then the
    runtime status record) cannot serve it: a duplicate start exits once it finds the home served.
    The process scan still sees it for that moment; pausing it as a second gateway made the update
    drain the real one, then refuse on the duplicate and restart the real one on the old code
    (hermes-hub 2026-11-12 12:29, 13:15, 13:55Z). Unprovable identity returns None (kept, as before)."""
    from gateway.status import live_gateway_pid_for_home
    try:
        served = live_gateway_pid_for_home(Path(home))
    except Exception:  # health: allow BLE001 -- unreadable identity proves nothing either way
        return None
    return int(served) if served and int(served) != int(pid) else None


def _discover_bare(service_pids: set[int]) -> tuple[list[dict], list[str]]:
    """``(bare gateways, notices)``: this install's ``gateway run`` processes outside any unit."""
    if _stop_busy_opted_in():
        from hermes_cli.update_preswap_discovery import bare_gateways
        return bare_gateways(service_pids)
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
        if home and not marker and (served := home_served_by_other(pid, home)) is not None:
            notices.append(f"  ↷ gateway PID {pid} is not the gateway of {home} (PID {served} serves it; "
                           "a duplicate start exits on its own): not paused")
            continue
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


#: The pre-swap deadline while an opted-in discovery runs (bounds the escape probe's own wait loop).
_DISCOVERY_DEADLINE: float | None = None


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
    if _DISCOVERY_DEADLINE is not None:
        deadline = min(deadline, _DISCOVERY_DEADLINE)
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

#: ``_manage_cmd`` answers for this pause (its sudo probes are subprocesses): resolved once, during discovery.
_MANAGE_CMDS: dict[str, list | None] = {}


def _manage_cmd(scope: str) -> list | None:
    if scope not in _MANAGE_CMDS:
        _MANAGE_CMDS[scope] = _resolve_manage_cmd(scope)
    return _MANAGE_CMDS[scope]


def _resolve_manage_cmd(scope: str) -> list | None:
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


def unit_stop_pending(unit: dict) -> bool:
    """A systemd unit whose stop has not settled (``deactivating``, or a job still queued): a start
    now would queue behind it. Unreadable counts as pending; a launchd job is never held here."""
    from hermes_cli.update_cmd_fleet_unit_budget import _settled, unit_job_state
    return unit.get("kind") == "systemd" and not _settled(unit_job_state(_scope_cmd(unit), unit["unit"]))


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


def _checkpoint_nonce(entry: dict) -> str | None:
    """The nonce of a valid, live explicit instant-update intent for this gateway's home, else
    ``None`` (never inferred from an environment flag; any doubt keeps the idle-only contract)."""
    if not entry.get("home"):
        return None
    try:
        from gateway.update_checkpoint import checkpoint_intent
        intent = checkpoint_intent(Path(entry["home"]))
    except Exception:  # health: allow BLE001 -- no provable intent means idle-only
        return None
    nonce = intent.get("nonce") if isinstance(intent, dict) else None
    return nonce if isinstance(nonce, str) and nonce else None


def _valid_checkpoint_ack(ack: dict, entry: dict, nonce: str) -> bool:
    """A checkpoint ACK is believed only when it is from the gateway we asked (pid), for this
    episode (required nonce echo), names the checkpoint, and states a sane bounded drain."""
    drain = ack.get("drain_timeout")
    return (ack.get("checkpoint") is True and type(ack.get("pid")) is int and _is_entry_gateway(ack["pid"], entry)
            and ack.get("nonce") == nonce
            and isinstance(drain, (int, float)) and not isinstance(drain, bool) and 0 < drain)


def _is_entry_gateway(pid: int, entry: dict) -> bool:
    """*pid* is the gateway behind *entry*: its own PID, or (a unit whose ``ExecStart`` is a non-exec
    wrapper, e.g. a worker node's ``hermes-gateway-broker``) a ``gateway run`` process below it. Such a
    unit's MainPID is the wrapper, while the gateway answers with its own PID; requiring equality refused
    every accepted checkpoint as "unbelievable" after the gateway had already drained its Kanban workers
    (hermes-worker-5 2026-11-12 15:15-15:18Z: four instant applies, four restarts on the old code)."""
    if pid == entry.get("pid"):
        return True
    from hermes_cli.gateway_service_members import gateway_descendant_pids
    owner = entry.get("pid")
    return type(owner) is int and pid in gateway_descendant_pids(owner)


def _ask_to_drain(entry: dict) -> bool | None:
    """Ask the gateway to exit only if idle; True on its ACK. Busy/old gateways refuse rather
    than starting an uncancellable pre-swap drain. SIGTERM,
    ``systemctl stop`` and ``launchctl bootout`` enter ``stop()`` at once and cut a turn off. No
    planned-stop marker on this path: the gateway's marker watcher takes that same immediate
    signal-stop path, and could fire before the request lands. The caller records the request
    (``mark_stop_sent``) BEFORE sending it, so a request that reaches the gateway after this update
    died still leaves the evidence that keeps the drained gateway's restart debt.

    With an explicit live instant-update intent (``checkpoint_intent``) the request is the guarded
    ``checkpoint-for-update`` carrying its nonce: the gateway itself refuses unknown reservations
    or work. Only a validated checkpoint ACK sets ``entry['checkpoint_drain_timeout']``, which
    ``_stop_gateways`` turns into the longer wait. There is no forced-stop fallback."""
    from gateway.control_socket import pause_gateway_for_update, query_gateway_control
    if not entry.get("home"):
        return False  # no request was sent
    entry.pop("checkpoint_drain_timeout", None)
    entry.pop("checkpoint_settle_timeout", None)
    entry.pop("refusal_reason", None)
    nonce = _checkpoint_nonce(entry)
    try:
        if nonce is not None:
            # A lost reply does not revoke a request already claimed by the loop.
            # This extends settlement only, never authorizes a checkout move.
            entry["checkpoint_settle_timeout"] = _CHECKPOINT_DRAIN_WAIT_S
            ack = query_gateway_control(Path(entry["home"]), "checkpoint-for-update",
                                        params={"nonce": nonce}, timeout=8.0, report_unsupported=True)
            if ack and ack.get("unsupported"):  # old gateway: the stricter idle-only request still applies
                entry.pop("checkpoint_settle_timeout", None)
                ack = pause_gateway_for_update(Path(entry["home"]), idle_only=True)
            elif ack and (ack.get("pausing") or ack.get("already_stopping")):
                if not _valid_checkpoint_ack(ack, entry, nonce):
                    return None  # unbelievable ACK: may be exiting; settle it, never extend the wait
                if ack.get("pausing"):
                    entry["checkpoint_drain_timeout"] = float(ack["drain_timeout"])
                return True
            elif ack and ack.get("busy"):
                # The cause is printed with the refusal (core's ``checkpoint_for_update`` reason).
                entry["refusal_reason"] = ("gateway refused the update checkpoint: "
                                           + str(ack.get("reason") or "no reason given (older gateway)"))
        else:
            ack = pause_gateway_for_update(Path(entry["home"]), idle_only=True)
    except Exception as exc:  # health: allow BLE001 -- unknown/busy gateways must not authorize a tree mutation
        entry["refusal_reason"] = f"did not answer the pause request ({type(exc).__name__}: {exc})"
        return None
    if ack and (ack.get("pausing") or ack.get("already_stopping")):
        return True
    if ack and ack.get("unsupported"):
        entry["pause_unsupported"] = True
    if not ack:
        entry.setdefault("refusal_reason", "gave no answer on its control socket")
    return False if ack and (ack.get("busy") or ack.get("unsupported")) else None


def _ask_busy_to_drain(entry: dict, timeout: float) -> bool:
    """The ordinary drain request (``request_restart``: in-flight turns get the gateway's own drain
    window and keep their resume markers); True on its ACK. Only for the opted-in stop: unlike
    the idle-only verb it cannot be taken back, and gateways older than that verb answer it.
    *timeout* is what the transaction deadline leaves for this one request."""
    from gateway.control_socket import pause_gateway_for_update
    if not entry.get("home") or timeout <= 0:
        return False
    try:
        ack = pause_gateway_for_update(Path(entry["home"]), timeout=timeout)
    except Exception:  # health: allow BLE001 -- no answer: the supervisor stop below handles it
        return False
    return bool(ack and (ack.get("pausing") or ack.get("already_stopping")))


#: The opted-in stop keeps this much of its deadline (at most) for the supervisor stops and kills,
#: so a gateway that is still draining when the wait ends has time to be stopped and verified.
_STOP_RESERVE_S = 8.0
_POLL_S = 0.2


def _left(deadline: float) -> float:
    return deadline - time.monotonic()


def _unit_state(unit: dict, deadline: float) -> dict[str, str]:
    """``ActiveState``/``MainPID``/``Job`` of *unit*, read within the deadline; ``{}`` when unknown."""
    from hermes_cli.update_cmd_fleet import _systemctl
    if (left := _left(deadline)) <= 0:
        return {}
    try:
        show = _systemctl(_scope_cmd(unit) + ["show", unit["unit"], "--property=ActiveState,SubState,MainPID,Job"],
                          timeout=min(5.0, left))
    except (OSError, subprocess.SubprocessError):
        return {}
    if show.returncode != 0:
        return {}
    return dict(line.partition("=")[::2] for line in (show.stdout or "").splitlines() if "=" in line)


def _systemctl_async(unit: dict, args: list[str], deadline: float, *, must: bool) -> None:
    """Queue a unit job without waiting for it (``stop --no-block``) or signal it (``kill``); the
    client call itself is bounded by the deadline. *must*: a failure to queue refuses."""
    from hermes_cli.update_cmd_fleet import _systemctl
    cmd = _manage_cmd(unit["scope"])
    if cmd is None:
        raise PauseRefused(f"no privilege to stop system unit {unit['unit']}", [unit["unit"]])
    if (left := _left(deadline)) <= 0:  # no floor: an expired deadline issues nothing
        if must:
            raise PauseRefused(f"the pre-swap deadline passed before systemctl {args[0]} {unit['unit']}", [unit["unit"]])
        return
    try:
        result = _systemctl(cmd + [*args, unit["unit"]], timeout=min(10.0, left))
    except (OSError, subprocess.SubprocessError) as exc:
        if must:
            raise PauseRefused(f"systemctl {args[0]} {unit['unit']} did not complete: {exc}", [unit["unit"]]) from exc
        return
    if must and result.returncode != 0:
        raise PauseRefused(f"systemctl {args[0]} {unit['unit']} failed: {(result.stderr or '').strip()}", [unit["unit"]])


def _begin_launchd_bootout(job: dict) -> subprocess.Popen:
    return subprocess.Popen(["launchctl", "bootout", f"{job['domain']}/{job['label']}"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _stop_at_once(entry: dict, deadline: float, procs: dict, *, drained: bool = False) -> None:
    """Start the stop of a gateway that did not take the drain, without waiting for it: its
    supervisor's job is only queued (``stop --no-block``; a launchd bootout runs in the
    background), a bare process gets marker + SIGTERM. ``_stopped`` verifies the outcome."""
    from hermes_cli.update_cmd_windows import _write_update_planned_stop_marker
    if _left(deadline) <= 0:
        raise PauseRefused(f"the pre-swap deadline passed before stopping {_names([entry])}", [_names([entry])])
    kind = entry.get("kind")
    if kind == "systemd":
        _systemctl_async(entry, ["stop", "--no-block"], deadline, must=True)
        return
    if entry.get("home") and not drained:
        _write_update_planned_stop_marker(Path(entry["home"]), entry["pid"])
    if kind == "launchd":
        procs[entry["pid"]] = _begin_launchd_bootout(entry)
    else:
        with suppress(ProcessLookupError, PermissionError):
            os.kill(entry["pid"], signal.SIGTERM)


def _force(entry: dict, deadline: float, procs: dict) -> None:
    """The last resort inside the deadline: SIGKILL through the supervisor (the stop job then
    finishes at once), or straight at a launchd/bare process."""
    if _left(deadline) <= 0:
        return
    if entry.get("kind") == "systemd":
        _systemctl_async(entry, ["kill", "--kill-whom=all", "--signal=SIGKILL"], deadline, must=False)
        return
    if (proc := procs.get(entry["pid"])) is not None and proc.poll() is None:
        with suppress(OSError):
            proc.kill()
    with suppress(ProcessLookupError, PermissionError):
        os.kill(entry["pid"], getattr(signal, "SIGKILL", signal.SIGTERM))


def _stopped(entry: dict, deadline: float, procs: dict) -> bool:
    """Verified stopped: the process incarnation is gone and, for a supervised gateway, the
    supervisor holds no job or restart for it (so nothing respawns it on the old tree)."""
    if _alive(entry["pid"], entry.get("ct")):
        return False
    if entry.get("kind") == "systemd":
        state = _unit_state(entry, deadline)
        return state.get("ActiveState") in ("inactive", "failed") and not state.get("Job") \
            and state.get("MainPID", "0") in ("0", "")
    if entry.get("kind") == "launchd":
        from hermes_cli.gateway import _locate_launchd_gateway_service
        if (proc := procs.get(entry["pid"])) is not None and proc.poll() is None:
            return False
        return (_locate_launchd_gateway_service(entry["label"])[1] or 0) <= 0
    return True


def _stop_busy_gateways(token: dict, entries: list[dict], deadline: float) -> None:
    """The opted-in stop: ONE transaction against the absolute *deadline*, which the caller started
    before discovery and the serial drain requests. Every gateway is asked to drain; one wait for
    all of them ends early enough to leave a reserve; the survivors are then stopped through their
    supervisor (a bare one is killed) and finally killed outright; every stop is queued, never
    awaited blocking, and verified. Anything not verified stopped at the deadline refuses, and the
    caller rolls everything back: the checkout never moves with a gateway possibly still running."""
    from hermes_cli import update_pause_record as pause_record
    total = max(0.0, _left(deadline))
    reserve = min(_STOP_RESERVE_S, total * 0.3)
    drain_end, force_at = deadline - reserve, deadline - reserve / 2
    procs: dict[int, subprocess.Popen] = {}
    waiting: list[dict] = []   # drain accepted: expected to exit on its own
    settling: list[dict] = []  # a stop is under way: must be verified
    try:
        for index, entry in enumerate(entries):
            if _left(deadline) <= 0:
                raise PauseRefused(f"the pre-swap deadline passed before {_names([entry])} was asked to stop; "
                                   "the checkout was not changed", [_names([e]) for e in entries[index:]])
            pause_record.mark_stop_sent(token, entry["pid"])  # before the request: see _ask_to_drain
            if _ask_busy_to_drain(entry, min(2.0, _left(drain_end))):
                waiting.append(entry)
            else:
                _stop_at_once(entry, deadline, procs)
                settling.append(entry)
        if waiting:
            print(f"  ⏳ Waiting up to {max(0.0, _left(drain_end)):.0f}s for in-flight turns on {_names(waiting)}, "
                  "then stopping them for the update...")
        while waiting and _left(drain_end) > 0:
            for entry in [e for e in waiting if not _alive(e["pid"], e.get("ct"))]:
                waiting.remove(entry)
                if entry.get("kind"):
                    _stop_at_once(entry, deadline, procs, drained=True)  # keep the supervisor from bringing it back
                settling.append(entry)
            if waiting:
                time.sleep(_POLL_S)
        if waiting:
            print(f"  ↷ Stopping {_names(waiting)} with work still running (the caller asked for it)")
        for entry in waiting:
            _stop_at_once(entry, deadline, procs, drained=True)
            settling.append(entry)
        forced: set[int] = set()
        while True:
            settling = [e for e in settling if not _stopped(e, deadline, procs)]
            if not settling:
                return
            if _left(deadline) <= 0:
                break
            if _left(force_at) <= 0:
                for entry in settling:
                    if entry["pid"] not in forced:
                        forced.add(entry["pid"])
                        _force(entry, deadline, procs)
            time.sleep(min(_POLL_S, max(0.0, _left(deadline))))
        raise PauseRefused(f"{_names(settling)} did not stop within {total:.0f}s; the checkout was not changed",
                           [_names([e]) for e in settling])
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                with suppress(OSError):
                    proc.kill()


def _drain_budget() -> float:
    """The post-update restart's wait for a drained gateway (after-turn wait + stop drain + headroom)."""
    from hermes_cli.update_cmd_fleet import _gateway_drain_budget
    return _gateway_drain_budget()


def _stop_gateways(token: dict, entries: list[dict], deadline: float | None = None, *, fence_checked: bool = False) -> None:
    """Request idle-only stops, then wait within one bounded pre-swap budget.
    A supervised gateway that exited through the drain is stopped through
    its supervisor at once: its exit 75 asks for a restart, which systemd honours after
    ``RestartSec=5`` and launchd immediately, still on the old tree. Past the budget the update
    defers without forcing active work to stop; the checkout must not move."""
    from hermes_cli import update_pause_record as pause_record
    from hermes_cli.update_cmd_drain_report import drain_progress_reporter
    from hermes_cli.update_gateway_fence import parked_launchd_launcher
    if token.get("force"):
        from hermes_cli.update_cmd_force_stop import force_stop_gateways
        return force_stop_gateways(token, entries)
    if _stop_busy_opted_in():
        return _stop_busy_gateways(token, entries, deadline if deadline is not None
                                   else time.monotonic() + _PRE_SWAP_DRAIN_WAIT_S)
    budget, waiting = min(_drain_budget(), _PRE_SWAP_DRAIN_WAIT_S), []
    refusals, refused = [], []
    for entry in entries:
        if not fence_checked and parked_launchd_launcher(entry):
            continue  # the ancestor fence owns starting and verifying this job
        pause_record.mark_stop_sent(token, entry["pid"])  # before the request: see _ask_to_drain
        acknowledged = _ask_to_drain(entry)
        if acknowledged:
            waiting.append(entry)
        else:
            if acknowledged is False:
                pause_record.mark_stop_refused(token, entry["pid"])
            # "busy or cannot acknowledge an idle pause" is the token hermes-sync matches to retry (keep it);
            # the bracketed cause names the refusing check.
            cause = entry.get("refusal_reason")
            reason = ("does not support idle pause; restart that gateway on the installed code when idle"
                      if entry.get("pause_unsupported") else
                      "is busy or cannot acknowledge an idle pause" + (f" [cause: {cause}]" if cause else "")
                      + "; retry when idle")
            refusals.append(f"{_names([entry])} {reason}")
            refused.append(_names([entry]))
            if acknowledged is None:
                waiting.append(entry)  # a lost ACK may already be exiting; settle it before rollback
            break
    if not waiting:
        if refusals:
            raise PauseRefused("; ".join(refusals), refused)
        return
    if acked := [e.get("checkpoint_drain_timeout") or e.get("checkpoint_settle_timeout")
                 for e in waiting if e.get("checkpoint_drain_timeout") or e.get("checkpoint_settle_timeout")]:
        budget = max(budget, min(max(acked), _CHECKPOINT_DRAIN_WAIT_S))
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


def _bare_replay_env(home: str) -> dict:
    """The replayed gateway's env: no supervisor marker of the updater's (an ``/update`` sent to a
    launchd/systemd gateway carries them), or the replay believes the service launched it and skips
    the supervised-gateway guard (#73480). Its profile pin rides on."""
    from hermes_cli.gateway_runtime_start import strip_supervisor_markers
    env = strip_supervisor_markers(_replay_env(home))
    for leaked in ("HERMES_UPDATE_PAUSED", "_HERMES_GATEWAY", STOP_BUSY_ENV):
        env.pop(leaked, None)
    return env


def _relaunch_bare(entry: dict) -> int:
    """Replay a bare gateway from its recorded argv under its recorded home; the new PID once it
    is serving. A gateway already serving that home (an earlier attempt's) is adopted instead."""
    if (existing := _gateway_on_home(entry["home"], {int(entry["pid"])})) is not None:
        return existing
    logs = Path(entry["home"]) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    env = _bare_replay_env(entry["home"])
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
    commit points in the same update; retrying requires a new run. Under ``--force`` a stop that
    has not settled (``StopUnsettled``) refuses as well, before git writes a file."""
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
    global _DISCOVERY_DEADLINE
    deadline = time.monotonic() + _PRE_SWAP_DRAIN_WAIT_S  # one clock for discovery, drain requests and stops
    opted_in = _stop_busy_opted_in()
    if _stop_busy_requested() and not opted_in:
        print(f"  ↷ {STOP_BUSY_ENV} is Linux-only; this host keeps the idle-only pause (busy gateways defer the update)")
    _MANAGE_CMDS.clear()
    budget = _subprocess_budget(deadline) if opted_in else nullcontext([])
    # An adopted set an earlier ``--force`` left mid-stop: its unit reads MainPID 0, so discovery
    # skips it, and the swap plus the resume would start it behind that unfinished stop job.
    if held := [u for u in token.get("posix_units") or [] if u.get("stop_unsettled") and unit_stop_pending(u)]:
        return _abort_unsettled(token, f"{_names(held)} is still stopping from an earlier `hermes update --force`")
    try:
        _DISCOVERY_DEADLINE = deadline if opted_in else None
        with budget as timed_out:
            units, jobs = _discover_systemd(), _discover_launchd()
            units, jobs, notices = _left_running(units, jobs)
            bare, more = _discover_bare({u["pid"] for u in units} | {j["pid"] for j in jobs})
    except Exception as exc:  # health: allow BLE001 -- a discovery that cannot finish stops nothing
        if opted_in:
            _deferral_streak(exc if isinstance(exc, PauseRefused) else
                             PauseRefused(str(exc), ["gateway inventory"]))
        return f"could not list running gateways ({exc}); the checkout was not changed"
    finally:
        _DISCOVERY_DEADLINE = None
    if opted_in:
        # A listing that ran out of time or lost a probe is unfinished, never an empty (or smaller) set.
        if timed_out or _left(deadline) <= 0:
            _deferral_streak(PauseRefused("deadline passed while listing gateways", ["gateway inventory"]))
            return "the pre-swap deadline passed while listing gateways; the checkout was not changed"
        if notices or more:
            # The caller asked for this host to converge: a gateway the update cannot stop is a refusal.
            why = "; ".join(line.strip().lstrip("↷ ").strip() for line in notices + more)
            _deferral_streak(PauseRefused(why, [line.strip().lstrip("↷ ").split(" keeps running", 1)[0]
                                              for line in notices + more]))
            return f"gateways that cannot be stopped for the update ({why}); the checkout was not changed"
    for line in notices + more:
        print(line)
    from hermes_cli.update_gateway_fence import parked_launchd_launcher
    # Do not acquire restart custody for a job already stopped by the caller's fence.
    supervised = [entry for entry in units + jobs if not parked_launchd_launcher(entry)]
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
        # Exemption and restart custody were decided together before recording. Re-evaluating
        # here could skip a newly parked launcher while retaining its durable restart debt.
        _stop_gateways(token, supervised + bare, deadline, fence_checked=True)
    except StopUnsettled as exc:
        return _abort_unsettled(token, str(exc))
    except BaseException as exc:  # health: allow BLE001 -- interruption must settle before cmd_update releases its fence
        from hermes_cli.update_cmd_windows import _resume_windows_gateways_after_update
        print(f"  ↷ Update deferred: {exc or type(exc).__name__}; the checkout was not changed")
        # A gateway that refused the pause left the set (``mark_stop_refused``): it was never stopped and owes
        # no restart. Say so instead of announcing a restart of nothing (hermes-hub 2026-11-12 18:11Z).
        if token.get("posix_units") or token.get("unmapped") or token.get("profiles"):
            print("  ↻ Recovery (a separate phase after the stop deadline, not bounded by it): "
                  f"restarting {_describe(token) or 'the stopped gateways'} on the old code...")
        else:
            print("  ↻ No gateway was stopped (each refused or was never asked); nothing to restart")
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


def _abort_unsettled(token: dict, why: str) -> str:
    """The commit point's refusal for a ``--force`` stop still in flight. A rollback start would
    queue behind that stop (or race the gateway still exiting) and the swap would follow, so neither
    runs: this process restarts nothing; the durable record stays owed for the next launch's
    recovery, which holds an entry until its process has exited and its unit's stop has settled."""
    from hermes_cli import update_pause_record as pause_record
    token["resume_needed"] = False  # no exit resume in this process (it would start behind the stop)
    for unit in token.get("posix_units") or []:
        if unit.get("kind") == "systemd":
            unit["stop_unsettled"] = True  # durable: recovery and the next update hold it while it stops
    token["resume_deferred"] = why  # ``sync`` keeps the record owed for the next launch
    pause_record.sync(token)
    return (f"--force could not finish stopping the gateways: {why}. The update stopped before "
            "changing the checkout; the stopped gateways restart at the next `hermes` command once "
            "their stop has completed, then run `hermes update --force` again")


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
