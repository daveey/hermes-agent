"""The fork's tests-os conftest hook only skips tests whose subject is the off tests-os lane.

scripts/fork-ci-runner.sh writes tests/ci/conftest.py. It used to turn any failing tests/ci test into a
skip when its report merely contained "tests-os", so a fork-authored assertion that printed the
``all-checks-pass.needs`` list (which names tests-os) could never fail. These tests run the real hook in
a throwaway tree.
"""
from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONFTEST = ROOT / "tests/ci/conftest.py"
WRITER = ROOT / "scripts/fork-ci-runner.sh"
ALLOWED = (
    "tests/ci/test_update_ci_routing.py::test_pull_requests_and_main_pushes_never_run_e2e"
)
LANE_TEST = ALLOWED.rsplit("::", 1)[1]


def _writer_heredoc() -> str:
    m = re.search(r"cat >> tests/ci/conftest\.py <<'PY'\n(.*?)\nPY\n", WRITER.read_text(), re.DOTALL)
    assert m, "fork-ci-runner.sh no longer writes tests/ci/conftest.py"
    return m.group(1) + "\n"


def test_writer_and_checked_in_conftest_are_the_same_hook():
    assert _writer_heredoc() == CONFTEST.read_text(encoding="utf-8")


def _run(tmp_path: Path, files: dict[str, str], *args: str, conftest: str | None = None):
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/workflows/ci.yaml").write_text(
        "jobs:\n  tests-os:\n    if: false  # fork: no macOS/Windows runner\n"
    )
    (tmp_path / "tests/ci").mkdir(parents=True)
    (tmp_path / "tests/ci/conftest.py").write_text(
        conftest if conftest is not None else CONFTEST.read_text(encoding="utf-8")
    )
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    fork_guard: x\n")
    for name, body in files.items():
        prefix = _NEEDS if body.startswith(_NEEDS) else ""
        text = prefix + textwrap.dedent(body.removeprefix(prefix))
        (tmp_path / "tests/ci" / name).write_text(text)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "no:xdist", "-rs", "-q", *args],
        cwd=tmp_path, capture_output=True, text=True, timeout=120, check=False,
    )


_NEEDS = "NEEDS = ['detect', 'tests', 'tests-os', 'lint', 'js-tests']\n"


def test_failure_that_only_prints_a_list_containing_tests_os_stays_a_failure(tmp_path):
    p = _run(tmp_path, {"test_gate.py": _NEEDS + """
        def test_js_tests_gates_the_merge():
            needs = [n for n in NEEDS if n != 'js-tests']
            assert 'js-tests' in needs
    """})
    assert p.returncode == 1, p.stdout
    assert "1 failed" in p.stdout and "skipped" not in p.stdout, p.stdout


def test_failure_printing_a_dict_naming_tests_os_stays_a_failure(tmp_path):
    p = _run(tmp_path, {"test_gate.py": """
        def test_jobs():
            jobs = {'tests-os': {'if': False}, 'lint': {}}
            assert 'js-tests' in jobs
    """})
    assert p.returncode == 1 and "skipped" not in p.stdout, p.stdout


def test_fork_guard_test_failing_with_tests_os_in_report_stays_a_failure(tmp_path):
    p = _run(tmp_path, {"test_update_ci_routing.py": _NEEDS + f"""
        import pytest

        @pytest.mark.fork_guard
        def {LANE_TEST}():
            assert 'js-tests' not in NEEDS and 'tests-os' not in NEEDS
    """})
    assert p.returncode == 1 and "skipped" not in p.stdout, p.stdout


def test_assertion_about_the_os_lane_itself_still_becomes_a_skip(tmp_path):
    p = _run(tmp_path, {"test_update_ci_routing.py": _NEEDS + f"""
        def {LANE_TEST}():
            assert 'tests-os' not in NEEDS, NEEDS
    """})
    assert p.returncode == 0, p.stdout
    assert "1 skipped" in p.stdout
    assert "fork: asserts the macOS/Windows tests-os lane" in p.stdout


def test_lane_test_failing_for_another_reason_stays_a_failure(tmp_path):
    p = _run(tmp_path, {"test_update_ci_routing.py": f"""
        def {LANE_TEST}():
            assert 1 == 2
    """})
    assert p.returncode == 1 and "skipped" not in p.stdout, p.stdout


def test_lane_name_in_source_does_not_hide_an_unrelated_failure(tmp_path):
    p = _run(tmp_path, {"test_update_ci_routing.py": f"""
        def {LANE_TEST}():
            lanes = ['tests-os']
            assert 1 == 2
    """})
    assert p.returncode == 1 and "skipped" not in p.stdout, p.stdout


def test_allowlisted_test_printing_needs_but_checking_js_stays_a_failure(tmp_path):
    p = _run(tmp_path, {"test_update_ci_routing.py": _NEEDS + f"""
        def {LANE_TEST}():
            needs = [n for n in NEEDS if n != 'js-tests']
            assert 'js-tests' in needs
    """})
    assert p.returncode == 1 and "skipped" not in p.stdout, p.stdout


