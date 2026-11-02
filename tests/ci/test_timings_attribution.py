"""The CI-timings review item blames a wall-time regression on a PR only when its own tests explain it.

Regression (2026-11-01): PRs #546, #548 and #552 each got "⚠️ Warnings / CI timings" for a +34..44% wall time
vs main, while the tests they added took 9.0 s, 12.7 s and 18.9 s and the per-shard deltas went both ways
(#546: 3/8 +183 s, 2/8 -110 s). The slowdown was CI-wide; the warning sat unanswered and the PRs merged with it.

Rule: a wall regression above the threshold is a Warning only when the PR's added/changed test files took
>= 60 s in this run, or one added test case took >= 10 s; otherwise it is Info, worded as not attributable.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "timings_report.py"
_spec = importlib.util.spec_from_file_location("timings_report_attr", _PATH)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_T0 = datetime(2025, 1, 1, tzinfo=UTC)

NEW = "tests/hermes_cli/test_kanban_ask_recheck_sync.py"
CHANGED = "tests/hermes_cli/test_kanban.py"


def _ts(seconds: float) -> str:
    return datetime.fromtimestamp(_T0.timestamp() + seconds, tz=UTC).isoformat().replace("+00:00", "Z")


def _job(name: str, dur_s: float) -> dict:
    return {"name": name, "duration_s": dur_s, "conclusion": "success", "started_at": _ts(0),
            "completed_at": _ts(dur_s), "wait_s": 0.0}


def _slow_ci(files: dict[str, float], slow_cases: dict[str, float] | None = None,
             pr_files: list[str] | None = None, added: dict[str, list[str]] | None = None) -> tuple[dict, dict]:
    """A run whose wall time is +39.5% vs main (25m30s vs 18m17s, as on #546), with fixture test timings."""
    cur = {"run_id": "1", "head_sha": "abc", "created_at": "", "jobs": [_job("Run tests (3/8)", 1530.0)],
           "test_durations": {"files": files, "slow_cases": slow_cases or {}},
           "pr_changes": {"number": 546, "test_files": pr_files if pr_files is not None else [NEW],
                          "added_cases": added if added is not None else {NEW: ["test_home_rechecks"]}}}
    base = {"run_id": "0", "head_sha": "def", "created_at": "", "jobs": [_job("Run tests (3/8)", 1097.0)]}
    return cur, base


def _result(statuses: list[dict]) -> dict:
    timing = [s for s in statuses if s["source"] == "ci timing"]
    assert len(timing) == 1 and len(timing[0]["results"]) == 1
    return timing[0]["results"][0]


def test_ci_wide_slowdown_with_small_pr_files_is_info():
    cur, base = _slow_ci({NEW: 9.0, "tests/gateway/test_session_tui_ops.py": 97.7})
    result = _result(_mod.generate_review_status(cur, base))
    assert result["kind"] == "info", result
    assert result["summary"] == (
        "CI wall time +39.5% vs main; not attributable to this PR (its added/changed test files took 9.0 s)"
    )
    assert f"{NEW}: 9.0 s" in result["detail"]


def test_pr_files_totalling_60s_or_more_warn_and_name_the_files():
    cur, base = _slow_ci({NEW: 41.0, CHANGED: 22.5, "tests/other/test_x.py": 300.0}, pr_files=[NEW, CHANGED])
    result = _result(_mod.generate_review_status(cur, base))
    assert result["kind"] == "warning", result
    assert "+39.5%" in result["summary"]
    assert "63.5 s" in result["summary"]
    assert f"{NEW} 41.0 s" in result["summary"]
    assert f"{CHANGED} 22.5 s" in result["summary"]
    assert "tests/other/test_x.py" not in result["summary"]


def test_one_added_case_of_10s_or_more_warns_and_names_it():
    cur, base = _slow_ci({NEW: 14.0}, slow_cases={f"{NEW}::TestRecheck::test_home_rechecks[sync]": 11.2})
    result = _result(_mod.generate_review_status(cur, base))
    assert result["kind"] == "warning", result
    assert f"{NEW}::TestRecheck::test_home_rechecks[sync] 11.2 s" in result["summary"]
    assert f"{NEW} 14.0 s" in result["summary"]


def test_slow_case_that_the_pr_did_not_add_stays_info():
    cur, base = _slow_ci({CHANGED: 30.0}, slow_cases={f"{CHANGED}::test_old_slow": 25.0}, pr_files=[CHANGED],
                         added={CHANGED: ["test_new_fast"]})
    result = _result(_mod.generate_review_status(cur, base))
    assert result["kind"] == "info", result
    assert "took 30.0 s" in result["summary"]


def test_without_pr_data_the_regression_still_warns():
    cur, base = _slow_ci({NEW: 9.0})
    cur.pop("pr_changes")
    result = _result(_mod.generate_review_status(cur, base))
    assert result["kind"] == "warning"
    assert "Wall time 25m30s vs 18m17s (+39.5%)" in result["summary"]


def test_review_status_only_reads_pr_attribution_from_the_saved_json(tmp_path, monkeypatch):
    cur, base = _slow_ci({NEW: 9.0})
    (tmp_path / "t.json").write_text(json.dumps(cur))
    (tmp_path / "b.json").write_text(json.dumps(base))
    out = tmp_path / "rs.txt"
    monkeypatch.setattr("sys.argv", ["timings_report.py", "--from-json", str(tmp_path / "t.json"), "--baseline",
                                     str(tmp_path / "b.json"), "--review-status-out", str(out),
                                     "--json-out", str(tmp_path / "saved.json"),
                                     "--review-status-only"])
    monkeypatch.setattr(_mod, "collect_pr_changes", lambda *a, **k: (_ for _ in ()).throw(AssertionError("api")))
    _mod.main()
    statuses = json.loads(out.read_text().split("=", 1)[1])
    assert _result(statuses)["kind"] == "info"


# A real excerpt of a "Run tests (3/8)" job log (run 41344034622) plus the runner's slow-case line.
_LOG = """\
2026-11-01T20:05:21.1556398Z [  0.0% |     2/~8000 | ✓3 | ✗0] ✓ tests/acp_adapter/test_acp_discard_review.py (3✓, 1.8s)
2026-11-01T20:05:21.2250867Z [  0.1% |     4/~8000 | ✓9 | ✗0] ✓ tests/acp_adapter/test_gateway_catalog_cwd.py (1✓, 1.9s)
2026-11-01T20:06:02.0000000Z [  2.0% |   160/~8000 | ✓99 | ✗1] ✗ tests/hermes_cli/test_kanban.py (40✓ 1✗, 120.5s)
2026-11-01T20:06:02.0000001Z   ⏱ slow test case 12.40s tests/hermes_cli/test_kanban.py::TestX::test_big[a]
2026-11-01T20:06:03.0000000Z [  2.1% |   161/~8000 | ✓99 | ✗1] ✓ tests/tools/test_empty.py (2.0s)
2026-11-01T20:19:11.4504416Z     108.88s  tests/tui_gateway/test_tui_gateway_server.py
"""


def test_parse_test_durations_reads_progress_and_slow_case_lines():
    files, slow = _mod.parse_test_durations(_LOG)
    assert files == {
        "tests/acp_adapter/test_acp_discard_review.py": 1.8,
        "tests/acp_adapter/test_gateway_catalog_cwd.py": 1.9,
        "tests/hermes_cli/test_kanban.py": 120.5,
        "tests/tools/test_empty.py": 2.0,
    }
    assert slow == {"tests/hermes_cli/test_kanban.py::TestX::test_big[a]": 12.4}


def test_collect_pr_changes_lists_test_files_and_added_cases(monkeypatch, tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 546}}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    files = [
        {"filename": NEW, "status": "added",
         "patch": "@@ -0,0 +1,9 @@\n+import pytest\n+\n+class TestRecheck:\n+    def test_home_rechecks(self):\n"
                  "+        pass\n+\n+async def test_async_case():\n+    pass\n"},
        {"filename": CHANGED, "status": "modified",
         "patch": "@@ -10,3 +10,4 @@\n def test_old():\n-    x = 1\n+    x = 2\n+def test_new_fast():\n"},
        {"filename": "tests/hermes_cli/test_gone.py", "status": "removed", "patch": "-def test_x():\n"},
        {"filename": "hermes_cli/kanban.py", "status": "modified", "patch": "+def test_like_name():\n"},
        {"filename": "tests/conftest.py", "status": "modified", "patch": "+def fixture():\n"},
    ]
    seen = []

    def fake_api_get(path, token, params=None, list_key=None):
        seen.append(path)
        return files

    monkeypatch.setattr(_mod, "api_get", fake_api_get)
    pr = _mod.collect_pr_changes("tok", "daveey/hermes")
    assert seen == ["/repos/daveey/hermes/pulls/546/files"]
    assert pr == {"number": 546, "test_files": [NEW, CHANGED],
                  "added_cases": {NEW: ["test_home_rechecks", "test_async_case"], CHANGED: ["test_new_fast"]}}


