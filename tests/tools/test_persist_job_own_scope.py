"""A Kanban worker's persist_on_release job gets its own scope, outside the run scope. [daveey]

The run scope is OOMPolicy=stop / KillMode=control-group and is stopped when the run OOMs or after
it ends, which killed a persisted paid-rental watchdog (hub, 2026-11-03 03:42Z).
"""

import contextlib
import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import tools.process_registry as pr
from tests.waiting import wait_until
from tools.process_registry import ProcessRegistry
from tools.process_registry_persist_scope import KANBAN_SLICE, kanban_run_scope_cgroup

RUN_SCOPE = "user.slice/user-1000.slice/user@1000.service/hermes.slice/hermes-kanban.slice/" \
            "hermes-worker-kanban-t_abc-run-7.scope"


def test_run_scope_cgroup_parses_only_kanban_run_scopes(tmp_path):
    cg = tmp_path / "cgroup"
    cg.write_text(f"0::/{RUN_SCOPE}\n")
    assert kanban_run_scope_cgroup(str(cg)) == Path("/sys/fs/cgroup") / RUN_SCOPE
    cg.write_text("0::/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-gateway.service\n")
    assert kanban_run_scope_cgroup(str(cg)) is None
    assert kanban_run_scope_cgroup(str(tmp_path / "missing")) is None


def _spawn(monkeypatch, tmp_path, *, persist, in_run_scope=True, in_user_unit=False):
    scope_dir = tmp_path / "run-scope"
    scope_dir.mkdir(exist_ok=True)
    (scope_dir / "memory.max").write_text("8589934592\n")
    monkeypatch.setattr(pr, "_IS_LINUX", True)
    monkeypatch.setattr(pr, "_is_supervised_gateway_process", lambda: False)
    monkeypatch.setattr(pr, "_systemd_run_user_scope_available", lambda: True)
    monkeypatch.setattr("tools.process_registry_persist_scope.kanban_run_scope_cgroup",
                        lambda: scope_dir if in_run_scope else None)
    monkeypatch.setattr("tools.process_registry_persist_scope.user_unit_cgroup",
                        lambda: scope_dir if in_user_unit else None, raising=False)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["env"] = kwargs.get("env")
        proc = MagicMock(pid=4321, stdout=iter([]))
        proc.poll.return_value = None
        return proc

    registry = ProcessRegistry()
    with (patch("subprocess.Popen", side_effect=fake_popen), patch("threading.Thread", return_value=MagicMock()),
          patch.object(registry, "_write_checkpoint")):
        session = registry.spawn_local("sleep 60", cwd=str(tmp_path), persist_on_release=persist)
    return session, captured["argv"]


@pytest.mark.platforms("linux")
def test_persisted_job_in_kanban_run_scope_gets_own_scope(monkeypatch, tmp_path):
    session, argv = _spawn(monkeypatch, tmp_path, persist=True)
    assert argv[0] == "/usr/bin/systemd-run" and "--scope" in argv and "--user" in argv
    assert argv[argv.index("--unit") + 1] == f"hermes-worker-{session.id}"
    assert f"--slice={KANBAN_SLICE}" in argv[:argv.index("--")]
    assert "MemoryMax=8589934592" in argv[:argv.index("--")], "the run scope's own cap, not the 4 GiB gateway one"
    assert session.systemd_unit == f"hermes-worker-{session.id}.scope"


@pytest.mark.platforms("linux")
def test_volatile_job_stays_in_run_scope(monkeypatch, tmp_path):
    session, argv = _spawn(monkeypatch, tmp_path, persist=False)
    assert argv[0] != "/usr/bin/systemd-run"
    assert session.systemd_unit == ""


@pytest.mark.platforms("linux")
def test_persisted_job_outside_any_user_unit_is_unchanged(monkeypatch, tmp_path):
    session, argv = _spawn(monkeypatch, tmp_path, persist=True, in_run_scope=False)
    assert argv[0] != "/usr/bin/systemd-run"
    assert session.systemd_unit == ""


SERVICE_CGROUP = "user.slice/user-1000.slice/user@1000.service/app.slice/hermes-always-on-dashboard.service"


