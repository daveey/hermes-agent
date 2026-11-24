"""The module entry must invoke canonical code, not a second Capability type."""
import os
import subprocess
import sys


def test_module_uses_canonical_main(tmp_path):
    # runpy executes the real __main__ guard in a fresh interpreter. Only canonical
    # main is replaced: calling a duplicate module's main fails this sentinel.
    probe = tmp_path / 'probe.py'
    probe.write_text(
        "import runpy\n"
        "from hermes_cli import kanban_selfwork_exception as sx\n"
        "sx.main = lambda: 37\n"
        "runpy.run_module('hermes_cli.kanban_selfwork_exception', run_name='__main__')\n"
    )
    result = subprocess.run([sys.executable, str(probe)], env={**os.environ, 'PYTHONPATH': os.getcwd()},
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 37, (result.stdout, result.stderr)
