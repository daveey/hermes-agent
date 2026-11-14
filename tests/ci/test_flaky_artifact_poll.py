"""flaky_kanban.poll_github reads metadata artifacts: validation before side effects, fixed unavailable codes."""
from __future__ import annotations

import urllib.error
from pathlib import Path

import pytest

from scripts.ci import flaky_artifact as fa
from scripts.ci import flaky_artifact_poll as fap
from scripts.ci import flaky_kanban as fk
from tests.ci.flaky_artifact_fakes import SHA, FakeGitHub, artifact, job, make_zip, meta
from tests.ci.test_flaky_kanban import FakeBoard

SECRET = "SECRET-payload-77c1"
A, B = "tests/x.py::test_a", "tests/y.py"


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(fk, "_gh", fake.gh)
    monkeypatch.setattr(fa, "download_zip", fake.download)
    return fake


def _poll(board=None, state=None):
    board = board if board is not None else FakeBoard()
    state = state if state is not None else fk.load_state(Path("/nonexistent/state.json"))
    return fk.poll_github("o/r", "tok", board, state), board, state


def _two_slices(gh, bodies=None, run_id=5):
    gh.run(run_id)
    gh.jobs[(run_id, 1)] = [job(9, 1, 2), job(10, 2, 2)]
    gh.artifacts[run_id] = [artifact(100, run_id, 1, 1), artifact(101, run_id, 1, 2)]
    gh.zips[100], gh.zips[101] = bodies or (make_zip(meta(run_id, slices=2, tests=(A,))),
                                            make_zip(meta(run_id, slice_no=2, slices=2, tests=(B,))))


def test_artifacts_become_cards_per_job_with_safe_context(gh):
    _two_slices(gh)
    log, board, state = _poll()
    assert [line.split()[0] for line in log] == ["opened", "opened", "POLL"]
    assert log[-1] == "POLL complete=1 empty=0 unavailable=0 not_applicable=0"
    assert state["runs"] == ["5/1"]
    card = board.cards[state["cards"][A]["card"]]
    assert "https://github.com/o/r/actions/runs/5/job/9" in card["body"] and f"branch `{SHA}`" in card["body"]
    assert "First-attempt output" not in card["body"]
    assert "https://github.com/o/r/actions/runs/5/job/10" in board.cards[state["cards"][B]["card"]]["body"]
    assert "evil branch text" not in str(board.cards) and "example.invalid" not in str(board.cards)
    # Only metadata endpoints are read: never check-runs/annotations or logs.
    assert all("/actions/runs" in u for u in gh.urls) and not any(("check-runs" in u or "logs" in u) for u in gh.urls)
    assert sorted(gh.downloads) == [100, 101]


def test_a_processed_run_attempt_is_not_read_again(gh):
    gh.simple(tests=(A,))
    _, board, state = _poll()
    gh.urls.clear()
    gh.downloads.clear()
    log, _, _ = _poll(board, state)
    assert log == [] and gh.downloads == [] and len(board.cards) == 1
    assert not any("/artifacts" in u or "/jobs" in u for u in gh.urls)


def test_complete_empty_report_is_processed_as_no_flakes(gh):
    gh.simple()
    log, board, state = _poll()
    assert log == ["POLL complete=1 empty=1 unavailable=0 not_applicable=0"]
    assert state["runs"] == ["5/1"] and not board.cards


@pytest.mark.parametrize("jobs", [
    [],
    [{"id": 1, "name": "Lint", "status": "completed", "conclusion": "success"}],
    [job(9, 1, 2, conclusion="skipped"), job(10, 2, 2, conclusion="skipped")],
    [job(9, name="Python tests / Run tests (${{ matrix.slice }}/${{ strategy.job-total }})", conclusion="skipped")],
    [{"id": 3, "name": "Run e2e tests", "status": "completed", "conclusion": "success"}],
])
def test_no_python_test_jobs_is_not_applicable_and_distinct_from_empty(gh, jobs):
    gh.run()
    gh.jobs[(5, 1)] = jobs
    log, board, state = _poll()
    assert log == ["POLL complete=0 empty=0 unavailable=0 not_applicable=1"]
    assert state["runs"] == ["5/1"] and gh.downloads == [] and not board.cards


