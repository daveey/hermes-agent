"""A Kanban run scope survives an OOM-killed command (OOMPolicy=continue, systemd >= 253)."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_worker_scope as scope

SCOPE = ["systemd-run", "--user", "--scope", "--unit", "hermes-worker-kanban-t_x-run-1",
         "--property", "MemoryMax=1073741824", "--", "hermes", "chat", "--", "-q"]
UNIT = "hermes-worker-kanban-t_x-run-1.scope"


@pytest.fixture
def probe(monkeypatch):
    """Fake systemd-run probe: ``probe(rc, stderr)`` sets its answer; ``calls`` records argv."""
    from tools import process_registry as pr
    monkeypatch.setattr(scope, "_oom_continue_supported", None)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(pr, "systemd_user_bus_env", lambda *a: {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/x"})
    state = SimpleNamespace(rc=0, stderr=b"", calls=[])

    def run(argv, **kwargs):
        state.calls.append(argv)
        return subprocess.CompletedProcess(argv, state.rc, b"", state.stderr)

    monkeypatch.setattr(subprocess, "run", run)
    return state


def test_argv_carries_oom_policy_continue_when_probe_accepts_it(probe):
    argv = scope.oom_policy_continue(SCOPE)
    boundary = argv.index("--")
    assert argv[:boundary] == [*SCOPE[:SCOPE.index("--")], "--property", "OOMPolicy=continue"]
    assert argv[boundary:] == SCOPE[SCOPE.index("--"):]  # worker args untouched, incl. their own "--"
    assert "OOMPolicy=continue" in probe.calls[0][:probe.calls[0].index("--")]
    scope.oom_policy_continue(SCOPE)
    assert len(probe.calls) == 1  # probed once
    env = {}
    scope.apply_toolchain_env(env, argv)
    assert env[scope.OOM_POLICY_ENV] == "continue"


def test_old_systemd_keeps_todays_argv_and_logs_once(probe, caplog):
    probe.rc, probe.stderr = 1, b"Unknown assignment: OOMPolicy=continue"
    assert scope.oom_policy_continue(SCOPE) == SCOPE
    assert scope.oom_policy_continue(SCOPE) == SCOPE
    assert len(probe.calls) == 1
    assert sum("rejects OOMPolicy=continue" in r.getMessage() for r in caplog.records) == 1
    env = {}
    scope.apply_toolchain_env(env, SCOPE)
    assert scope.OOM_POLICY_ENV not in env


@pytest.mark.parametrize("version,rc,stderr,emitted", [
    # systemd 239/245/249 reject OOMPolicy on transient scopes (#102486, commit 611ee856c53).
    (239, 1, b"Unknown assignment: OOMPolicy=continue", False),
    (245, 1, b"Unknown assignment: OOMPolicy=continue", False),
    (249, 1, b"Failed to start transient scope unit: Cannot set property OOMPolicy, or unknown property.", False),
    (253, 0, b"", True),
    (255, 0, b"", True),  # hub + workers (255.4): accepted, rc 0
])
def test_oom_policy_emitted_exactly_when_systemd_accepts_it(probe, version, rc, stderr, emitted):
    probe.rc, probe.stderr = rc, stderr
    argv = scope.oom_policy_continue(SCOPE)
    assert ("OOMPolicy=continue" in argv[:argv.index("--")]) is emitted, version
    assert scope._oom_continue_supported is emitted  # a verdict either way: probed once


def test_generic_scope_probe_and_argv_stay_free_of_oom_policy(probe):
    """Only Kanban run scopes carry it; the shared availability probe never does (#102486)."""
    from tools import process_registry as pr
    scope.oom_policy_continue(SCOPE)
    assert not any("OOMPolicy" in a for a in pr._systemd_scope_argv("systemd-run", "u", "true"))


def _live_scope_bus() -> bool:
    import shutil
    from tools import process_registry as pr
    return bool(shutil.which("systemd-run")) and "DBUS_SESSION_BUS_ADDRESS" in pr.systemd_user_bus_env()


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("policy", [False, True])
def test_live_child_oom_kills_scope_unless_continue(policy):
    """Before/after on a real user scope: stop takes the parent shell (143), continue keeps it (137 child)."""
    import uuid
    from tools import process_registry as pr
    if not _live_scope_bus():
        pytest.skip("no systemd-run user bus")
    argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--unit",
            f"hermes-test-oom-{uuid.uuid4().hex[:8]}", "-p", "MemoryMax=64M", "-p", "MemorySwapMax=0", "--",
            "bash", "-c", "python3 -c 'a=bytearray(300*2**20)'; rc=$?; sleep 3; echo survived rc=$rc"]
    if policy:
        argv = scope.oom_policy_continue(argv)
        if "OOMPolicy=continue" not in argv:
            pytest.skip("systemd < 253 rejects OOMPolicy=continue on scopes")
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=pr.systemd_user_bus_env(), check=False)
    if policy:
        assert proc.returncode == 0 and "survived rc=137" in proc.stdout, (proc.stdout, proc.stderr)
    else:
        assert proc.returncode in (143, -15) and "survived" not in proc.stdout, (proc.returncode, proc.stdout)


