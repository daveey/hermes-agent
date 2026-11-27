"""The requested composed acceptance must execute, never silently lose its opt-in."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.platforms("posix")
def test_canonical_runner_preserves_composed_opt_in_without_credentials(tmp_path):
    knobs = {"HERMES_COMPOSED_BOARD_URL": "http://127.0.0.1:8794", "HERMES_COMPOSED_REQUIRED": "1"}
    case = tmp_path / "test_composed_environment_canary.py"
    marker = tmp_path / "executed"
    case.write_text(
        "import os\nfrom pathlib import Path\n"
        "def test_canary():\n"
        f"    expected = {knobs!r}\n"
        "    assert {k: os.environ.get(k) for k in expected} == expected\n"
        "    assert 'HERMES_COMPOSED_ADMIN_KEY' not in os.environ\n"
        "    assert 'HERMES_COMPOSED_UNTRUSTED_SECRET' not in os.environ\n"
        f"    Path({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["bash", str(root / "scripts/run_tests.sh"), "-j", "1", str(case)],
        cwd=tmp_path, capture_output=True, text=True, timeout=180,
        env={**os.environ, "HERMES_PYTHON": sys.executable, "HERMES_TEST_FILE_RETRIES": "0",
             **knobs, "HERMES_COMPOSED_ADMIN_KEY": "must-not-forward",
             "HERMES_COMPOSED_UNTRUSTED_SECRET": "must-not-forward"},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text() == "executed"
