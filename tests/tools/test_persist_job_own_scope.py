"""A Kanban worker's persist_on_release job gets its own scope, outside the run scope. [daveey]

The run scope is OOMPolicy=stop / KillMode=control-group and is stopped when the run OOMs or after
it ends, which killed a persisted paid-rental watchdog (hub, 2026-11-03 03:42Z).
"""

import os
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import tools.process_registry as pr
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


def _spawn(monkeypatch, tmp_path, *, persist, in_run_scope=True):
    scope_dir = tmp_path / "run-scope"
    scope_dir.mkdir(exist_ok=True)
    (scope_dir / "memory.max").write_text("8589934592\n")
    monkeypatch.setattr(pr, "_IS_LINUX", True)
    monkeypatch.setattr(pr, "_is_supervised_gateway_process", lambda: False)
    monkeypatch.setattr(pr, "_systemd_run_user_scope_available", lambda: True)
    monkeypatch.setattr("tools.process_registry_persist_scope.kanban_run_scope_cgroup",
                        lambda: scope_dir if in_run_scope else None)
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
def test_persisted_job_outside_kanban_run_scope_is_unchanged(monkeypatch, tmp_path):
    session, argv = _spawn(monkeypatch, tmp_path, persist=True, in_run_scope=False)
    assert argv[0] != "/usr/bin/systemd-run"
    assert session.systemd_unit == ""


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
    finally:
        subprocess.run(["systemctl", "--user", "stop", session.systemd_unit], check=False, timeout=15,
                       env=pr.systemd_user_bus_env() or os.environ)