@pytest.mark.parametrize("jobs", [
    # Metadata-only projection of Actions run 43074641622's actual job shape.
    [{"id": 130711154736, "name": "CI review comment (live)",
      "status": "completed", "conclusion": "cancelled", "run_attempt": 1}],
    [],
])
def test_cancelled_non_test_run_is_not_applicable_and_deduped(gh, jobs):
    gh.run()
    gh.runs[0]["conclusion"] = "cancelled"
    gh.jobs[(5, 1)] = jobs
    log, board, state = _poll()
    assert log == ["POLL complete=0 empty=0 unavailable=0 not_applicable=1"]
    assert state["runs"] == ["5/1"] and not board.cards and not gh.downloads
    assert not any("/artifacts" in url for url in gh.urls)
    assert _poll(board, state)[0] == []


def test_affected_job_names_are_recognised(gh):
    gh.simple()
    gh.jobs[(5, 1)] = [job(9, name="Python tests / Run tests (affected, 1/1)")]
    log, _, state = _poll()
    assert state["runs"] == ["5/1"] and log[-1].startswith("POLL complete=1")


def _set_zip(gh, body):
    gh.zips[100] = body


_BREAKS = {
    "missing": lambda gh: gh.artifacts.update({5: []}),
    "expired": lambda gh: gh.artifacts.update({5: [artifact(100, expired=True)]}),
    "duplicate": lambda gh: (gh.artifacts.update({5: [artifact(100), artifact(102)]}), gh.zips.update({102: gh.zips[100]})),
    "other_attempt_only": lambda gh: gh.artifacts.update({5: [artifact(100, attempt=2)]}),
    "incomplete": lambda gh: _set_zip(gh, make_zip(meta(complete=False))),
    "wrong_run": lambda gh: _set_zip(gh, make_zip(meta(run_id=6))),
    "wrong_attempt": lambda gh: _set_zip(gh, make_zip(meta(attempt=2))),
    "wrong_head": lambda gh: _set_zip(gh, make_zip(meta(head_sha="c" * 40))),
    "wrong_repo": lambda gh: _set_zip(gh, make_zip(meta(repository="o/other"))),
    "wrong_slices": lambda gh: _set_zip(gh, make_zip(meta(slices=2))),
    "unknown_field": lambda gh: _set_zip(gh, make_zip(meta(extra=SECRET))),
    "not_zip": lambda gh: _set_zip(gh, b"nope"),
    "denied": lambda gh: _set_zip(gh, fa.ArtifactError("download_unavailable")),
    "run_without_sha": lambda gh: gh.runs[0].pop("head_sha"),
}


@pytest.mark.parametrize("how,code", [
    ("missing", "missing"), ("expired", "expired"), ("duplicate", "invalid"), ("other_attempt_only", "missing"),
    ("incomplete", "incomplete"), ("wrong_run", "invalid"), ("wrong_attempt", "invalid"), ("wrong_head", "invalid"),
    ("wrong_repo", "invalid"), ("wrong_slices", "invalid"), ("unknown_field", "invalid"), ("not_zip", "invalid"),
    ("denied", "download_unavailable"), ("run_without_sha", "invalid"),
])
def test_unavailable_data_is_a_fixed_failed_code_and_leaves_the_run_unprocessed(gh, how, code):
    gh.simple(tests=(A,))
    _BREAKS[how](gh)
    log, board, state = _poll()
    assert log == [f"WAITING run=5/1 code={code} polls=1", "POLL complete=0 empty=0 unavailable=1 not_applicable=0"]
    assert state["runs"] == [] and not board.cards
    assert SECRET not in "\n".join(log)


def test_unavailable_run_is_retried_next_poll_and_then_filed_once(gh):
    gh.simple(tests=(A,))
    good = gh.zips[100]
    gh.zips[100] = fa.ArtifactError("download_unavailable")
    log, board, state = _poll()
    assert log[0] == "WAITING run=5/1 code=download_unavailable polls=1" and state["runs"] == []
    gh.zips[100] = good
    log, board, state = _poll(board, state)
    assert log[0].startswith("opened") and state["runs"] == ["5/1"]
    assert _poll(board, state)[0] == [] and len(board.cards) == 1