def test_unrelated_probe_failure_is_not_cached(probe):
    probe.rc, probe.stderr = 1, b"Failed to connect to bus"
    assert scope.oom_policy_continue(SCOPE) == SCOPE
    probe.rc, probe.stderr = 0, b""
    assert "OOMPolicy=continue" in scope.oom_policy_continue(SCOPE)


def test_non_scope_argv_is_never_probed(probe):
    assert scope.oom_policy_continue(["hermes", "chat"]) == ["hermes", "chat"]
    assert probe.calls == []


def test_dispatch_argv_gets_the_policy(probe, monkeypatch):
    from hermes_cli import kanban_aggregate_scope
    from hermes_cli import kanban_db_dispatch as kbd
    from tools import process_registry as pr
    monkeypatch.setattr(kanban_aggregate_scope, "configure_pool", lambda: None)
    monkeypatch.setattr(scope, "consecutive_oom_runs", lambda *a, **kw: 0)
    monkeypatch.setattr(pr, "restart_safe_gateway_child_argv",
                        lambda command, **kw: SimpleNamespace(argv=["systemd-run", "--user", "--scope", "--collect",
                                                                    "--", *command], mode="scoped"))
    argv = kbd._restart_safe_worker_argv(SimpleNamespace(id="t_x", current_run_id=1), ["hermes", "chat"])
    assert "OOMPolicy=continue" in argv[:argv.index("--")]


KERNEL = f"""\
python3 invoked oom-killer: gfp_mask=0xcc0(GFP_KERNEL), order=0, oom_score_adj=200
memory: usage 8388608kB, limit 8388608kB, failcnt 1234
oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=/,mems_allowed=0,oom_memcg=/user.slice/hermes.slice/hermes-kanban.slice/{UNIT},task_memcg=/user.slice/hermes.slice/hermes-kanban.slice/{UNIT},task=python3,pid=1870933,uid=1000
Memory cgroup out of memory: Killed process 1870933 (python3) total-vm:8000000kB, anon-rss:7692760kB, file-rss:9100kB, shmem-rss:0kB, UID:1000 pgtables:296kB oom_score_adj:200
oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=/,mems_allowed=0,oom_memcg=/x/other.scope,task_memcg=/x/other.scope,task=node,pid=55,uid=1000
Memory cgroup out of memory: Killed process 55 (node) total-vm:1kB, anon-rss:99kB, file-rss:0kB
"""


def _kernel(monkeypatch, text=KERNEL, props=""):
    def run(argv, **kwargs):
        if argv[0] == "journalctl":
            return subprocess.CompletedProcess(argv, 0, text, "")
        return subprocess.CompletedProcess(argv, 0, props, "")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("tools.process_registry.systemd_user_bus_env", dict)


def test_kernel_victim_is_parsed_for_this_unit_only(monkeypatch):
    _kernel(monkeypatch)
    victim = scope.kernel_oom_victim(UNIT)
    assert victim == {"pid": 1870933, "task": "python3", "anon_rss_bytes": 7692760 * 1024,
                      "limit_bytes": 8388608 * 1024}
    assert scope.kernel_oom_victim(UNIT, pid=42) is None


def test_describe_never_prints_a_peak_below_the_limit_for_an_oom_kill():
    cap = 8 * 1024 ** 3
    low = {"scope_result": "oom-kill", "memory_max_bytes": cap, "memory_peak_bytes": int(373.559 * 1024 ** 2)}
    text = scope.describe(low)
    assert "OOM-killed at 8192 MiB (peak unknown)" in text and "373" not in text
    victim = {"pid": 1870933, "task": "python3", "anon_rss_bytes": 7692760 * 1024}
    text = scope.describe({**low, "oom_victim": {**victim, "is_worker": False}})
    assert "kernel victim: python3 pid 1870933, anon-rss 7512.46 MiB" in text and "373" not in text
    assert "the worker itself" in scope.describe({**low, "oom_victim": {**victim, "is_worker": True}})
    assert "(peak 8192 MiB)" in scope.describe({**low, "memory_peak_bytes": cap})