@pytest.mark.parametrize("container", ["['tests-os']", "{'tests-os': {'enabled': False}}", "{'tests-os', 'lint'}"])
def test_allowlisted_test_printing_containers_but_checking_js_stays_a_failure(tmp_path, container):
    p = _run(tmp_path, {"test_update_ci_routing.py": f"""
        def {LANE_TEST}():
            needs = {container}
            assert 'js-tests' in needs
    """})
    assert p.returncode == 1 and "skipped" not in p.stdout, p.stdout


@pytest.mark.parametrize("linux_ok,expected", [(True, 0), (False, 1)])
def test_consumer_map_only_skips_when_the_os_consumer_is_the_only_failure(tmp_path, linux_ok, expected):
    p = _run(tmp_path, {"test_update_ci_routing.py": f"""
        def test_every_set_lane_reaches_its_consumer_whatever_the_other_lanes_say():
            consumers = {{'tests/e2e': {linux_ok!r}, 'tests-os/e2e-windows': False}}
            assert all(consumers.values()), consumers
    """})
    assert p.returncode == expected, p.stdout
    assert ("skipped" in p.stdout) == (expected == 0), p.stdout


@pytest.mark.parametrize("body", [
    "assert 'js-tests' in ('tests-os', 'lint')",
    "assert 'js-tests' in 'tests-os,lint'",
    "assert ['lint', 'tests-os'] == ['lint', 'js-tests']",
    "assert {'lint', 'tests-os'} == {'lint', 'js-tests'}",
    "a = {'tests/e2e': False}; b = {'tests-os/e2e-windows': False}; assert all(a.values()) and all(b.values()), (a, b)",
    "assert {'tests/e2e': False, 'tests-os/w': False} == {'tests/e2e': True, 'tests-os/w': True}",
])
def test_other_predicates_cannot_borrow_an_os_diagnostic(tmp_path, body):
    p = _run(tmp_path, {"test_update_ci_routing.py":
        "def test_every_set_lane_reaches_its_consumer_whatever_the_other_lanes_say():\n    " + body + "\n"})
    assert p.returncode == 1 and "skipped" not in p.stdout, p.stdout


def test_failure_printing_a_set_naming_tests_os_stays_a_failure(tmp_path):
    p = _run(tmp_path, {"test_gate.py": """
        def test_jobs():
            jobs = {'tests-os', 'lint'}
            assert 'js-tests' in jobs
    """})
    assert p.returncode == 1 and "skipped" not in p.stdout, p.stdout


def test_parametrized_lane_test_still_becomes_a_skip(tmp_path):
    p = _run(tmp_path, {"test_update_ci_routing.py": _NEEDS + f"""
        import pytest

        @pytest.mark.parametrize("n", [1, 2])
        def {LANE_TEST}(n):
            assert 'tests-os' not in NEEDS
    """})
    assert p.returncode == 0 and "2 skipped" in p.stdout, p.stdout


def test_hook_is_inert_on_a_tree_that_keeps_the_os_lane(tmp_path):
    (tmp_path / ".github/workflows").mkdir(parents=True)
    p = _run_with_lane_on(tmp_path)
    assert p.returncode == 1 and "skipped" not in p.stdout, p.stdout


def _run_with_lane_on(tmp_path: Path):
    # same layout as _run, but ci.yaml has no fork marker
    (tmp_path / "tests/ci").mkdir(parents=True)
    (tmp_path / ".github/workflows/ci.yaml").write_text("jobs:\n  tests-os:\n    if: true\n")
    (tmp_path / "tests/ci/conftest.py").write_text(CONFTEST.read_text(encoding="utf-8"))
    (tmp_path / "tests/ci" / "test_update_ci_routing.py").write_text(
        f"def {LANE_TEST}():\n    assert 'tests-os' not in ['tests-os']\n"
    )
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "no:xdist", "-q"],
        cwd=tmp_path, capture_output=True, text=True, timeout=120, check=False,
    )


def test_writer_replaces_an_older_hook_block_instead_of_keeping_it(tmp_path):
    script = WRITER.read_text()
    strip = re.search(r"(python3 - <<'PY'\nimport pathlib\nf = pathlib\.Path\('tests/ci/conftest\.py'\).*?\nPY\n)", script, re.DOTALL)
    append = re.search(r"(cat >> tests/ci/conftest\.py <<'PY'\n.*?\nPY\n)", script, re.DOTALL)
    assert strip and append
    (tmp_path / "tests/ci").mkdir(parents=True)
    old = (
        "# upstream part\nX = 1\n"
        "# [daveey] fork-ci-runner: tests-os is off on the fork (old block)\nOLD_BLOCK = True\n"
    )
    (tmp_path / "tests/ci/conftest.py").write_text(old)
    for _ in range(2):  # and idempotent
        subprocess.run(["bash", "-ec", strip.group(1) + append.group(1)], cwd=tmp_path, check=True)
    got = (tmp_path / "tests/ci/conftest.py").read_text()
    assert got.startswith("# upstream part\nX = 1\n")
    assert "OLD_BLOCK" not in got
    assert got.count("_FORK_OS_LANE_TESTS = frozenset") == 1
    assert got.endswith(CONFTEST.read_text(encoding="utf-8"))