def test_all_artifacts_are_validated_before_any_card_side_effect(gh):
    _two_slices(gh)
    gh.zips[101] = make_zip(meta(slice_no=2, slices=2, complete=False))
    log, board, state = _poll()
    assert log[0] == "WAITING run=5/1 code=incomplete polls=1" and not board.cards and state["cards"] == {}
    assert gh.downloads == [100, 101]                  # slice 1 was read and valid, yet nothing was filed


@pytest.mark.parametrize("jobs,code", [
    ([job(9, 1, 2)], "jobs_unavailable"),                                          # partial rerun: slice 2 absent
    ([job(9, 1, 2), job(10, 2, 2, conclusion="cancelled")], "jobs_unavailable"),
    ([job(9, 1, 2), job(10, 2, 2, status="in_progress", conclusion=None)], "jobs_unavailable"),
    ([job(9, 1, 2), job(10, 2, 2, conclusion="timed_out")], "jobs_unavailable"),
    ([job(9, 1, 2), job(10, 1, 2)], "jobs_unavailable"),                           # duplicate slice
    ([job(9, 1, 2), job(10, 2, 3)], "jobs_unavailable"),                           # inconsistent M
    ([job(9, 2, 2), job(10, 3, 3)], "jobs_unavailable"),
    ([job(9, name="Run tests (x/y)")], "jobs_unavailable"),                        # Run-tests-like, unrecognised
    ([job(9, name="Run tests (1/1) extra")], "jobs_unavailable"),
    ([job(9, name="Python tests / Run tests (${{ matrix.slice }})")], "jobs_unavailable"),   # unexpanded, not skipped
    ([job(9, 1, 65)], "jobs_unavailable"),
    ([job(9, name="Run tests (0/1)")], "jobs_unavailable"),
    ([job(9, name="Run tests (٣/٤)")], "jobs_unavailable"),                        # non-ASCII digits
    ([job(9, 1, 1), job(10, name="Run tests (garbage)")], "jobs_unavailable"),
    ([{"id": True, "name": "Run tests (1/1)", "status": "completed", "conclusion": "success"}], "jobs_unavailable"),
])
def test_incomplete_or_unrecognised_jobs_never_mean_no_flakes(gh, jobs, code):
    gh.simple()
    gh.jobs[(5, 1)] = jobs
    log, _board, state = _poll()
    assert log == [f"WAITING run=5/1 code={code} polls=1", "POLL complete=0 empty=0 unavailable=1 not_applicable=0"]
    assert state["runs"] == [] and gh.downloads == []


def test_a_failed_test_job_still_supplies_its_artifact(gh):
    gh.simple(tests=(A,))
    gh.jobs[(5, 1)] = [job(9, conclusion="failure")]
    log, _, state = _poll()
    assert log[0].startswith("opened") and state["runs"] == ["5/1"]


def test_skipped_slices_do_not_count_toward_the_expected_set(gh):
    gh.simple()
    gh.jobs[(5, 1)] = [job(9, 1, 1), job(10, 2, 2, conclusion="skipped")]
    assert _poll()[0] == ["POLL complete=1 empty=1 unavailable=0 not_applicable=0"]


def test_listing_failures_leave_that_run_retryable_and_other_runs_proceed(gh):
    gh.simple(run_id=5, tests=(A,))
    gh.simple(run_id=6, tests=(B,), art_id=200)
    gh.fail["runs/5/artifacts"] = urllib.error.URLError(SECRET)
    log, board, state = _poll()
    assert log[0] == "WAITING run=5/1 code=artifacts_unavailable polls=1"
    assert log[1].startswith("opened") and log[-1] == "POLL complete=1 empty=0 unavailable=1 not_applicable=0"
    assert state["runs"] == ["6/1"] and SECRET not in "\n".join(log)
    gh.fail.clear()
    gh.fail["runs/6/attempts/1/jobs"] = ValueError(SECRET)       # processed run 6 must not even be listed again
    log, board, state = _poll(board, state)
    assert log[0].startswith("opened") and state["runs"] == ["6/1", "5/1"]


