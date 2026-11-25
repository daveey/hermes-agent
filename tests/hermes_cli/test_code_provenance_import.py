"""Import must be inert; CLI execution is separately exercised by client tests."""
import os
import subprocess
import sys
from pathlib import Path


def test_client_import_does_not_execute_main(tmp_path):
    env = {**os.environ, 'HERMES_HOME': str(tmp_path), 'PYTHONDONTWRITEBYTECODE': '1'}
    probe = tmp_path / 'probe.py'
    probe.write_text('import sys\nsys.path.insert(0, sys.argv[1])\nfrom hermes_cli import code_provenance_client\nprint("IMPORT_OK")\n')
    result = subprocess.run([sys.executable, '-B', str(probe), str(Path(__file__).resolve().parents[2])],
                            env=env, capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'IMPORT_OK'
