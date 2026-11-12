"""Exercise the external relay child, not a mocked subprocess return."""

import json
import logging
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent import credential_relay_repull as relay


@pytest.mark.parametrize("polluted", [False, True])
def test_pull_child_owns_python_imports(tmp_path, monkeypatch, polluted):
    """A timer-like clean parent and a PM-bootstrapped parent both pull successfully."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(relay, "_ra", lambda: SimpleNamespace(logger=logging.getLogger(__name__)))
    poison = tmp_path / "backend-site-packages"
    poison.mkdir()
    # Deterministic version skew: the child needs stdlib ssl, not backend packages.
    (poison / "ssl.py").write_text("raise AttributeError(\"module 'lib' has no attribute 'GEN_EMAIL'\")\n")
    output = tmp_path / "receipt.json"
    script = tmp_path / "pull.py"
    script.write_text(
        "import ssl, json, os, sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text(json.dumps({k: os.environ.get(k) for k in "
        "('HERMES_HOME', 'AWS_DEFAULT_REGION', 'WORKER_OAUTH_STAMP', 'PYTHONPATH', 'PYTHONHOME')}))\n"
    )
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("WORKER_OAUTH_STAMP", str(tmp_path / "stamp"))
    monkeypatch.delenv("PYTHONHOME", raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    cmd = [sys.executable, str(script), str(output)]
    if polluted:
        monkeypatch.setenv("PYTHONPATH", str(poison))
        broken = subprocess.run(cmd, capture_output=True, text=True, check=False)
        assert broken.returncode != 0
        assert "GEN_EMAIL" in broken.stderr
    before = dict(os.environ)
    relay._run_pull(cmd)
    assert output.exists(), "relay child failed before writing its success receipt"
    result = json.loads(output.read_text())
    assert result == {"HERMES_HOME": before["HERMES_HOME"], "AWS_DEFAULT_REGION": "us-west-2",
                      "WORKER_OAUTH_STAMP": before["WORKER_OAUTH_STAMP"],
                      "PYTHONPATH": None, "PYTHONHOME": None}
    assert dict(os.environ) == before


def test_pull_child_ignores_backend_pythonhome(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PYTHONHOME", str(tmp_path / "nonexistent-runtime"))
    monkeypatch.setattr(relay, "_ra", lambda: SimpleNamespace(logger=logging.getLogger(__name__)))
    script = tmp_path / "pull.py"
    receipt = tmp_path / "ok"
    script.write_text("import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('ok')\n")
    relay._run_pull([sys.executable, str(script), str(receipt)])
    assert receipt.read_text() == "ok"
    assert os.environ["PYTHONHOME"] == str(tmp_path / "nonexistent-runtime")