def test_jobs_listing_failure_is_jobs_unavailable(gh):
    gh.simple()
    gh.fail["/attempts/1/jobs"] = TimeoutError(SECRET)
    log, _, state = _poll()
    assert log[0] == "WAITING run=5/1 code=jobs_unavailable polls=1" and state["runs"] == [] and SECRET not in str(log)


def test_completed_runs_listing_failure_changes_no_state(gh):
    gh.simple(tests=(A,))
    gh.fail["actions/runs?"] = urllib.error.URLError(SECRET)
    log, board, state = _poll()
    assert log == ["FAILED runs_unavailable"] and state["runs"] == [] and not board.cards and gh.downloads == []


def test_main_exits_one_once_an_unavailable_run_persists(gh, tmp_path, monkeypatch, capsys):
    gh.simple()
    gh.artifacts[5] = []
    monkeypatch.setenv("GH_TOKEN", "tok")
    monkeypatch.setattr(fk, "CliBoard", lambda *a, **k: FakeBoard())
    monkeypatch.setattr(fk, "HubRelay", lambda *a, **k: None)
    argv = ["--from-github", "--repo", "o/r", "--state", str(tmp_path / "s.json")]
    assert fk.main(["--initialize-intake", "--state", str(tmp_path / "s.json")]) == 0
    capsys.readouterr()
    assert fk.main(argv) == 0
    out = capsys.readouterr().out
    assert "WAITING run=5/1 code=missing polls=1" in out and "FAILED" not in out and "POLL complete=0" in out
    assert fk.main(argv) == 1
    assert "FAILED run=5/1 code=missing polls=2" in capsys.readouterr().out
    assert fk.load_state(tmp_path / "s.json")["runs"] == []


def test_jobs_and_artifacts_are_paged_past_one_hundred(gh):
    gh.run()
    filler = [job(1000 + i, name=f"Lint {i}") for i in range(130)]
    gh.jobs[(5, 1)] = filler + [job(9, 1, 2), job(10, 2, 2)]
    noise = [artifact(2000 + i, name=f"other-{i}") for i in range(130)]
    gh.artifacts[5] = noise + [artifact(100, slice_no=1), artifact(101, slice_no=2)]
    gh.zips[100], gh.zips[101] = make_zip(meta(slices=2, tests=(A,))), make_zip(meta(slice_no=2, slices=2))
    log, _, state = _poll()
    assert log[0].startswith("opened") and state["runs"] == ["5/1"]
    assert any("page=2" in u and "/jobs" in u for u in gh.urls) and any("page=2" in u and "/artifacts" in u for u in gh.urls)


def test_exactly_one_hundred_items_needs_the_next_page_to_end(gh):
    gh.simple()
    gh.jobs[(5, 1)] = [job(1000 + i, name=f"Lint {i}") for i in range(99)] + [job(9)]
    assert _poll()[0] == ["POLL complete=1 empty=1 unavailable=0 not_applicable=0"]
    assert any("/jobs" in u and "page=2" in u for u in gh.urls)


def test_page_overflow_is_rejected(gh, monkeypatch):
    gh.simple()
    monkeypatch.setattr(fap, "MAX_PAGES", 2)
    gh.artifacts[5] = [artifact(3000 + i, name=f"other-{i}") for i in range(250)] + [artifact(100)]
    log, _, state = _poll()
    assert log[0] == "WAITING run=5/1 code=artifacts_unavailable polls=1" and state["runs"] == []


def test_rerun_attempt_does_not_mix_in_earlier_attempt_artifacts(gh):
    gh.run(5, attempt=2)
    gh.jobs[(5, 2)] = [job(19)]
    gh.artifacts[5] = [artifact(100, attempt=1)]
    gh.zips[100] = make_zip(meta(attempt=1, tests=(A,)))
    log, board, state = _poll()
    assert log[0] == "WAITING run=5/2 code=missing polls=1" and not board.cards
    gh.artifacts[5].append(artifact(110, attempt=2))
    gh.zips[110] = make_zip(meta(attempt=2, tests=(B,)))
    log, board, state = _poll(board, state)
    assert state["runs"] == ["5/2"] and list(state["cards"]) == [B]


