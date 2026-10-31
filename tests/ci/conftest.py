# [daveey] fork-ci-runner: tests-os is off on the fork (no macOS/Windows runner,
# hosts/hermes-hub/gh-runner/README.md). Written by scripts/fork-ci-runner.sh.
import ast as _fork_ast
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



def _fork_os_lane_failure(item, rep):
    """Inspect the crash, never the source/locals printed around it.

    The allowlist remains the outer bound. Its mixed parameter cases and
    consumer dictionaries need proof that the disabled OS lane is the failure,
    not merely a lane mentioned alongside a broken Linux consumer.
    """
    message = str(getattr(getattr(rep.longrepr, "reprcrash", None), "message", ""))
    params = getattr(getattr(item, "callspec", None), "params", {})
    name = item.originalname or item.name
    if name == "test_real_update_suites_gate_the_merge_when_their_lane_fires":
        return (params.get("job") == "tests-os" and message ==
                "TypeError: unsupported operand type(s) for +: 'bool' and 'str'")
    if name == "test_selected_update_consumers_must_succeed":
        if params.get("jobs") == ("tests-os",) and params.get("result") in ("absent", "skipped"):
            return message.startswith("AssertionError: assert True is (")
    # The routing tests print a flat consumer->bool map. Keep their known OS-only
    # mismatch, but a False Linux consumer is a real regression even beside it.
    if name not in {"test_pull_requests_and_main_pushes_never_run_e2e"}:
        for raw in _fork_re.findall(r"\{[^{}]*\}", message):
            try:
                values = _fork_ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                continue
            if (isinstance(values, dict) and values and
                    all(isinstance(k, str) and isinstance(v, bool) for k, v in values.items())):
                failed = [k for k, v in values.items() if not v]
                if failed and all(k.startswith("tests-os/") or k == "desktop_updater" for k in failed):
                    return True
    # Remove container reprs, recursively, before looking for an OS-lane subject.
    # This also strips nested jobs dicts: incidental lists/dicts/sets cannot grant a skip.
    while True:
        stripped = _fork_re.sub(r"\[[^\[\]{}]*\]|\{[^\[\]{}]*\}", "", message)
        if stripped == message:
            break
        message = stripped
    return bool(_FORK_OS_LANE_RE.search(message))


@_fork_pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    # A fork_guard test asserts the fork's own wiring: its failure is real even when it names tests-os.
    if (_FORK_NO_OS_LANE and rep.when == "call" and rep.failed and not item.get_closest_marker("fork_guard")
            and item.nodeid.split("[")[0] in _FORK_OS_LANE_TESTS
            and _fork_os_lane_failure(item, rep)):
        rep.outcome = "skipped"
        rep.longrepr = (str(item.path), item.location[1] or 0, "fork: asserts the macOS/Windows tests-os lane, which is off on daveey/hermes")
