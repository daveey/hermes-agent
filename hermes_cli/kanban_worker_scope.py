"""Retain failed worker scopes until their cause is durably booked by the reaper.

Only Kanban opts out of --collect. Successful scopes still collect automatically;
failed scopes retain systemd's Result/MemoryPeak even after their cgroup is gone.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
import subprocess
import sys

SNAPSHOT_PREFIX = "[kanban-worker-scope] "


def emit_exit_snapshot() -> None:
    """Save cgroup peak before a SIGTERM-handling worker exits and systemd removes it.

    Older systemd versions retain Result but not MemoryPeak. No snapshot is
    possible when the worker itself takes SIGKILL; that peak stays unknown.
    """
    task_id = os.environ.get("HERMES_KANBAN_TASK", "")
    run_id = os.environ.get("HERMES_KANBAN_RUN_ID", "")
    if not task_id or not run_id.isdecimal():
        return
    unit = f"hermes-worker-kanban-{task_id}-run-{run_id}.scope"
    try:
        relative = next(line[3:] for line in Path("/proc/self/cgroup").read_text(encoding="utf-8-sig").splitlines()
                        if line.startswith("0::") and line.endswith("/" + unit))
        cgroup = Path("/sys/fs/cgroup") / relative.lstrip("/")
        values: dict = {key: _bytes((cgroup / filename).read_text(encoding="utf-8-sig").strip()) for key, filename in (
            ("memory_max_bytes", "memory.max"), ("memory_peak_bytes", "memory.peak"))}
        events = dict(line.split() for line in (cgroup / "memory.events").read_text(encoding="utf-8-sig").splitlines())
        values.update(systemd_unit=unit, oom_kill=int(events.get("oom_kill", "0")))
        print(SNAPSHOT_PREFIX + json.dumps(values), file=sys.stderr, flush=True)
    except (OSError, ValueError, StopIteration):
        return


def _logged_snapshot(log: str, unit: str) -> dict:
    for line in reversed(log.splitlines()):
        if not line.startswith(SNAPSHOT_PREFIX):
            continue
        try:
            data = json.loads(line[len(SNAPSHOT_PREFIX):])
            if not isinstance(data, dict) or data.get("systemd_unit") != unit:
                continue
            return {key: value for key, value in data.items()
                    if key in {"memory_max_bytes", "memory_peak_bytes", "oom_kill"}
                    and type(value) is int and 0 <= value < (1 << 64) - 1}
        except ValueError:
            continue
    return {}


def retain_failed_scope(argv: list[str]) -> list[str]:
    """Remove systemd-run's --collect, never a worker argument of the same name."""
    if not argv or Path(argv[0]).name != "systemd-run" or "--scope" not in argv or "--" not in argv:
        return argv
    boundary = argv.index("--")
    return [arg for arg in argv[:boundary] if arg != "--collect"] + argv[boundary:]


