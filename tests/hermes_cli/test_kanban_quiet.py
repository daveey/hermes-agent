"""``hermes kanban quiet-hold``: real CLI processes, real lease, real observer children.

Only the observer command is a tiny fixture script; the lease, parser registration, subprocess
custody and exit codes are the production code paths. No test sleeps: processes signal readiness
through files/pipes and the observer overruns by blocking on an Event.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agent import claim_quiet as cq
from hermes_cli import kanban_quiet as kq
from tests.waiting import wait_until

REPO = Path(__file__).resolve().parents[2]
WAIT = 60.0
pytestmark = pytest.mark.platforms("linux")


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    yield root
    assert cq._CUSTODY == set() and cq._OWNERS == {}, "lease descriptors left in custody"


def _script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body))
    return str(path)


def _cli(*argv: str, **kw) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "hermes_cli.main", "kanban", *argv], cwd=REPO,
                            env=dict(os.environ), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, **kw)


def _run(*argv: str) -> tuple[int, str, str]:
    proc = _cli(*argv)
    out, err = proc.communicate(timeout=WAIT * 3)
    return proc.returncode, out, err


def _hold_argv(max_s: int, *observer: str, reason: str = "measure") -> list[str]:
    return ["quiet-hold", "--max-s", str(max_s), "--reason", reason, "--", *observer]


def _group_gone(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    return False


# Observer that records what it inherited, then exits with the status in argv[2].
INSPECT = """
    import json, os, sys
    root = os.path.realpath(os.environ["HERMES_HOME"])
    leaked = []
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if "claim-quiet" in target:
            leaked.append(target)
    env = {k: v for k, v in os.environ.items() if k.startswith("HERMES_CLAIM_QUIET_")}
    json.dump({"leaked": leaked, "env": env, "pgid_is_own": os.getpgid(0) == os.getpid(),
               "argv": sys.argv[2:]}, open(sys.argv[1], "w"))
    sys.exit(int(sys.argv[2]))
