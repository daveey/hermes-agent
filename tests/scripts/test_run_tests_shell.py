"""The canonical shell must run tests and propagate a failing test's status."""
from pathlib import Path
import os
import subprocess
import sys

import pytest


@pytest.mark.platforms("posix")
def test_shell_runner_executes_tests_and_propagates_failure(tmp_path):
    case = tmp_path / "test_runner_canary.py"
    marker = tmp_path / "executed"
    case.write_text(
        "from pathlib import Path\nimport os\n"
        "def test_canary():\n"
        f"    Path({str(marker)!r}).write_text('executed')\n"
        "    assert os.environ['PATHEXT'] == '.COM;.EXE;.BAT;.CMD'\n"
        "    assert False, 'runner failure propagation canary'\n",
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["bash", str(root / "scripts/run_tests.sh"), "-j", "1", str(case)],
        cwd=tmp_path, capture_output=True, text=True, timeout=180,
        env={**os.environ, "HERMES_PYTHON": sys.executable, "HERMES_TEST_FILE_RETRIES": "0",
             "PATHEXT": ".COM;.EXE;.BAT;.CMD"},
             check=False,
    )
    assert marker.read_text(encoding="utf-8-sig") == "executed", result.stdout + result.stderr
    assert result.returncode != 0, result.stdout + result.stderr
    assert "runner failure propagation canary" in result.stdout + result.stderr


@pytest.mark.platforms("posix")
def test_shell_runner_forwards_the_flaky_report_path(tmp_path):
    """CI sets HERMES_TEST_FLAKY_REPORT on the step; the hermetic env -i must keep it (t_94f2c8ba)."""
    marker = tmp_path / "failed-once"
    case = tmp_path / "test_flaky_canary.py"
    case.write_text(
        "from pathlib import Path\n"
        "def test_flaky_canary():\n"
        f"    m = Path({str(marker)!r})\n"
        "    if not m.exists():\n"
        "        m.write_text('x')\n"
        "        assert False, 'first attempt flake'\n",
        encoding="utf-8",
    )
    report = tmp_path / "out" / "flaky.json"
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["bash", str(root / "scripts/run_tests.sh"), "-j", "1", str(case)],
        cwd=tmp_path, capture_output=True, text=True, timeout=180,
        env={**os.environ, "HERMES_PYTHON": sys.executable, "HERMES_TEST_FILE_RETRIES": "1",
             "HERMES_TEST_FLAKY_REPORT": str(report)},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    import json
    ids = [e["test_id"] for e in json.loads(report.read_text(encoding="utf-8-sig"))["flaky"]]
    assert len(ids) == 1 and ids[0].endswith("test_flaky_canary.py::test_flaky_canary"), ids


@pytest.mark.platforms("posix")
def test_shell_runner_forwards_host_slot_settings(tmp_path):
    """The hermetic env -i keeps HERMES_TEST_HOST_SLOTS / SLOT_DIR; the child sees SLOT_HELD=1."""
    case = tmp_path / "test_slot_canary.py"
    case.write_text("import os\ndef test_slot_canary():\n"
                    "    assert os.environ.get('HERMES_TEST_SLOT_HELD') == '1'\n", encoding="utf-8")
    slot_dir = tmp_path / "slots"
    env = {k: v for k, v in os.environ.items() if k != "HERMES_TEST_SLOT_HELD"}
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["bash", str(root / "scripts/run_tests.sh"), "-j", "1", str(case)],
        cwd=tmp_path, capture_output=True, text=True, timeout=180,
        env={**env, "HERMES_PYTHON": sys.executable, "HERMES_TEST_FILE_RETRIES": "0",
             "HERMES_TEST_HOST_SLOTS": "3", "HERMES_TEST_SLOT_DIR": str(slot_dir)},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    names = {p.name for p in slot_dir.iterdir()}
    assert names and names <= {"0.lock", "1.lock", "2.lock"}, names
