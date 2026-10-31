# [daveey] fork-ci-runner: tests-os is off on the fork (no macOS/Windows runner,
# hosts/hermes-hub/gh-runner/README.md). Written by scripts/fork-ci-runner.sh.
import pathlib as _fork_pathlib
import re as _fork_re

import pytest as _fork_pytest

_FORK_NO_OS_LANE = "# fork: no macOS/Windows runner" in (
    _fork_pathlib.Path(__file__).resolve().parents[2] / ".github/workflows/ci.yaml"
).read_text(encoding="utf-8")
# desktop_updater's only consumer is tests-os[windows].
_FORK_OS_LANE_RE = _fork_re.compile(r"tests-os|e2e-windows|_windows_desktop_updater|os-tests\[|'desktop_updater': False")
# Only these upstream tests (node id without the parametrize suffix) have the tests-os lane as their
# subject, so only they may become skips. A failure is never a skip just because its report prints the
# lane name (e.g. the `all-checks-pass.needs` list): every other test, including every fork-authored
# one, fails for real. A new upstream test about the lane fails here until it is added on purpose.
_FORK_OS_LANE_TESTS = frozenset({
    "tests/ci/test_classify_changes.py::test_real_update_suites_gate_the_merge_when_their_lane_fires",
    "tests/ci/test_required_results.py::test_selected_update_consumers_must_succeed",
    "tests/ci/test_update_ci_routing.py::test_every_set_lane_reaches_its_consumer_whatever_the_other_lanes_say",
    "tests/ci/test_update_ci_routing.py::test_marker_corpus_change_runs_every_language_that_reads_it",
    "tests/ci/test_update_ci_routing.py::test_pull_requests_and_main_pushes_never_run_e2e",
    "tests/ci/test_update_ci_routing.py::test_replay_rejects_ineffective_required_step",
    "tests/ci/test_update_ci_routing.py::test_replay_uses_executed_gate_output",
    "tests/ci/test_update_ci_routing.py::test_strict_acceptance_dispatch_reaches_every_e2e_suite",
    "tests/ci/test_update_ci_routing.py::test_update_owner_change_dispatches_its_suites_end_to_end",
    "tests/ci/test_update_ci_routing.py::test_update_path_change_reaches_the_suites_that_consume_it",
    "tests/ci/test_update_ci_routing.py::test_update_path_change_selects_every_suite_that_exercises_it",
    "tests/ci/test_update_transitive_routing.py::test_second_hop_update_change_dispatches_real_update_consumers",
})


@_fork_pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    # A fork_guard test asserts the fork's own wiring: its failure is real even when it names tests-os.
    if (_FORK_NO_OS_LANE and rep.when == "call" and rep.failed and not item.get_closest_marker("fork_guard")
            and item.nodeid.split("[")[0] in _FORK_OS_LANE_TESTS
            and _FORK_OS_LANE_RE.search(str(rep.longrepr))):
        rep.outcome = "skipped"
        rep.longrepr = (str(item.path), item.location[1] or 0, "fork: asserts the macOS/Windows tests-os lane, which is off on daveey/hermes")