def _systemctl(*args: str) -> subprocess.CompletedProcess | None:
    from tools.process_registry import systemd_user_bus_env

    env = systemd_user_bus_env()
    if env is None:
        return None
    try:
        return subprocess.run(
            ["systemctl", "--user", *args], env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # Non-systemd hosts, unavailable bus, or old systemd: preserve exit-code handling.
        return None


def read_failure(task_id: str, run_id: int | None, log: str = "") -> dict | None:
    if run_id is None:
        return None
    unit = f"hermes-worker-kanban-{task_id}-run-{run_id}.scope"
    proc = _systemctl("show", unit, "-p", "LoadState", "-p", "Result",
                      "-p", "MemoryMax", "-p", "MemoryPeak")
    props = (dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
             if proc is not None and not proc.returncode else {})
    snapshot = _logged_snapshot(log, unit)
    result = props.get("Result") if props.get("LoadState") == "loaded" else None
    if snapshot.get("oom_kill", 0) > 0:
        result = "oom-kill"
    if result not in {"oom-kill", "signal"}:
        return None
    return {
        "systemd_unit": unit, "scope_result": result,
        "memory_max_bytes": snapshot.get("memory_max_bytes", _bytes(props.get("MemoryMax", ""))),
        "memory_peak_bytes": snapshot.get("memory_peak_bytes", _bytes(props.get("MemoryPeak", ""))),
    }


def _bytes(raw: str) -> int | None:
    # UINT64_MAX is systemd's unset sentinel; don't report an exabyte-scale peak.
    return int(raw) if raw.isdecimal() and int(raw) < (1 << 64) - 1 else None


def _size(value: int | None) -> str:
    return f"{value / (1024 * 1024):g} MiB" if value is not None else "unknown"


def describe(failure: dict) -> str:
    limit = _size(failure["memory_max_bytes"])
    peak = _size(failure["memory_peak_bytes"])
    if failure["scope_result"] == "oom-kill":
        return (f"Worker was OOM-killed at {limit} (peak {peak}); run memory-heavy steps "
                "under `systemd-run --user --scope -p MemoryMax=…` or reduce the footprint.")
    return f"Worker scope died from a signal (MemoryMax {limit}; peak {peak})."


def release_failure(metadata: dict) -> None:
    """Called only after run/error accounting commits; reset-failed allows scope GC."""
    unit = metadata.get("systemd_unit")
    if unit:
        _systemctl("reset-failed", unit)


# --- Stop a finished run's scope ------------------------------------------------
# A worker's scope outlives the worker: anything it left behind (a background
# pytest whose wrapper the agent pkill'ed, ``ssh-agent -s``, a ``nohup … &``)
# stays in ``hermes-worker-kanban-<task>-run-<n>.scope`` and nothing stopped the
# scope, so orphans held hub test slots for hours and scopes piled up for days.
# Each dispatch tick now stops the scope of every run of this board that ended
# more than ``RUN_SCOPE_STOP_GRACE_SECONDS`` ago and whose worker is gone.
# Jobs the worker started with ``persist_on_release`` (recorded in any profile's
# ``processes.json``) and their descendants are spared: such a scope gets every
# other process signalled instead of a unit stop. [daveey]
RUN_SCOPE_STOP_GRACE_SECONDS = 600
RUN_SCOPE_SWEEP_INTERVAL_SECONDS = 300
_SCOPE_UNIT_PREFIX = "hermes-worker-kanban-"
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_last_scope_sweep: dict[str, float] = {}


def _parse_scope_unit(unit: str) -> tuple[str, int] | None:
    import re

    m = re.fullmatch(r"hermes-worker-kanban-(?P<task>.+)-run-(?P<run>\d+)\.scope", unit)
    return (m.group("task"), int(m.group("run"))) if m else None


def _active_run_scopes() -> list[str] | None:
    proc = _systemctl("list-units", "--type=scope", "--state=active", "--plain",
                      "--no-legend", "--no-pager", _SCOPE_UNIT_PREFIX + "*")
    if proc is None or proc.returncode:
        return None
    return [line.split()[0] for line in proc.stdout.splitlines()
            if line.split() and line.split()[0].startswith(_SCOPE_UNIT_PREFIX)]


def _scope_pids(unit: str) -> list[int] | None:
    proc = _systemctl("show", unit, "-p", "ControlGroup", "--value")
    if proc is None or proc.returncode or not proc.stdout.strip():
        return None
    try:
        raw = (_CGROUP_ROOT / proc.stdout.strip().lstrip("/") / "cgroup.procs").read_text(encoding="utf-8-sig")
    except OSError:
        return None
    return [int(p) for p in raw.split() if p.isdecimal()]


def _proc_stat(pid: int) -> tuple[int, int] | None:
    """(ppid, start ticks) from /proc/<pid>/stat, or None when the pid is gone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8-sig")
        fields = stat[stat.rindex(")") + 2:].split()
        return int(fields[1]), int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", "replace").strip()[:200]
    except OSError:
        return ""


def _persisted_pids() -> set[int]:
    """Live pids of ``persist_on_release`` jobs from every profile's process checkpoint."""
    from hermes_constants import get_default_hermes_root, get_hermes_home

    homes = {get_hermes_home()}
    try:
        root = get_default_hermes_root()
        homes.add(root)
        homes.update(p for p in (root / "profiles").iterdir() if p.is_dir())
    except OSError:
        pass
    pids: set[int] = set()
    for home in homes:
        try:
            entries = json.loads((home / "processes.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or not entry.get("persist_on_release"):
                continue
            pid, start = entry.get("pid"), entry.get("host_start_time")
            if type(pid) is not int or pid <= 1:
                continue
            stat = _proc_stat(pid)
            if stat is not None and (type(start) is not int or stat[1] == start):
                pids.add(pid)
    return pids


def _is_spared(pid: int, persisted: set[int]) -> bool:
    seen = 0
    while pid > 1 and seen < 64:
        if pid in persisted:
            return True
        stat = _proc_stat(pid)
        if stat is None:
            return False
        pid, seen = stat[0], seen + 1
    return False


def _signal_unspared(pids: list[int]) -> None:
    import signal
    import time

    def send(sig: int, targets: list[int]) -> None:
        for pid in targets:
            try:
                os.kill(pid, sig)
            except OSError:
                pass

    send(signal.SIGTERM, pids)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and any(_proc_stat(p) is not None for p in pids):
        time.sleep(0.1)
    send(getattr(signal, "SIGKILL", signal.SIGTERM), [p for p in pids if _proc_stat(p) is not None])


def stop_ended_run_scopes(
    conn: sqlite3.Connection, *, board: str | None = None,
    grace_seconds: int = RUN_SCOPE_STOP_GRACE_SECONDS, force: bool = False,
) -> list[dict]:
    """Stop the systemd scopes of this board's runs that ended ``grace_seconds`` ago.

    Throttled to one sweep per ``RUN_SCOPE_SWEEP_INTERVAL_SECONDS`` per board unless
    ``force``. A run whose row is not on this board, whose retained worker is still
    alive (``reap_terminal_workers`` owns that), or whose scope holds this process is
    left alone. Returns one record per scope acted on (also logged and booked as a
    ``run_scope_stopped`` event). Never raises.
    """
    import logging
    import time

    log = logging.getLogger(__name__)
    key = board or "default"
    if not force and time.monotonic() - _last_scope_sweep.get(key, float("-inf")) < RUN_SCOPE_SWEEP_INTERVAL_SECONDS:
        return []
    _last_scope_sweep[key] = time.monotonic()
    stopped: list[dict] = []
    try:
        units = _active_run_scopes()
        if not units:
            return []
        from hermes_cli import kanban_db as kb

        cutoff = int(time.time()) - grace_seconds
        persisted: set[int] | None = None
        for unit in units:
            parsed = _parse_scope_unit(unit)
            if parsed is None:
                continue
            task_id, run_id = parsed
            row = conn.execute(
                "SELECT ended_at, worker_pid FROM task_runs WHERE id = ? AND task_id = ?",
                (run_id, task_id)).fetchone()
            if row is None or row[0] is None or int(row[0]) > cutoff:
                continue
            if row[1] is not None and kb._pid_alive(int(row[1])):
                continue
            pids = _scope_pids(unit)
            if not pids or os.getpid() in pids:
                continue
            if persisted is None:
                persisted = _persisted_pids()
            spared = [p for p in pids if _is_spared(p, persisted)]
            doomed = [p for p in pids if p not in spared]
            if not doomed:
                continue
            record = {
                "systemd_unit": unit, "run_id": run_id, "task_id": task_id,
                "mode": "signal" if spared else "stop", "spared": spared,
                "killed": [{"pid": p, "cmd": _cmdline(p)} for p in doomed],
            }
            if spared:
                _signal_unspared(doomed)
            else:
                proc = _systemctl("stop", "--no-block", unit)
                if proc is None or proc.returncode:
                    log.warning("kanban: could not stop scope %s: %s", unit,
                                (proc.stderr.strip() if proc is not None else "no user bus"))
                    continue
            log.info("kanban: stopped scope %s of ended run %s (%s): killed %s, spared %s",
                     unit, run_id, record["mode"],
                     [f"{k['pid']}:{k['cmd'][:60]}" for k in record["killed"]], spared)
            try:
                with kb.write_txn(conn):
                    kb._append_event(conn, task_id, "run_scope_stopped",
                                     {k: v for k, v in record.items() if k != "task_id"}, run_id=run_id)
            except Exception:
                log.debug("kanban: booking run_scope_stopped for %s failed", unit, exc_info=True)
            stopped.append(record)
    except Exception:
        log.warning("kanban: stopping ended run scopes failed", exc_info=True)
    return stopped


def stopped_scope_units(conn: sqlite3.Connection, *, board: str | None = None) -> list[str]:
    """Dispatch-tick entry: :func:`stop_ended_run_scopes`, reduced to the unit names."""
    return [r["systemd_unit"] for r in stop_ended_run_scopes(conn, board=board)]


# --- Typed OOM recovery -------------------------------------------------------
# An ``oom_killed`` run is a resource failure, not a generic crash: the retry
# gets a doubled MemoryMax (bounded by MemAvailable admission) instead of dying
# at the same cap again.
OOM_CAP_CEILING_BYTES = 12 * 1024 ** 3
OOM_ADMISSION_RESERVE_BYTES = 4 * 1024 ** 3   # always left for the host
OOM_ALERT_THRESHOLD = 5                       # kills per 24h before a card opens
OOM_ALERT_WINDOW_SECONDS = 24 * 3600
_MEMORY_MAX_PROP = "MemoryMax="


def mem_available_bytes() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def raised_memory_max(base: int, consecutive_ooms: int, mem_available: int | None) -> int:
    """Cap for the next run: base * 2**n, <= 12 GiB, admitted only if it fits in MemAvailable.

    Unknown MemAvailable refuses the raise (returns base) rather than guessing.
    """
    if consecutive_ooms <= 0 or mem_available is None:
        return base
    want = min(base << min(consecutive_ooms, 8), OOM_CAP_CEILING_BYTES)
    admitted = mem_available - OOM_ADMISSION_RESERVE_BYTES
    if want <= base or admitted <= base:
        return base
    return min(want, admitted)


def consecutive_oom_runs(task_id: str, board: str | None = None) -> int:
    """Number of most-recent finished runs, in a row, that ended ``oom_killed``."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    try:
        with kbc.connect_closing(board=board) as conn:
            rows = conn.execute(
                "SELECT outcome FROM task_runs WHERE task_id = ? AND outcome IS NOT NULL "
                "ORDER BY id DESC LIMIT 8", (task_id,)).fetchall()
    except Exception:
        return 0
    n = 0
    for row in rows:
        if row[0] != "oom_killed":
            break
        n += 1
    return n


def apply_oom_recovery(argv: list[str], task_id: str, board: str | None = None) -> list[str]:
    """Rewrite the scope's MemoryMax property for a kanban worker.

    Every worker starts at 2x the registry base cap (4 GiB -> 8 GiB) when MemAvailable admits it:
    a fixed 4 GiB first-run cap OOM-killed fresh workers before recovery could ever apply
    (3 kills/24h after the typed-recovery deploy, all first runs). Each consecutive OOM-killed
    run then doubles again, bounded by the 12 GiB ceiling and MemAvailable admission.
    """
    n = consecutive_oom_runs(task_id, board)
    for i, arg in enumerate(argv):
        if arg == "--":
            break
        if arg.startswith(_MEMORY_MAX_PROP):
            try:
                base = int(arg[len(_MEMORY_MAX_PROP):])
            except ValueError:
                return argv
            new = raised_memory_max(base, n + 1, mem_available_bytes())
            if new > base:
                argv = list(argv)
                argv[i] = f"{_MEMORY_MAX_PROP}{new}"
            return argv
    return argv


# Heavy JS toolchains get bounded parallelism by default inside workers
# (an explicit repo/user env value still wins).
WORKER_TOOLCHAIN_ENV = {
    "VITEST_MAX_WORKERS": "2",
    "VITEST_MIN_WORKERS": "1",
    "JEST_MAX_WORKERS": "2",
}
_NODE_HEAP_FLAG = "--max-old-space-size=3072"


def apply_toolchain_env(env: dict) -> None:
    for key, value in WORKER_TOOLCHAIN_ENV.items():
        env.setdefault(key, value)
    node_options = env.get("NODE_OPTIONS", "")
    if "max-old-space-size" not in node_options:
        env["NODE_OPTIONS"] = (node_options + " " + _NODE_HEAP_FLAG).strip()


def note_oom_kill(
    task_id: str, board: str | None = None, *, now: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> str | None:
    """Count OOM kills in 24h; above the threshold open one card per incident.

    Later kills in the same incident (an open alert card exists) become comments.
    Returns the alert task id, or None. Never raises.

    Pass ``conn`` when the caller already holds a write transaction on this
    board. A fresh ``connect()`` from inside that transaction deadlocks: init
    re-runs the legacy-inflight backfill, and its ``BEGIN IMMEDIATE`` waits on
    the lock the caller holds for the busy timeout. The alert card's
    ``create_task`` / ``add_comment`` nest as a savepoint on ``conn``.
    """
    import time
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    now = int(now if now is not None else time.time())

    def _note(db: sqlite3.Connection) -> str | None:
        kills = db.execute(
            "SELECT COUNT(*) FROM task_runs WHERE outcome = 'oom_killed' AND ended_at >= ?",
            (now - OOM_ALERT_WINDOW_SECONDS,)).fetchone()[0] + 1  # + this run, not yet booked
        if kills <= OOM_ALERT_THRESHOLD:
            return None
        key = "worker-oom-rate-alert"
        open_card = db.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? AND status NOT IN ('done', 'archived') "
            "ORDER BY created_at DESC LIMIT 1", (key,)).fetchone()
        if open_card:
            kb.add_comment(db, open_card[0], "oom-detector",
                           f"Another worker OOM kill ({task_id}); {kills} in the last 24h.")
            return open_card[0]
        return kb.create_task(
            db, title=f"Worker OOM kills: {kills} in 24h",
            body=(f"{kills} kanban worker scopes were OOM-killed in the last 24h "
                  f"(threshold {OOM_ALERT_THRESHOLD}); latest: {task_id}. Retries get a doubled "
                  "MemoryMax up to 12 GiB; check `journalctl --user | grep oom-kill` and the "
                  "victims' last output for the heavy step."),
            assignee="default", created_by="oom-detector", priority=70,
            idempotency_key=key, board=board)

    try:
        if conn is not None:
            return _note(conn)
        with kbc.connect_closing(board=board) as owned:
            return _note(owned)
    except Exception:
        import logging
        logging.getLogger(__name__).warning("oom detector failed", exc_info=True)
        return None