"""

# Observer that parks (grandchild too) until killed; records both pids.
OVERRUN = """
    import os, subprocess, sys, threading
    grand = subprocess.Popen([sys.executable, "-c", "import threading; threading.Event().wait(600)"])
    open(sys.argv[1] + ".tmp", "w").write(f"{os.getpid()} {grand.pid}")
    os.rename(sys.argv[1] + ".tmp", sys.argv[1])
    if len(sys.argv) > 2 and sys.argv[2] == "ignore-term":
        import signal
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    threading.Event().wait(600)
"""


def _pids(path: Path) -> tuple[int, int]:
    wait_until(path.exists, timeout=WAIT, what="observer pid file")
    leader, grand = path.read_text().split()
    return int(leader), int(grand)


def test_cli_end_to_end_passes_identity_and_keeps_the_lease_descriptors_out_of_the_observer(home, tmp_path):
    report = tmp_path / "report.json"
    observer = _script(tmp_path, "observer.py", INSPECT)
    code, _out, err = _run(*_hold_argv(30, sys.executable, observer, str(report), "0", "--", "x y"))
    assert code == 0, err
    seen = json.loads(report.read_text())
    assert seen["leaked"] == [], "the observer inherited a lease descriptor"
    assert seen["pgid_is_own"], "the observer must lead its own process group"
    assert seen["argv"] == ["0", "--", "x y"], "argv must be passed verbatim, without a shell"
    env = seen["env"]
    assert set(env) == {kq.ENV_EPISODE, kq.ENV_PRODUCER_PID, kq.ENV_EXPIRES_AT, kq.ENV_MAX_S}
    assert env[kq.ENV_MAX_S] == "30" and len(env[kq.ENV_EPISODE]) == 32
    assert cq.read_active() is None, "admission must be open after the command returns"
    assert (home / "kanban" / "claim-quiet" / "last-started.json").exists()
    assert not list(home.rglob("*.db")) and not (home / "config.yaml").exists(), "no board init / config write"


def test_cli_registration_and_usage_errors(home):
    assert _run("quiet-hold", "--max-s", "5", "--reason", "r", "true")[0] == kq.EXIT_USAGE
    assert _run("quiet-hold", "--max-s", "5", "--reason", "r", "--")[0] == kq.EXIT_USAGE
    assert _run("quiet-hold", "--max-s", "481", "--reason", "r", "--", "true")[0] == kq.EXIT_USAGE
    assert _run("quiet-hold", "--max-s", "0", "--reason", "r", "--", "true")[0] == kq.EXIT_USAGE
    assert _run("quiet-hold", "--max-s", "5", "--reason", "  ", "--", "true")[0] == kq.EXIT_USAGE
    assert _run("quiet-hold", "--reason", "r", "--", "true")[0] == 2  # argparse: --max-s required
    assert not (home / "kanban" / "claim-quiet").exists(), "usage errors must not touch the filesystem"


def test_observer_failure_releases_the_lease_and_reports_observer_failed(home, tmp_path):
    report = tmp_path / "report.json"
    observer = _script(tmp_path, "observer.py", INSPECT)
    code, _out, err = _run(*_hold_argv(30, sys.executable, observer, str(report), "7"))
    assert code == kq.EXIT_OBSERVER_FAILED and "status 7" in err
    assert cq.read_active() is None and cq.inspect().state == "inactive"


def test_spawn_failure_is_observer_failed_closes_the_owner_and_consumes_the_cooldown(home):
    code = kq.hold(30, "r", [str(home / "no-such-binary")])
    assert code == kq.EXIT_OBSERVER_FAILED
    assert cq.read_active() is None
    with pytest.raises(cq.QuietCooldownError):
        cq.acquire(max_s=30, reason="again")


def test_cooldown_refuses_a_second_hold_and_never_starts_the_observer(home, tmp_path):
    assert _run(*_hold_argv(30, "true"))[0] == 0
    marker = tmp_path / "ran"
    observer = _script(tmp_path, "mark.py", "import sys; open(sys.argv[1], 'w').write('x')")
    code, _out, err = _run(*_hold_argv(30, sys.executable, observer, str(marker)))
    assert code == kq.EXIT_COOLDOWN and "cooldown" in err
    assert not marker.exists()


@pytest.mark.live_system_guard_bypass  # we SIGKILL our own orphaned observer group
def test_busy_when_another_process_holds_the_lease(home, tmp_path):
    pidfile = tmp_path / "pids"
    observer = _script(tmp_path, "overrun.py", OVERRUN)
    holder = _cli(*_hold_argv(60, sys.executable, observer, str(pidfile)))
    leader, grand = _pids(pidfile)
    try:
        wait_until(lambda: cq.read_active() is not None, timeout=WAIT, what="lease published")
        code, _out, err = _run(*_hold_argv(30, "true"))
        assert code == kq.EXIT_BUSY, err
    finally:
        _reap_holder(holder, leader, grand)


def _reap_holder(holder: subprocess.Popen, leader: int, grand: int) -> None:
    """SIGKILL the producer (a crash), then the orphaned observer group that still holds its pipes."""
    holder.send_signal(signal.SIGKILL)
    holder.wait(timeout=WAIT)
    for pid in (leader, grand):
        os.kill(pid, signal.SIGKILL)
    wait_until(lambda: _group_gone(leader), timeout=WAIT, what="observer group gone")
    holder.communicate(timeout=WAIT)


@pytest.mark.live_system_guard_bypass  # we SIGKILL our own orphaned observer group
def test_crash_of_the_producer_opens_admission_even_though_the_observer_lives(home, tmp_path):
    pidfile = tmp_path / "pids"
    observer = _script(tmp_path, "overrun.py", OVERRUN)
    holder = _cli(*_hold_argv(60, sys.executable, observer, str(pidfile)))
    leader, grand = _pids(pidfile)
    try:
        wait_until(lambda: cq.read_active() is not None, timeout=WAIT, what="lease published")
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=WAIT)
        wait_until(lambda: cq.read_active() is None, timeout=WAIT, what="admission open after crash")
        assert cq.inspect().state == "inactive"
        assert not _group_gone(leader), "fixture: the observer outlives its producer"
    finally:
        _reap_holder(holder, leader, grand)


def test_deadline_kills_an_overrunning_observer_group_and_opens_admission_first(home, tmp_path):
    pidfile = tmp_path / "pids"
    observer = _script(tmp_path, "overrun.py", OVERRUN)
    proc = _cli(*_hold_argv(1, sys.executable, observer, str(pidfile)))
    leader, grand = _pids(pidfile)
    _out, err = proc.communicate(timeout=WAIT)
    assert proc.returncode == kq.EXIT_DEADLINE, err
    assert _group_gone(leader), "observer group (including the grandchild) must be dead"
    for pid in (leader, grand):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert cq.read_active() is None


def test_a_term_ignoring_observer_is_killed_after_the_bounded_grace(home, tmp_path, monkeypatch):
    monkeypatch.setattr(kq, "TERM_GRACE_S", 0.3)
    pidfile = tmp_path / "pids"
    observer = _script(tmp_path, "overrun.py", OVERRUN)
    argv = [sys.executable, observer, str(pidfile), "ignore-term"]
    # drive hold() in-process with a 1s lease; the observer ignores TERM and must be KILLed
    code = kq.hold(1, "r", argv)
    leader, _grand = _pids(pidfile)
    assert code == kq.EXIT_DEADLINE
    assert _group_gone(leader)
    assert cq.read_active() is None


def test_lease_is_released_before_the_observer_is_signalled(home, tmp_path, monkeypatch):
    pidfile = tmp_path / "pids"
    observer = _script(tmp_path, "overrun.py", OVERRUN)
    states: list[str] = []
    real = kq._terminate_group

    def probe(proc):
        states.append(cq.inspect().state)  # admission must already be open when TERM is sent
        return real(proc)

    monkeypatch.setattr(kq, "_terminate_group", probe)
    assert kq.hold(1, "r", [sys.executable, observer, str(pidfile)]) == kq.EXIT_DEADLINE
    assert states == ["inactive"]


def test_signal_cancels_the_hold_terminates_the_observer_and_releases(home, tmp_path):
    pidfile = tmp_path / "pids"
    observer = _script(tmp_path, "overrun.py", OVERRUN)
    proc = _cli(*_hold_argv(120, sys.executable, observer, str(pidfile)))
    leader, _grand = _pids(pidfile)
    wait_until(lambda: cq.read_active() is not None, timeout=WAIT, what="lease published")
    proc.send_signal(signal.SIGTERM)
    _out, err = proc.communicate(timeout=WAIT)
    assert proc.returncode == kq.EXIT_CANCELLED, err
    assert _group_gone(leader) and cq.read_active() is None


def test_unresolved_group_death_is_never_reported_as_success(home, tmp_path, monkeypatch):
    def cannot_verify(proc):  # really reap the group (no leak), but report that death was unproven
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        return False

    monkeypatch.setattr(kq, "_terminate_group", cannot_verify)
    pidfile = tmp_path / "pids"
    observer = _script(tmp_path, "overrun.py", OVERRUN)
    assert kq.hold(1, "r", [sys.executable, observer, str(pidfile)]) == kq.EXIT_UNRESOLVED
    assert cq.read_active() is None, "cleanup failure must not extend the lease"


def test_release_failure_is_reported_and_still_attempts_to_terminate(home, tmp_path, monkeypatch):
    real = cq.QuietOwner.close

    def failing(self):
        real(self)  # really release, then report a (late) failure
        raise OSError("close failed")

    monkeypatch.setattr(cq.QuietOwner, "close", failing)
    assert kq.hold(30, "r", ["true"]) == kq.EXIT_RELEASE_FAILED
    assert cq.read_active() is None


def _child_script(tmp_path: Path) -> str:
    return _script(tmp_path, "acquirer.py", """
        import sys
        from agent import claim_quiet as cq
        print("ready", flush=True)
        sys.stdin.readline()
        try:
            owner = cq.acquire(max_s=60, reason="race")
        except cq.QuietLeaseError as exc:
            print(type(exc).__name__, flush=True)
            sys.exit(0)
        print("ok", flush=True)
        sys.stdin.read()  # hold until the parent closes stdin
        owner.close()
    """)


def test_concurrent_acquire_from_real_processes_has_exactly_one_winner(home, tmp_path):
    script = _child_script(tmp_path)
    children = [subprocess.Popen([sys.executable, script], cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO)},
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
                for _ in range(6)]
    try:
        for child in children:
            assert child.stdout.readline().strip() == "ready"
        for child in children:  # release them all at once
            child.stdin.write("go\n")
            child.stdin.flush()
        results = [child.stdout.readline().strip() for child in children]
        assert results.count("ok") == 1, results
        assert set(results) - {"ok"} <= {"QuietBusyError", "QuietCooldownError"}
        winner = children[results.index("ok")]
        assert cq.read_active() is not None
        winner.send_signal(signal.SIGKILL)  # crash: admission opens without any cleanup running
        winner.wait(timeout=WAIT)
        wait_until(lambda: cq.read_active() is None, timeout=WAIT, what="admission open after crash")
    finally:
        for child in children:
            child.stdin.close()
            child.wait(timeout=WAIT)
            child.stdout.close()
