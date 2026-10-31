# [daveey] fork-ci-runner: tests-os is off on the fork (no macOS/Windows runner,
# hosts/hermes-hub/gh-runner/README.md). Written by scripts/fork-ci-runner.sh.
import ast as _fork_ast
import pathlib as _fork_pathlib
import re as _fork_re

import pytest as _fork_pytest

_FORK_NO_OS_LANE = "# fork: no macOS/Windows runner" in (
    _fork_pathlib.Path(__file__).resolve().parents[2] / ".github/workflows/ci.yaml"
).read_text(encoding="utf-8")
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
    """Recognize only the allowlisted upstream assertions' OS-only mismatch.

    Source, locals and arbitrary diagnostic mentions never grant a skip. Match
    the known failed predicate, and for a composite consumer predicate require
    every reported non-OS consumer to have succeeded.
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
        return message.startswith("AssertionError: assert ['tests'] == ['tests', 'tests-os']\n")
    if name == "test_strict_acceptance_dispatch_reaches_every_e2e_suite":
        return message == "AssertionError: tests-os/e2e-windows did not run\nassert None is not None"
    if name in {
        "test_update_path_change_selects_every_suite_that_exercises_it",
        "test_update_path_change_reaches_the_suites_that_consume_it",
    }:
        if _fork_re.search(
            r"desktop_updater never reaches its tests\nassert False\n \+  where False = _windows_desktop_updater_tests_selected\(",
            message,
        ):
            return True
    if name == "test_pull_requests_and_main_pushes_never_run_e2e":
        # This predicate checks lane membership, not the values printed inside run.
        while True:
            stripped = _fork_re.sub(r"\[[^\[\]{}]*\]|\{[^\[\]{}]*\}", "", message)
            if stripped == message:
                break
            message = stripped
        return (bool(_fork_re.fullmatch(r"AssertionError: assert \('tests' in [\s},.]* and 'tests-os' in [\s},.]*\)", message)) or
                bool(_fork_re.search(r"(?:^|\n|AssertionError: )assert 'tests-os' (?:not )?in *$", message)))
    # Remaining allowlisted predicates are all(consumer_map.values()). Equality
    # diffs and membership failures aren't this assertion, even if they name OS.
    if not _fork_re.search(r"(?:^AssertionError: |\n)assert False\n \+  where False = all\(dict_values\(", message):
        return False
    maps = []
    for raw in _fork_re.findall(r"\{[^{}]*\}", message):
        try:
            values = _fork_ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            continue
        if (isinstance(values, dict) and values and
                all(isinstance(k, str) and isinstance(v, bool) for k, v in values.items()) and
                all(k.startswith(("tests/", "tests-os/")) or k in
                    {"python", "rust", "frontend", "desktop_updater"} for k in values)):
            maps.append(values)
    # Do not accept one OS-only map while a different map reports a Linux failure.
    failed = [k for values in maps for k, value in values.items() if not value]
    return bool(failed) and all(k.startswith("tests-os/") or k == "desktop_updater" for k in failed)


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
