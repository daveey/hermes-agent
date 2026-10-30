"""Execute JS runner provisioning against an apt client with empty indexes."""

import json
import os
from pathlib import Path
import subprocess

import pytest

import hermes_yaml as yaml


WORKFLOW = Path(__file__).resolve().parents[3] / ".github/workflows/js-tests.yml"


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("update_status", [0, 100])
def test_js_provisioning_refreshes_indexes_and_fails_closed(tmp_path, update_status):
    workflow = yaml.safe_load(WORKFLOW.read_text())
    step = next(
        step for step in workflow["jobs"]["check"]["steps"]
        if step.get("name") == "Install zsh and a virtual display for Electron"
    )
    sudo = tmp_path / "sudo"
    sudo.write_text('#!/bin/sh\nexec "$@"\n')
    sudo.chmod(0o755)
    apt = tmp_path / "apt-get"
    apt.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "root = pathlib.Path(os.environ['APT_TEST_ROOT'])\n"
        "with (root / 'calls').open('a') as log:\n"
        "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'update':\n"
        "    status = int(os.environ['APT_UPDATE_STATUS'])\n"
        "    if not status: (root / 'indexes').touch()\n"
        "    sys.exit(status)\n"
        "sys.exit(0 if (root / 'indexes').exists() else 100)\n"
    )
    apt.chmod(0o755)
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}",
               APT_TEST_ROOT=str(tmp_path), APT_UPDATE_STATUS=str(update_status))
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        env=env, capture_output=True, text=True, check=False,
    )
    calls = [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]
    assert result.returncode == update_status, result.stderr
    assert calls[0] == ["update", "--error-on=any"]
    if update_status:
        assert len(calls) == 1
    else:
        assert len(calls) == 2
        assert calls[1][:2] == ["install", "-y"]
        assert {"zsh", "xvfb", "libnotify4", "libxss1", "xdg-utils"} <= set(calls[1][2:])
