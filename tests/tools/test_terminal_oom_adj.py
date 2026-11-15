"""Terminal commands, not the agent, are preferred victims inside run scopes."""
import shutil
import sys
from pathlib import Path

import pytest

from tools import terminal_oom
from tools.environments.local import LocalEnvironment

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux OOM adjustment")


@pytest.mark.parametrize("inside", [True, False])
@pytest.mark.parametrize("long_lived", [True, False])
def test_command_adjustment(tmp_path, monkeypatch, inside, long_lived):
    if not shutil.which("choom"):
        pytest.skip("choom unavailable")
    monkeypatch.setattr(terminal_oom, "run_scope_cgroup", lambda: tmp_path if inside else None)
    parent = Path("/proc/self/oom_score_adj").read_text().strip()
    env = LocalEnvironment(cwd=str(tmp_path))
    if long_lived:
        proc = env._spawn_long_lived(["cat", "/proc/self/oom_score_adj"], None)
    else:
        proc = env._run_bash("cat /proc/self/oom_score_adj")
    try:
        out, _ = proc.communicate(timeout=10)
        assert proc.returncode == 0
        if isinstance(out, bytes):
            out = out.decode()
        assert out.strip() == ("1000" if inside else parent)
        assert Path("/proc/self/oom_score_adj").read_text().strip() == parent
    finally:
        env._reap_long_lived(proc)


def test_no_choom_preserves_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal_oom, "run_scope_cgroup", lambda: tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    argv = ["bash", "-c", "true"]
    assert terminal_oom.prefer_command_victim(argv) == argv
