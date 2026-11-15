"""Native bootstrap regression coverage for the exec-time allocator policy (#3122)."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


# Run the real bootstrap and real exec boundaries. Only dependency preparation is
# replaced: this test must not update an installation or activate another graph.
_DRIVER = r'''
import json
import os
from pathlib import Path
import sys

root, log, managed, *argv = sys.argv[1:]
sys.path.insert(0, root)
from pm import environments
from hermes_cli import _early_recovery, venv_sync

environments.activate_dependencies = lambda root: None
_early_recovery.restore_interrupted_pull = lambda root: False
_early_recovery.recover_if_needed = lambda root: None
venv_sync.prepare_launch = lambda root, argv: (
    Path(sys.executable) if managed == "yes" and not Path(log).exists() else None
)
venv_sync.relaunch_command = lambda *args: sys.orig_argv

# Persist across exec; fail immediately on a second exec rather than hanging.
def audit(event, args):
    if event != "os.exec":
        return
    path = Path(log)
    rows = json.loads(path.read_text()) if path.exists() else []
    rows.append({name: os.environ.get(name) for name in
                 ("MallocSpaceEfficient", "HERMES_LAUNCH_MALLOC_SET")})
    path.write_text(json.dumps(rows))
    if len(rows) > 1:
        raise SystemExit("bootstrap attempted a second exec")

sys.addaudithook(audit)
sys.argv = ["hermes", *argv]
import hermes_bootstrap
print(json.dumps({name: os.environ.get(name) for name in
                  ("MallocSpaceEfficient", "HERMES_LAUNCH_MALLOC_SET")}))
'''


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("managed", [False, True], ids=["current-runtime", "managed-relaunch"])
@pytest.mark.parametrize("argv", [["gateway", "run"], ["serve"], ["chat"]])
def test_bootstrap_allocator_exec_once(tmp_path, managed, argv):
    driver = tmp_path / "bootstrap_driver.py"
    driver.write_text(_DRIVER, encoding="utf-8")
    log = tmp_path / "execs.json"
    env = {**os.environ, "HOME": str(tmp_path), "HERMES_HOME": str(tmp_path / "home")}
    for name in ("MallocSpaceEfficient", "HERMES_LAUNCH_MALLOC_SET", "HERMES_MALLOC_SPACE_EFFICIENT"):
        env.pop(name, None)
    result = subprocess.run(
        [sys.executable, str(driver), str(Path(__file__).resolve().parents[1]),
         str(log), "yes" if managed else "no", *argv],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    rows = json.loads(log.read_text(encoding="utf-8")) if log.exists() else []
    server = argv[0] != "chat"
    expected = {"MallocSpaceEfficient": "1" if server else None,
                "HERMES_LAUNCH_MALLOC_SET": "1" if server else None}
    assert rows == ([expected] if managed or server else [])
    assert json.loads(result.stdout) == {
        "MallocSpaceEfficient": None, "HERMES_LAUNCH_MALLOC_SET": None,
    }
