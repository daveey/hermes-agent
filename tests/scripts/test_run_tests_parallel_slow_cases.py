"""run_tests_parallel.py prints one ``⏱ slow test case`` line per test case at or above the threshold.

The CI-timings review item (scripts/ci/timings_report.py) reads these lines from the test-slice job logs to tell
whether a PR's own added test case is slow (>= 10 s) before it attributes a wall-time regression to the PR.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests._fixtures.runner_probe import add_runner_imports

_REAL = Path(__file__).resolve().parents[2] / "scripts"
_spec = importlib.util.spec_from_file_location("run_tests_parallel_slow", _REAL / "run_tests_parallel.py")
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


_OUTPUT = """\
tests/x/test_a.py ..                                                     [100%]

============================== slowest durations ===============================
11.02s call     tests/x/test_a.py::TestA::test_slow[p1]
1.50s setup    tests/x/test_a.py::TestA::test_slow[p1]
4.00s call     tests/x/test_a.py::test_medium

(3 durations < 2.5s hidden.  Use -vv to show these durations.)
============================== 2 passed in 16.80s ==============================
"""


def test_slow_cases_sum_phases_and_keep_only_cases_over_the_threshold():
    assert _mod._slow_cases_from_output(_OUTPUT, 10.0) == {"tests/x/test_a.py::TestA::test_slow[p1]": 12.52}
    assert _mod._slow_cases_from_output(_OUTPUT, 4.0) == {
        "tests/x/test_a.py::TestA::test_slow[p1]": 12.52,
        "tests/x/test_a.py::test_medium": 4.0,
    }
    assert _mod._slow_cases_from_output("no durations section\n", 10.0) == {}


def test_durations_flags_are_added_unless_disabled_or_given():
    assert _mod._durations_args([], 10.0) == ["--durations=0", "--durations-min=2.5"]
    assert _mod._durations_args(["-q"], 0.0) == []
    assert _mod._durations_args(["--durations=5"], 10.0) == []
    assert _mod._durations_args(["--durations", "5"], 10.0) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX runner probe")
def test_runner_prints_the_slow_case_line(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    root = tmp_path / "runner-root"
    (root / "scripts" / "ci").mkdir(parents=True)
    shutil.copy2(_REAL / "run_tests_parallel.py", root / "scripts")
    shutil.copy2(_REAL / "run_tests_host_slots.py", root / "scripts")
    shutil.copy2(_REAL / "run_tests_parallel_rss.py", root / "scripts")
    shutil.copy2(_REAL / "run_tests_launch.py", root / "scripts")
    shutil.copy2(_REAL / "run_tests_fork_child.py", root / "scripts")
    add_runner_imports(root)
    shutil.copy2(_REAL / "ci" / "list_os_marked_tests.py", root / "scripts" / "ci")
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    (probe_dir / "test_probe_slow.py").write_text(textwrap.dedent("""
        import time

        def test_sleeps():
            time.sleep(0.6)

        def test_fast():
            pass
    """), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(root / "scripts" / "run_tests_parallel.py"), "--paths", str(probe_dir), "-j", "1",
         "--file-timeout", "60"],
        cwd=probe_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
        timeout=120, check=False, env={**__import__("os").environ, "HERMES_TEST_SLOW_CASE_S": "0.4"},
    )
    assert proc.returncode == 0, proc.stdout
    # Only lower bounds hold on a CPU-starved runner: the 0.6 s sleep is a floor (a throttled slice measured it
    # at 1.75 s), and even ``test_fast`` may cross 0.4 s there. Which cases fall under the threshold is pinned
    # by the parser unit test above, so this probe checks the printed line's shape and the threshold floor.
    cases: dict[str, float] = {}
    for ln in proc.stdout.splitlines():
        if "⏱ slow test case" in ln:
            m = re.fullmatch(r"\s*⏱ slow test case (\d+\.\d\d)s (\S+)", ln)
            assert m, ln
            cases[m.group(2).rsplit("::", 1)[-1]] = float(m.group(1))
    assert set(cases) <= {"test_sleeps", "test_fast"}, proc.stdout
    assert cases.get("test_sleeps", 0.0) >= 0.6, proc.stdout
    assert all(secs >= 0.4 for secs in cases.values()), proc.stdout
