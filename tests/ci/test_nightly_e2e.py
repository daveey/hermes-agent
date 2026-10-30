"""scripts/ci/nightly_e2e.py: dispatch the nightly e2e and report it on the board (card per failing-test set)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "ci"))
import nightly_e2e as ne

T0 = 1_800_000_000.0
SHA = "a" * 40


def _log(*ids: str) -> str:
    lines = ["2026-10-30T10:09:00.1Z   ║ FAILED " + i + " - live copy" for i in ids]  # must be ignored
    lines += [f"2026-10-30T10:20:07.5Z === Summary: 116 files, 780 tests passed, {len(ids)} failed ==="]
    lines += ["2026-10-30T10:20:07.6Z FAILED " + i + " - AssertionError: x" for i in ids]
    return "\n".join(lines)


class FakeGitHub:
    def __init__(self):
        self.runs: dict[int, dict] = {}
        self.logs: dict[int, str] = {}
        self.dispatched: list[tuple[str, str]] = []
        self.next_id = 100
        self.auto_create = True

    def resolve(self, ref):
        return SHA if ref == "main" else ref.ljust(40, "0")

    def dispatch(self, ref, sha):
        self.dispatched.append((ref, sha))
        if self.auto_create:
            self.make_run()

    def make_run(self, status="queued", conclusion=None, job_conclusion=None, log=""):
        rid = self.next_id
        self.next_id += 1
        self.runs[rid] = {"id": rid, "html_url": f"https://gh/run/{rid}", "status": status, "conclusion": conclusion,
                          "jobs": [{"id": rid * 10, "name": "resolve", "conclusion": "success"},
                                   {"id": rid * 10 + 1, "name": "e2e / e2e", "conclusion": job_conclusion}]}
        self.logs[rid * 10 + 1] = log
        return rid

    def finish(self, rid, job_conclusion, log=""):
        r = self.runs[rid]
        r.update(status="completed", conclusion=job_conclusion)
        r["jobs"][1]["conclusion"] = job_conclusion
        self.logs[rid * 10 + 1] = log

    def runs_since(self, since):
        return list(self.runs.values())

    def run(self, run_id):
        return self.runs[run_id]

    def jobs(self, run_id):
        return self.runs[run_id]["jobs"]

    def job_log(self, job_id):
        return self.logs[job_id]


class FakeBoard:
    def __init__(self):
        self.cards: dict[str, dict] = {}
        self.created: list[tuple] = []

    def create(self, title, body, key, *, distinct_from=None, distinct_reason=None):
        cid = f"t_{len(self.cards) + 1}"
        self.cards[cid] = {"status": "ready", "title": title, "body": body, "comments": [], "key": key}
        self.created.append((cid, key))
        return cid

    def status(self, cid):
        return self.cards[cid]["status"]

    def comment(self, cid, body, board=None):
        self.cards[cid]["comments"].append(body)


@pytest.fixture
def env():
    return FakeGitHub(), FakeBoard(), ne.load_state(Path("/nonexistent/state.json"))


def _dispatch(gh, state, ref="main", now=T0):
    return ne.dispatch(gh, state, ref, now=now, sleep=lambda s: None, lookup_seconds=0)


def test_failing_tests_are_sorted_unique_and_skip_live_copies():
    log = _log("tests/e2e/b.py::t[2]", "tests/e2e/a.py::t", "tests/e2e/b.py::t[2]")
    assert ne.failing_tests(log) == ["tests/e2e/a.py::t", "tests/e2e/b.py::t[2]"]


def test_failure_key_depends_only_on_the_set():
    assert ne.failure_key(["b", "a"]) == ne.failure_key(["a", "b"]) != ne.failure_key(["a"])


def test_dispatch_records_the_new_run(env):
    gh, _board, state = env
    entry = _dispatch(gh, state)
    assert gh.dispatched == [("main", SHA)]
    assert entry["run_id"] == 100 and entry["url"] == "https://gh/run/100" and entry["sha"] == SHA
    assert state["pending"] == [entry]


def test_second_dispatch_does_not_adopt_the_first_run(env):
    gh, _board, state = env
    first = _dispatch(gh, state)
    second = _dispatch(gh, state, now=T0 + 60)
    assert first["run_id"] != second["run_id"]


def test_green_run_files_nothing_and_clears_pending(env):
    gh, board, state = env
    _dispatch(gh, state)
    gh.finish(100, "success")
    lines = ne.report(gh, board, state, now=T0 + 3600)
    assert board.cards == {} and state["pending"] == [] and "green" in lines[0]


def test_failed_run_files_one_card_with_run_and_tests(env):
    gh, board, state = env
    _dispatch(gh, state, ref="29953696677a")
    gh.finish(100, "failure", _log("tests/e2e/z.py::t", "tests/e2e/a.py::t"))
    ne.report(gh, board, state, now=T0 + 3600)
    assert len(board.cards) == 1
    card = next(iter(board.cards.values()))
    assert "https://gh/run/100" in card["body"]
    assert card["body"].index("tests/e2e/a.py::t") < card["body"].index("tests/e2e/z.py::t")
    assert "29953696677a" in card["body"]


def test_second_failure_with_same_tests_updates_the_card(env):
    gh, board, state = env
    for i in range(2):
        _dispatch(gh, state, now=T0 + i * 86400)
        gh.finish(100 + i, "failure", _log("tests/e2e/a.py::t"))
        ne.report(gh, board, state, now=T0 + i * 86400 + 3600)
    assert len(board.cards) == 1
    assert "https://gh/run/101" in board.cards["t_1"]["comments"][0]


def test_different_failing_tests_get_their_own_card(env):
    gh, board, state = env
    _dispatch(gh, state)
    gh.finish(100, "failure", _log("tests/e2e/a.py::t"))
    ne.report(gh, board, state, now=T0 + 10)
    _dispatch(gh, state, now=T0 + 20)
    gh.finish(101, "failure", _log("tests/e2e/a.py::t", "tests/e2e/b.py::t"))
    ne.report(gh, board, state, now=T0 + 30)
    assert len(board.cards) == 2


def test_archived_card_is_replaced_not_commented(env):
    gh, board, state = env
    _dispatch(gh, state)
    gh.finish(100, "failure", _log("tests/e2e/a.py::t"))
    ne.report(gh, board, state, now=T0 + 10)
    board.cards["t_1"]["status"] = "archived"
    _dispatch(gh, state, now=T0 + 20)
    gh.finish(101, "failure", _log("tests/e2e/a.py::t"))
    ne.report(gh, board, state, now=T0 + 30)
    assert len(board.cards) == 2 and board.cards["t_1"]["comments"] == []
    assert board.created[0][1] != board.created[1][1]  # fresh idempotency key


def test_green_comments_on_open_failure_card_only(env):
    gh, board, state = env
    _dispatch(gh, state)
    gh.finish(100, "failure", _log("tests/e2e/a.py::t"))
    ne.report(gh, board, state, now=T0 + 10)
    _dispatch(gh, state, now=T0 + 20)
    gh.finish(101, "success")
    ne.report(gh, board, state, now=T0 + 30)
    assert "GREEN" in board.cards["t_1"]["comments"][0] and "https://gh/run/101" in board.cards["t_1"]["comments"][0]
    board.cards["t_1"]["status"] = "done"
    _dispatch(gh, state, now=T0 + 40)
    gh.finish(102, "success")
    ne.report(gh, board, state, now=T0 + 50)
    assert len(board.cards["t_1"]["comments"]) == 1


def test_run_still_going_within_deadline_stays_pending(env):
    gh, board, state = env
    _dispatch(gh, state)
    ne.report(gh, board, state, now=T0 + 3.9 * 3600)
    assert len(state["pending"]) == 1 and board.cards == {}


def test_run_not_finished_in_four_hours_files_did_not_run_card_once(env):
    gh, board, state = env
    _dispatch(gh, state)
    ne.report(gh, board, state, now=T0 + 4.1 * 3600)
    assert state["pending"] == [] and len(board.cards) == 1
    card = board.cards["t_1"]
    assert "did NOT run" in card["body"] and "https://gh/run/100" in card["body"]
    _dispatch(gh, state, now=T0 + 86400)
    ne.report(gh, board, state, now=T0 + 86400 + 4.1 * 3600)
    assert len(board.cards) == 1 and len(card["comments"]) == 1


def test_dispatch_that_never_creates_a_run_is_reported(env):
    gh, board, state = env
    gh.auto_create = False
    entry = _dispatch(gh, state)
    assert entry["run_id"] is None
    ne.report(gh, board, state, now=T0 + 600)
    assert len(state["pending"]) == 1  # still within the lookup window
    ne.report(gh, board, state, now=T0 + 31 * 60)
    assert state["pending"] == [] and "no workflow run was created" in board.cards["t_1"]["title"]


def test_cancelled_run_is_a_did_not_run_card(env):
    gh, board, state = env
    _dispatch(gh, state)
    gh.finish(100, "cancelled")
    ne.report(gh, board, state, now=T0 + 600)
    assert len(board.cards) == 1 and "did not run" in board.cards["t_1"]["title"].lower()


def test_failure_outside_pytest_still_files_a_card(env):
    gh, board, state = env
    _dispatch(gh, state)
    gh.finish(100, "failure", "setup exploded")
    ne.report(gh, board, state, now=T0 + 600)
    assert "outside pytest" in board.cards["t_1"]["body"]


def test_api_error_keeps_run_pending_then_gives_up(env):
    gh, board, state = env
    _dispatch(gh, state)
    gh.run = lambda rid: (_ for _ in ()).throw(RuntimeError("503"))
    ne.report(gh, board, state, now=T0 + 600)
    assert len(state["pending"]) == 1
    ne.report(gh, board, state, now=T0 + 7 * 3600)
    assert state["pending"] == []


def test_state_round_trip(tmp_path):
    p = tmp_path / "s" / "state.json"
    s = ne.load_state(p)
    s["cards"]["k"] = {"id": "t_1", "n": 1}
    ne.save_state(p, s)
    assert ne.load_state(p)["cards"]["k"] == {"id": "t_1", "n": 1}


# --- workflow wiring -------------------------------------------------------------------------------------------


def _wf(name):
    return YAML(typ="safe").load((ROOT / ".github" / "workflows" / name).read_text())


def test_nightly_workflow_is_dispatch_only_and_calls_the_gates_e2e_job():
    wf = _wf("nightly-e2e.yml")
    triggers = wf.get("on", wf.get(True))
    assert set(triggers) == {"workflow_dispatch"}, "no schedule/push: the hub cron dispatches it"
    assert triggers["workflow_dispatch"]["inputs"]["ref"]["default"] == "main"
    call = wf["jobs"]["e2e"]
    assert call["uses"] == "./.github/workflows/tests.yml"
    assert call["with"]["python"] is False and call["with"]["e2e"] is True and call["with"]["e2e_upgrade"] is False
    # the same definition the gate runs, not a copy
    gate = _wf("upstream-sync.yml")["jobs"]["full-suite"]
    assert gate["uses"] == call["uses"] and gate["with"]["e2e"] is True


def test_python_input_defaults_on_and_gates_only_the_python_lanes():
    tests = _wf("tests.yml")
    assert tests["on"]["workflow_call"]["inputs"]["python"]["default"] is True
    assert tests["jobs"]["test"]["if"] == "inputs.python"
    assert tests["jobs"]["termux-assembly-permissions"]["if"] == "inputs.python"
    assert tests["jobs"]["e2e"]["if"] == "inputs.e2e"