def test_stop_policy_oom_names_the_kernel_victim(monkeypatch):
    _kernel(monkeypatch, props="LoadState=loaded\nResult=oom-kill\nMemoryMax=8589934592\nMemoryPeak=391705600\n")
    failure = scope.read_failure("t_x", 1, "", pid=999)
    assert failure["scope_result"] == "oom-kill"
    assert failure["oom_victim"]["pid"] == 1870933 and not failure["oom_victim"]["is_worker"]
    assert "kernel victim: python3 pid 1870933" in scope.describe(failure)


def _snapshot(**extra):
    return scope.SNAPSHOT_PREFIX + json.dumps({"systemd_unit": UNIT, "oom_kill": 1,
                                               "memory_max_bytes": 1 << 30, "memory_peak_bytes": 1 << 30, **extra})


def test_continue_policy_child_oom_does_not_fail_the_run(monkeypatch):
    """The worker outlived a command's OOM kill: its exit is not an OOM failure."""
    _kernel(monkeypatch)  # the kernel victim is a child, not the worker pid
    assert scope.read_failure("t_x", 1, _snapshot(oom_policy="continue"), pid=999) is None
    # Without the policy marker the snapshot still means systemd stopped the scope for OOM.
    assert scope.read_failure("t_x", 1, _snapshot(), pid=999)["scope_result"] == "oom-kill"


def test_continue_policy_worker_itself_oom_killed_still_fails(monkeypatch):
    """When the agent is the largest process, the kernel kills it: the run fails as before."""
    _kernel(monkeypatch)
    monkeypatch.setattr(scope, "_oom_continue_supported", True)
    failure = scope.read_failure("t_x", 1, "", pid=1870933)
    assert failure["scope_result"] == "oom-kill"
    assert failure["memory_max_bytes"] == 8388608 * 1024  # from the kernel record; the scope is gone
    assert "kernel victim: the worker itself, python3 pid 1870933" in scope.describe(failure)


def _journal_argv(monkeypatch, text=KERNEL, props=""):
    """Like ``_kernel``, and returns the list of journalctl argvs it was asked to run."""
    seen = []

    def run(argv, **kwargs):
        if argv[0] == "journalctl":
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, text, "")
        return subprocess.CompletedProcess(argv, 0, props, "")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("tools.process_registry.systemd_user_bus_env", dict)
    return seen


def test_fresh_dispatcher_still_finds_the_agent_as_oom_victim(monkeypatch):
    """Restarted after the spawn: no probe verdict in this process, no snapshot (SIGKILL), scope GC'd."""
    monkeypatch.setattr(scope, "_oom_continue_supported", None)
    seen = _journal_argv(monkeypatch, props="LoadState=not-found\n")
    failure = scope.read_failure("t_x", 1, "", pid=1870933)
    assert failure is not None and failure["scope_result"] == "oom-kill"
    assert failure["oom_victim"]["is_worker"]
    assert all(argv[0] == "journalctl" for argv in seen)  # no systemd-run probe was needed
    assert scope._oom_continue_supported is None
    # A child victim is still no failure of the run.
    assert scope.read_failure("t_x", 1, "", pid=999) is None


def test_kernel_log_read_is_bounded_by_the_run_start(monkeypatch):
    """It runs under the reclaim write txn: read from the run's start, not a day back."""
    seen = _journal_argv(monkeypatch, props="LoadState=loaded\nResult=oom-kill\nMemoryMax=8589934592\n")
    scope.read_failure("t_x", 1, "", pid=999, started_at=1794550650)
    scope.read_failure("t_x", 1, "", pid=1870933, started_at=1794550650.9)
    assert [argv[argv.index("-S") + 1] for argv in seen] == ["@1794550650", "@1794550650"]
    seen.clear()
    scope.read_failure("t_x", 1, "", pid=999)  # unknown start: the old day-long window
    assert seen[0][seen[0].index("-S") + 1] == "-24h"


def _worker5_journal():
    from pathlib import Path
    return (Path(__file__).parent / "fixtures/oom-agent-victim.txt").read_text().split("### worker-7")[0]


def test_bound_interpreter_victim_differs_from_launcher(monkeypatch):
    _journal_argv(monkeypatch, text=_worker5_journal())
    failure = scope.read_failure("t_01a1da7f22dada56", 16314, pid=2111911, worker_pids={2112259})
    assert failure is not None
    assert "Worker was OOM-killed at 8192 MiB" in scope.describe(failure)
    assert failure["oom_victim"]["is_worker"]