def _spawn_from_cgroup(monkeypatch, tmp_path, relative, memory_max=None):
    """Spawn a persisted job from a process whose /proc/self/cgroup says ``relative`` (real parser)."""
    import tools.process_registry_persist_scope as ps
    cg = tmp_path / "self-cgroup"
    cg.write_text(f"0::/{relative}\n")
    root = tmp_path / "cgroupfs"
    if memory_max is not None:
        (root / relative).mkdir(parents=True)
        (root / relative / "memory.max").write_text(f"{memory_max}\n")
    monkeypatch.setattr(ps, "PROC_SELF_CGROUP", str(cg), raising=False)
    monkeypatch.setattr(ps, "CGROUP_ROOT", root, raising=False)
    real_read = Path.read_text

    def read_text(self, *a, **k):  # pre-fix code reads /proc/self/cgroup directly
        return real_read(cg if str(self) == "/proc/self/cgroup" else self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(pr, "_IS_LINUX", True)
    monkeypatch.setattr(pr, "_is_supervised_gateway_process", lambda: False)
    monkeypatch.setattr(pr, "_systemd_run_user_scope_available", lambda: True)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        proc = MagicMock(pid=4321, stdout=iter([]))
        proc.poll.return_value = None
        return proc

    registry = ProcessRegistry()
    with (patch("subprocess.Popen", side_effect=fake_popen), patch("threading.Thread", return_value=MagicMock()),
          patch.object(registry, "_write_checkpoint")):
        session = registry.spawn_local("sleep 60", cwd=str(tmp_path), persist_on_release=True)
    return session, captured["argv"]


@pytest.mark.platforms("linux")
def test_persisted_job_from_any_user_service_gets_own_app_slice_scope(monkeypatch, tmp_path):
    """Hub chats run in hermes-always-on-dashboard.service (KillMode=control-group), restarted by every update."""
    session, argv = _spawn_from_cgroup(monkeypatch, tmp_path, SERVICE_CGROUP, memory_max="max")
    assert argv[0] == "/usr/bin/systemd-run" and "--scope" in argv and "--user" in argv, argv
    head = argv[:argv.index("--")]
    assert argv[argv.index("--unit") + 1] == f"hermes-worker-{session.id}"
    assert "--slice=app.slice" in head and f"--slice={KANBAN_SLICE}" not in head
    assert "MemoryMax=infinity" in head, "an uncapped spawner's job is not given the 4 GiB worker cap"
    assert session.systemd_unit == f"hermes-worker-{session.id}.scope"


@pytest.mark.platforms("linux")
def test_persisted_job_inherits_a_capped_spawner_units_memory_max(monkeypatch, tmp_path):
    _session, argv = _spawn_from_cgroup(monkeypatch, tmp_path, SERVICE_CGROUP, memory_max=str(16 << 30))
    assert f"MemoryMax={16 << 30}" in argv[:argv.index("--")]


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("relative", ["system.slice/hermes.service", "", "user.slice/user-1000.slice/session-3.scope"])
def test_persisted_job_outside_a_user_unit_is_unchanged(monkeypatch, tmp_path, relative):
    session, argv = _spawn_from_cgroup(monkeypatch, tmp_path, relative)
    assert argv[0] != "/usr/bin/systemd-run"
    assert session.systemd_unit == ""


def _user_systemd() -> bool:
    import shutil
    return bool(pr._IS_LINUX and shutil.which("systemd-run") and pr._systemd_run_user_scope_available())


SPAWNER = """
import json, sys, time
sys.path.insert(0, {repo!r})
from tools.process_registry import ProcessRegistry
session = ProcessRegistry().spawn_local({command!r}, cwd={cwd!r}, persist_on_release=True)
open({out!r} + ".tmp", "w").write(json.dumps({{"pid": session.pid, "unit": session.systemd_unit}}))
import os; os.replace({out!r} + ".tmp", {out!r})
time.sleep(600)
"""


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


@pytest.mark.live_system_guard_bypass
@pytest.mark.platforms("linux")
@pytest.mark.skipif(not _user_systemd(), reason="needs a user systemd bus")
@pytest.mark.parametrize("chatty", [False, True], ids=["sleep", "writes-output-after-spawner-exit"])
def test_real_persisted_job_survives_stop_of_its_spawning_user_service(tmp_path, monkeypatch, chatty):
    """A transient user service (KillMode=control-group) spawns a persist job; stopping it spares the job, and a
    job that keeps printing outlives its spawner's stdout pipe (no SIGPIPE on the next write)."""
    import uuid
    monkeypatch.setattr(pr, "_SYSTEMD_SCOPE_AVAILABLE", None)
    monkeypatch.setattr(pr, "_SYSTEMD_SCOPE_PROBED_AT", 0.0)
    import sys
    repo = str(Path(pr.__file__).resolve().parents[1])
    out = tmp_path / "spawned.json"
    ticks = tmp_path / "ticks"
    command = f"while :; do echo tick; echo t >> {ticks}; sleep 0.1; done" if chatty else "sleep 120"
    script = tmp_path / "spawner.py"
    script.write_text(SPAWNER.format(repo=repo, command=command, cwd=str(tmp_path), out=str(out)))
    home = tmp_path / "home"
    home.mkdir()
    unit = f"hermes-test-persist-spawner-{uuid.uuid4().hex[:8]}.service"
    env = pr.systemd_user_bus_env() or dict(os.environ)
    subprocess.run(["systemd-run", "--user", "--quiet", "--collect", "--unit", unit,
                    "--property", "KillMode=control-group", f"--setenv=HERMES_HOME={home}",
                    f"--setenv=PATH={os.environ.get('PATH', '/usr/bin:/bin')}",
                    sys.executable, str(script)], check=True, timeout=15, env=env)
    spawned = {}
    try:
        wait_until(out.exists, timeout=30, what="the spawner's job report")
        spawned = json.loads(out.read_text())
        pid = spawned["pid"]
        wait_until(lambda: _alive(pid), what="the persisted job")
        # systemd-run execs the job only once its scope exists; stopping the spawner earlier tests nothing.
        wait_until(lambda: spawned["unit"] and Path(f"/proc/{pid}/cgroup").read_text().rstrip().endswith(
            "/" + spawned["unit"]), what="the job's own scope")
        # ...and only once the command itself runs: until then the scope holds ``bash -lic``, which ignores
        # SIGTERM, so a stop in that window is lost and waits out TimeoutStopSec (a loaded host widens it).
        if chatty:
            wait_until(ticks.exists, what="the job's first output")
        else:
            wait_until(lambda: "sleep" in _scope_comms(pid), what="the job's command to start")
        # ``stop`` returns once systemd has killed the unit's whole cgroup (KillMode=control-group).
        subprocess.run(["systemctl", "--user", "stop", unit], check=True, timeout=30, env=env)
        assert subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit],
                              env=env, check=False).returncode != 0
        assert _alive(pid), "stopping the spawner's service killed its persist_on_release job"
        if chatty:
            seen = ticks.stat().st_size
            wait_until(lambda: ticks.stat().st_size >= seen + 6, what="the job printing after its spawner exited")
            assert _alive(pid), "the job died writing to its dead spawner's pipe"
        cgroup = Path(f"/proc/{pid}/cgroup").read_text()
        assert spawned["unit"] and cgroup.rstrip().endswith("/app.slice/" + spawned["unit"]), cgroup
    finally:
        subprocess.run(["systemctl", "--user", "stop", unit], check=False, timeout=30, env=env)
        if spawned.get("unit"):
            subprocess.run(["systemctl", "--user", "stop", spawned["unit"]], check=False, timeout=30, env=env)
        if spawned.get("pid") and _alive(spawned["pid"]):
            os.kill(spawned["pid"], 9)