def test_collect_pr_changes_outside_a_pull_request_is_none(monkeypatch, tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"ref": "refs/heads/main"}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setattr(_mod, "api_get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no API call")))
    assert _mod.collect_pr_changes("tok", "daveey/hermes") is None


# Regression (2026-11-01, PR #575's own bot comment): the test-slice jobs come back from the API as
# "Python tests / Run tests (8/8)" (a reusable-workflow job in the caller run, empty workflow_name), so a
# `startswith("Run tests (")` check skipped every slice and the item said the PR's files took "0.0 s (not run)".
def test_collect_reads_durations_from_slice_jobs_named_with_the_caller_prefix(monkeypatch):
    jobs = [{"job_id": 7, "name": "Python tests / Run tests (8/8)", "workflow_name": "", "conclusion": "success",
             "steps": [], "html_url": "u"},
            {"job_id": 8, "name": "Run tests (1/8)", "workflow_name": "Python tests", "conclusion": "success",
             "steps": [], "html_url": "u"},
            {"job_id": 9, "name": "Python lints / ruff", "workflow_name": "", "conclusion": "success",
             "steps": [], "html_url": "u"}]
    logs = {7: _LOG, 8: "2026-11-01T20:05:21Z [ 1% | 1/~9 | ✓1 | ✗0] ✓ tests/ci/test_new.py (9✓, 1.3s)\n",
            9: "2026-11-01T20:05:21Z ✓ tests/lint_only.py (1✓, 5.0s)\n"}
    monkeypatch.setattr(_mod, "_job_log", lambda job_id, repo, token: logs[job_id])
    monkeypatch.setattr(_mod, "parse_cache_log", lambda log, steps: [])
    durations: dict = {"files": {}, "slow_cases": {}}
    _mod._collect_cache_data(jobs, "o/r", "t", durations)
    assert durations["files"]["tests/hermes_cli/test_kanban.py"] == 120.5
    assert durations["files"]["tests/ci/test_new.py"] == 1.3
    assert "tests/lint_only.py" not in durations["files"]
    assert durations["slow_cases"] == {"tests/hermes_cli/test_kanban.py::TestX::test_big[a]": 12.4}


def test_no_per_file_durations_at_all_is_unknown_not_zero_seconds():
    # Durations missing for the whole run means collection broke, not that the PR's tests took 0 s:
    # the regression must not be waved through as Info.
    cur, base = _slow_ci({})
    result = _result(_mod.generate_review_status(cur, base))
    assert result["kind"] == "warning", result
    assert "0.0 s" not in result["summary"]
