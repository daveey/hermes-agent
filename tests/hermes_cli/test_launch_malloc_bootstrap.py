"""Bootstrap must not leak exec-time allocator settings on degraded startup."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("marked,value", [(True, "1"), (False, "0"), (False, "1")])
def test_bootstrap_child_environment(tmp_path, failed, marked, value):
    source = Path(__file__).resolve().parents[2]
    script = f'''
import os, sys, subprocess
sys.path.insert(0, {str(source)!r})
from hermes_cli import venv_sync, _early_recovery
import pm.environments
_early_recovery.restore_interrupted_pull = lambda *a: False
_early_recovery.recover_if_needed = lambda *a: None
pm.environments.activate_dependencies = lambda *a: None
def prepare(*a):
    if {failed!r}:
        raise RuntimeError('injected completion failure')
    return None
venv_sync.prepare_launch = prepare
sys.argv = ['hermes', 'gateway', 'run']
os.environ['MallocSpaceEfficient'] = {value!r}
os.environ.pop('HERMES_LAUNCH_MALLOC_SET', None)
if {marked!r}:
    os.environ['HERMES_LAUNCH_MALLOC_SET'] = '1'
import hermes_bootstrap
code = 'import os,json; print(json.dumps([os.environ.get("MallocSpaceEfficient"), os.environ.get("HERMES_LAUNCH_MALLOC_SET")]))'
child = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True,
                       text=True, encoding='utf-8', check=True, timeout=20)
print(child.stdout.strip())
'''
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        env=dict(os.environ, HERMES_HOME=str(tmp_path / "home")),
        capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [None if marked else value, None]
    assert ("injected completion failure" in result.stderr) == failed
