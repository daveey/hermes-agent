"""Real PM preflight failures must still allow system Git consumers."""
import importlib
import json
import shutil
import subprocess

import pytest

import pm
from tools import checkpoint_manager, working_diff


@pytest.mark.parametrize("consumer", ["checkpoint", "diff"])
def test_git_consumers_survive_missing_pm_runtime(tmp_path, monkeypatch, consumer):
    stamp = tmp_path / "install.json"
    stamp.write_text(json.dumps({
        "distribution": "docker", "pmRuntime": str(tmp_path / "missing-runtime"),
    }), encoding="utf-8")
    monkeypatch.setattr(importlib.import_module("pm.paths"), "install_stamp_path", lambda project: stamp)
    with pytest.raises(pm.InstallError, match="packaged PM runtime is missing or invalid"):
        pm.ensure("git")

    git = shutil.which("git")
    assert git is not None
    repo = tmp_path / "repo"
    subprocess.run([git, "init", str(repo)], check=True, capture_output=True)
    (repo / "new.txt").write_text("runtime fallback content\n", encoding="utf-8")
    monkeypatch.setattr(checkpoint_manager, "CHECKPOINT_BASE", tmp_path / "checkpoints")
    # Do not replace pm.ensure: exercise its real preflight through the consumer.
    if consumer == "checkpoint":
        assert checkpoint_manager.CheckpointManager(enabled=True).ensure_checkpoint(str(repo)) is True
    else:
        result = working_diff.collect_working_diff(str(repo))
        assert result["success"] is True
        assert "+runtime fallback content" in result["diff"]