def test_hostile_api_text_and_exception_text_never_reach_cards_or_logs(gh):
    gh.simple(tests=(A,))
    gh.runs[0]["head_branch"] = f"{SECRET} branch"
    gh.jobs[(5, 1)][0]["html_url"] = f"https://example.invalid/{SECRET}"
    gh.artifacts[5][0]["archive_download_url"] = f"https://evil.invalid/{SECRET}"
    log, board, _ = _poll()
    assert SECRET not in "\n".join(log) + str(board.cards)
    gh.simple(run_id=6)
    gh.zips[100] = fa.ArtifactError("download_unavailable")
    log, _, _ = _poll()
    assert SECRET not in "\n".join(log)


def test_card_filing_failure_leaves_the_run_unprocessed_and_a_retry_does_not_duplicate(gh):
    gh.simple(tests=(A,))

    class FlakyBoard(FakeBoard):
        failing = True

        def comment(self, card, body, board=None):
            if self.failing:
                raise RuntimeError(SECRET)
            super().comment(card, body, board)

    board = FlakyBoard()
    board.add_foreign("t_open", "ready", f"Flaky test: `{A}`", title=f"Deflake {A}")
    log, board, state = _poll(board)
    assert log[0].startswith("FAILED") and log[-1] == "POLL complete=0 empty=0 unavailable=1 not_applicable=0"
    assert state["runs"] == [] and board.cards["t_open"]["comments"] == []
    board.failing = False
    log, board, state = _poll(board, state)
    assert log[0].startswith("appended") and state["runs"] == ["5/1"]
    assert len(board.cards["t_open"]["comments"]) == 1 and len(board.cards) == 1
    assert _poll(board, state)[0] == []


def test_assess_run_results_are_card_free(gh):
    """The assembly module returns entries only; it has no access to a board."""
    _two_slices(gh)
    result = fap.assess_run("o/r", gh.runs[0], lambda url: gh.gh(url, "tok"), "tok")
    assert result.kind == "complete" and [j for j, _ in result.jobs] == [9, 10]
    assert result.jobs[0][1] == [{"test_id": A, "retried": "tests", "first_attempt_excerpt": "",
                                  "run_attempt": 1, "source_sha": "b" * 40, "slice": 1, "slices": 2}]
    assert fap.Unavailable("missing").code == "missing"


@pytest.mark.parametrize("own", [1, 3, True, None, "2"])
def test_explicit_other_or_malformed_job_attempt_is_unavailable(gh, own):
    gh.run(5, attempt=2)
    gh.jobs[(5, 2)] = [{**job(19), "run_attempt": own}]
    log, board, state = _poll()
    assert log[0] == "WAITING run=5/2 code=jobs_unavailable polls=1"
    assert not state["runs"] and not board.cards and not gh.downloads


@pytest.mark.parametrize("jobs", [[job(9, conclusion="skipped")], [job(9)],
                                  [job(9, conclusion="cancelled")]])
def test_cancelled_test_run_never_means_no_flakes(gh, jobs):
    gh.run()["conclusion"] = "cancelled"
    gh.jobs[(5, 1)] = jobs
    log, board, state = _poll()
    assert log[0] == "WAITING run=5/1 code=jobs_unavailable polls=1"
    assert not state["runs"] and not board.cards


@pytest.mark.parametrize("total", [200, 201])
def test_run_flake_limit_is_aggregate_and_precedes_all_side_effects(gh, total):
    tests = tuple(f"tests/x.py::test_{i}" for i in range(total))
    _two_slices(gh, bodies=(make_zip(meta(slices=2, tests=tests[:100])),
                           make_zip(meta(slice_no=2, slices=2, tests=tests[100:]))))
    log, board, state = _poll()
    if total == 200:
        assert len(board.cards) == total and state["runs"] == ["5/1"]
    else:
        assert log[0] == "WAITING run=5/1 code=invalid polls=1"
        assert not board.cards and not state["runs"]