def test_command_victim_outside_run_process_set_is_not_failure(monkeypatch):
    _journal_argv(monkeypatch, text=_worker5_journal())
    assert scope.read_failure("t_01a1da7f22dada56", 16314, pid=2111911, worker_pids={42, 43}) is None


def test_dispatch_books_bound_victim_and_uplifts_retry(tmp_path, monkeypatch):
    from pathlib import Path
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as kbd
    from tests.hermes_cli.test_kanban_worker_exit_trailer import _dead_worker_with_log
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(scope, "mem_available_bytes", lambda: 40 * 1024 ** 3)
    kbd._recent_worker_exits.clear()
    kb.init_db()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="bound interpreter OOM", assignee="a")
        _dead_worker_with_log(conn, tid, 2111911, 1)
        rid = kb.get_task(conn, tid).current_run_id
        for bound in (2112259, 2112260, 2112261):
            conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
                         (tid, rid, "worker_bound", json.dumps({"pid": bound}), 1))
        conn.commit()
        journal = _worker5_journal().replace("t_01a1da7f22dada56-run-16314", f"{tid}-run-{rid}")
        _journal_argv(monkeypatch, text=journal)
        monkeypatch.setattr(kbd, "crash_worker_output", lambda *a, **kw: "managed worker lost: EOFError")
        kbd.detect_crashed_workers(conn)
        run = conn.execute("SELECT * FROM task_runs WHERE id=?", (rid,)).fetchone()
        assert run["outcome"] == "oom_killed"
        assert "OOM-killed at 8192 MiB" in kb.get_task(conn, tid).last_failure_error
    # A small base avoids the independent 12-GiB ceiling; OOM doubles the normal first-run uplift.
    argv = ["systemd-run", "--scope", "--property", f"MemoryMax={2 * 1024 ** 3}", "--", "hermes"]
    assert f"MemoryMax={8 * 1024 ** 3}" in scope.apply_oom_recovery(argv, tid)


def test_run_worker_pids_is_run_scoped_and_tolerates_bad_events():
    import sqlite3
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE task_events (task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT)")
        events = [("t_x", 1, "worker_bound", json.dumps({"pid": p})) for p in (2, 3, 4, 0, -1, True, "5")]
        events += [("t_other", 1, "worker_bound", '{"pid":6}'), ("t_x", 2, "worker_bound", '{"pid":7}'),
                   ("t_x", 1, "spawned", '{"pid":8}')]
        events += [("t_x", 1, "worker_bound", raw) for raw in (None, "{", "[]", "null", "{}")]
        conn.executemany("INSERT INTO task_events VALUES (?,?,?,?)", events)
        assert scope.run_worker_pids(conn, "t_x", 1, 9) == {2, 3, 4, 9}
        assert scope.run_worker_pids(conn, "t_x", None, 9) == {9}
    assert scope.run_worker_pids(None, "t_x", 1, 9) == {9}


def test_classify_dead_worker_passes_the_run_start(monkeypatch):
    import sqlite3
    from hermes_cli import kanban_db_dispatch as kbd
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, started_at INTEGER)")
    conn.execute("INSERT INTO task_runs VALUES (5, 1794550650)")
    assert scope.run_started_at(conn, 5) == 1794550650
    assert scope.run_started_at(conn, 6) is None and scope.run_started_at(None, 5) is None
    seen = {}
    monkeypatch.setattr(kbd._kb, "read_worker_log", lambda *a, **kw: "")
    monkeypatch.setattr(kbd._worker_scope, "read_failure",
                        lambda *a, **kw: seen.update(kw) or {"systemd_unit": UNIT, "scope_result": "signal",
                                                             "memory_max_bytes": None, "memory_peak_bytes": None})
    monkeypatch.setattr(kbd, "crash_worker_output", lambda *a, **kw: "")
    kbd._classify_dead_worker(4242, "h:1", None, task_id="t_x", run_id=5, conn=conn)
    assert seen == {"pid": 4242, "worker_pids": {4242}, "started_at": 1794550650, "exit_code": None}