def _scope_comms(pid: int) -> set:
    """Command names of every process in ``pid``'s cgroup."""
    rel = Path(f"/proc/{pid}/cgroup").read_text().strip().split("::", 1)[-1]
    comms = set()
    for member in (Path("/sys/fs/cgroup") / rel.lstrip("/") / "cgroup.procs").read_text().split():
        with contextlib.suppress(OSError):
            comms.add(Path(f"/proc/{member}/comm").read_text().strip())
    return comms


def _in_real_run_scope() -> bool:
    return kanban_run_scope_cgroup() is not None and pr._systemd_run_user_scope_available()


@pytest.mark.live_system_guard_bypass
@pytest.mark.skipif(not _in_real_run_scope(), reason="needs a real Kanban run scope and a user systemd bus")
def test_real_persisted_job_lands_outside_run_scope(tmp_path, monkeypatch):
    """On a Kanban worker host: the job's cgroup is its own scope, so stopping the run scope can't reach it."""
    monkeypatch.setattr(pr, "_SYSTEMD_SCOPE_AVAILABLE", None)  # earlier tests may have cached a fake verdict
    monkeypatch.setattr(pr, "_SYSTEMD_SCOPE_PROBED_AT", 0.0)
    if not pr._systemd_run_user_scope_available():
        pytest.skip("user systemd bus probe failed at run time")
    registry = ProcessRegistry()
    session = registry.spawn_local("sleep 30", cwd=str(tmp_path), persist_on_release=True)
    try:
        deadline = time.monotonic() + 10
        cgroup = ""
        while time.monotonic() < deadline:
            cgroup = Path(f"/proc/{session.pid}/cgroup").read_text()
            if session.systemd_unit in cgroup:
                break
            time.sleep(0.1)
        assert session.systemd_unit and cgroup.rstrip().endswith("/" + session.systemd_unit), cgroup
        assert f"/{KANBAN_SLICE}/" in cgroup
        assert "hermes-worker-kanban-" not in cgroup
        # A stop before the command runs hits ``bash -lic`` (ignores SIGTERM) and waits out TimeoutStopSec.
        wait_until(lambda: "sleep" in _scope_comms(session.pid), what="the job's command to start")
    finally:
        subprocess.run(["systemctl", "--user", "stop", session.systemd_unit], check=False, timeout=15,
                       env=pr.systemd_user_bus_env() or os.environ)