def test_failure_path_reads_the_worker_log_once(monkeypatch):
    """No authority rc and a reap-registry miss: the exit trailer comes from the log already read."""
    from hermes_cli import kanban_db_dispatch as kbd
    reads = []
    monkeypatch.setattr(kbd._kb, "read_worker_log", lambda *a, **kw: reads.append(kw) or "no trailer\n")
    monkeypatch.setattr(kbd, "_recent_worker_exits", {})
    monkeypatch.setattr(kbd._worker_scope, "read_failure",
                        lambda *a, **kw: {"systemd_unit": UNIT, "scope_result": "oom-kill",
                                          "memory_max_bytes": None, "memory_peak_bytes": None})
    monkeypatch.setattr(kbd._worker_scope, "note_oom_kill", lambda *a, **kw: None)
    monkeypatch.setattr(kbd, "crash_worker_output", lambda *a, **kw: "")
    assert kbd._classify_dead_worker(4242, "h:1", task_id="t_x", run_id=5).kind == "oom_killed"
    assert len(reads) == 1
    assert kbd._worker_log_exit_code("t_x", run_id=5, log="x\n" + kbd.KANBAN_WORKER_EXIT_TRAILER + "3\n") == 3
    assert len(reads) == 1  # a caller-held log is never re-read


def test_clean_exit_never_reads_the_kernel_log(monkeypatch):
    """rc 0: the worker outlived any kill, so no journalctl runs inside the reclaim write txn."""
    seen = _journal_argv(monkeypatch, props="LoadState=loaded\nResult=success\n")
    assert scope.read_failure("t_x", 1, "", pid=1870933, exit_code=0) is None
    assert seen == []
    assert scope.read_failure("t_x", 1, "", pid=1870933, exit_code=1)["scope_result"] == "oom-kill"
    assert len(seen) == 1  # a non-zero exit still asks the kernel whether the worker was its victim


def test_classify_dead_worker_passes_the_known_exit_code(monkeypatch):
    from hermes_cli import kanban_db_dispatch as kbd
    seen = _journal_argv(monkeypatch, props="LoadState=not-found\n")
    monkeypatch.setattr(kbd._kb, "read_worker_log", lambda *a, **kw: "")
    monkeypatch.setattr(kbd, "crash_worker_output", lambda *a, **kw: "")
    dead = kbd._classify_dead_worker(1870933, "h:1", 0, task_id="t_x", run_id=1)  # the authority's rc
    assert dead.kind == "clean_exit" and seen == []
    monkeypatch.setattr(kbd, "_recent_worker_exits", {1870933: (0, 0.0)})  # reaped here: wait status 0
    assert kbd._classify_dead_worker(1870933, "h:1", task_id="t_x", run_id=1).kind == "clean_exit" and seen == []
    monkeypatch.setattr(kbd, "_recent_worker_exits", {})
    monkeypatch.setattr(kbd, "_worker_log_exit_code", lambda *a, **kw: 0)  # the log's exit trailer
    assert kbd._classify_dead_worker(1870933, "h:1", task_id="t_x", run_id=1).kind == "clean_exit" and seen == []
    monkeypatch.setattr(kbd, "_recent_worker_exits", {1870933: (9, 0.0)})  # SIGKILL: ask the kernel
    kbd._classify_dead_worker(1870933, "h:1", task_id="t_x", run_id=1)
    assert len(seen) == 1


def test_compute_policy_nice_wrapper_keeps_the_oom_policy_env(probe, monkeypatch):
    """bounded_scope's own ``-- nice -n 15`` comes after the policy; the worker env still learns it."""
    from hermes_cli import kanban_aggregate_scope
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import worker_compute_capacity as wcc
    from tools import process_registry as pr
    monkeypatch.setattr(kanban_aggregate_scope, "configure_pool", lambda: None)
    monkeypatch.setattr(wcc, "worker_enabled", lambda: True)
    monkeypatch.setattr(wcc, "configure_card", lambda board, task_id: "hermes-kanban-" + "0" * 64 + ".slice")
    monkeypatch.setattr(scope, "consecutive_oom_runs", lambda *a, **kw: 0)
    monkeypatch.setattr(pr, "restart_safe_gateway_child_argv",
                        lambda command, **kw: SimpleNamespace(argv=["systemd-run", "--user", "--scope", "--collect",
                                                                    "--", *command], mode="scoped"))
    argv = kbd._restart_safe_worker_argv(SimpleNamespace(id="t_x", current_run_id=1), ["hermes", "chat"])
    boundary = argv.index("--")
    assert "OOMPolicy=continue" in argv[:boundary]
    assert argv[boundary:boundary + 4] == ["--", "nice", "-n", "15"]
    env = {}
    scope.apply_toolchain_env(env, argv)
    assert env[scope.OOM_POLICY_ENV] == "continue"
