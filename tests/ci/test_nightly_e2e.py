"""scripts/ci/nightly_e2e.py: dispatch the nightly e2e and report it on the board (card per failing-test set)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "ci"))
import nightly_e2e as ne

T0 = 1_800_000_000.0
SHA = "a" * 40


def _log(*ids: str) -> str:
    lines = [
        "2026-10-30T10:09:00.1Z   ║ FAILED " + i + " - live copy" for i in ids
    ]  # must be ignored
    lines += [
        f"2026-10-30T10:20:07.5Z === Summary: 116 files, 780 tests passed, {len(ids)} failed ==="
    ]
    lines += [
        "2026-10-30T10:20:07.6Z FAILED " + i + " - AssertionError: x" for i in ids
    ]
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
        self.runs[rid] = {
            "id": rid,
            "html_url": f"https://gh/run/{rid}",
            "status": status,
            "conclusion": conclusion,
            "jobs": [
                {"id": rid * 10, "name": "resolve", "conclusion": "success"},
                {"id": rid * 10 + 1, "name": "e2e / e2e", "conclusion": job_conclusion},
            ],
        }
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
        self.cards[cid] = {
            "status": "ready",
            "title": title,
            "body": body,
            "comments": [],
            "key": key,
        }
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


def test_failing_tests_include_pytest_error_summary_ids():
    log = "\n".join(
        [
            _log("tests/e2e/a.py::t"),
            "2026-10-30T10:09:00.1Z   ║ ERROR tests/e2e/live.py::t - live copy",
            "2026-10-30T10:20:07.7Z ERROR tests/e2e/c.py::t - fixture 'tmux' not found",
            "2026-10-30T10:20:07.8Z ERROR tests/e2e/broken.py - ImportError: x",
        ]
    )
    assert ne.failing_tests(log) == [
        "tests/e2e/a.py::t",
        "tests/e2e/broken.py",
        "tests/e2e/c.py::t",
    ]


def test_failure_key_depends_only_on_the_set():
    assert (
        ne.failure_key(["b", "a"])
        == ne.failure_key(["a", "b"])
        != ne.failure_key(["a"])
    )


def test_dispatch_records_the_new_run(env):
    gh, _board, state = env
    entry = _dispatch(gh, state)
    assert gh.dispatched == [("main", SHA)]
    assert (
        entry["run_id"] == 100
        and entry["url"] == "https://gh/run/100"
        and entry["sha"] == SHA
    )
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
    assert card["body"].index("tests/e2e/a.py::t") < card["body"].index(
        "tests/e2e/z.py::t"
    )
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
    assert (
        "GREEN" in board.cards["t_1"]["comments"][0]
        and "https://gh/run/101" in board.cards["t_1"]["comments"][0]
    )
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
    assert (
        state["pending"] == []
        and "no workflow run was created" in board.cards["t_1"]["title"]
    )


def test_cancelled_run_is_a_did_not_run_card(env):
    gh, board, state = env
    _dispatch(gh, state)
    gh.finish(100, "cancelled")
    ne.report(gh, board, state, now=T0 + 600)
    assert (
        len(board.cards) == 1 and "did not run" in board.cards["t_1"]["title"].lower()
    )


def test_failure_outside_pytest_still_files_a_card(env):
    gh, board, state = env
    _dispatch(gh, state)
    gh.finish(100, "failure", "setup exploded")
    ne.report(gh, board, state, now=T0 + 600)
    assert "outside pytest" in board.cards["t_1"]["body"]


def _raise(err):
    def fn(*_a, **_k):
        raise err

    return fn


@pytest.mark.parametrize("failing", ["run", "job_log"])
def test_persistent_github_error_files_did_not_run_card_past_deadline(env, failing):
    gh, board, state = env
    _dispatch(gh, state)
    gh.finish(100, "failure", _log("tests/e2e/a.py::t"))
    setattr(gh, failing, _raise(OSError("HTTP 503")))
    lines = ne.report(gh, board, state, now=T0 + 3.9 * 3600)
    assert len(state["pending"]) == 1 and board.cards == {} and "retrying" in lines[0]
    lines = ne.report(gh, board, state, now=T0 + 4.1 * 3600)
    assert state["pending"] == [] and len(board.cards) == 1
    card = board.cards["t_1"]
    assert "did NOT run" in card["body"] and "HTTP 503" in card["body"]
    assert "https://gh/run/100" in card["body"] and "did-not-run after errors" in lines[0]
    # the next night's persistent error comments on the same open card
    _dispatch(gh, state, now=T0 + 86400)
    gh.finish(101, "failure", _log("tests/e2e/a.py::t"))
    ne.report(gh, board, state, now=T0 + 86400 + 4.1 * 3600)
    assert len(board.cards) == 1 and "HTTP 503" in card["comments"][0]


class BrokenBoard(FakeBoard):
    create = status = comment = _raise(RuntimeError("hermes kanban create failed (1): db locked"))


def test_board_failure_past_deadline_keeps_run_pending_and_says_so(env):
    gh, _board, state = env
    _dispatch(gh, state)
    gh.run = _raise(OSError("HTTP 503"))
    lines = ne.report(gh, BrokenBoard(), state, now=T0 + 5 * 3600)
    assert len(state["pending"]) == 1
    assert ne.BOARD_FAILED in lines[0] and "HTTP 503" in lines[0] and "db locked" in lines[0]
    board = FakeBoard()  # the board recovers: the next tick files the card
    ne.report(gh, board, state, now=T0 + 5 * 3600 + 600)
    assert state["pending"] == [] and len(board.cards) == 1


def _seed(path, gh, dispatched_at):
    with ne.locked_state(path) as state:
        _dispatch(gh, state, now=dispatched_at)


@pytest.mark.platforms("posix")
def test_main_report_exits_1_and_keeps_pending_when_board_fails(tmp_path, monkeypatch, capsys):
    gh, path = FakeGitHub(), tmp_path / "state.json"
    _seed(path, gh, time.time() - 5 * 3600)
    gh.run = _raise(OSError("HTTP 503"))
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setattr(ne, "RestGitHub", lambda *a: gh)
    monkeypatch.setattr(ne, "NightlyBoard", lambda *a: BrokenBoard())
    assert ne.main(["report", "--state", str(path)]) == 1
    assert ne.BOARD_FAILED in capsys.readouterr().out
    assert [e["run_id"] for e in ne.load_state(path)["pending"]] == [100]


# A real process per command: report loads pending, then (inside GitHub calls) a dispatch runs and saves.
_CHILD = r"""
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import nightly_e2e as ne
mode, state, signal = sys.argv[2], sys.argv[3], Path(sys.argv[4])

class GH:
    def __init__(self, *a): pass
    def resolve(self, ref): return "b" * 40
    def dispatch(self, ref, sha): pass
    def runs_since(self, since): return [{"id": 5000, "html_url": "https://gh/run/5000"}]
    def run(self, run_id):
        signal.touch()  # report holds the pending list it loaded; the racing dispatch starts now
        time.sleep(1.5)
        return {"status": "completed", "conclusion": "success"}
    def jobs(self, run_id): return [{"id": 1, "name": "e2e / e2e", "conclusion": "success"}]

ne.RestGitHub, ne.NightlyBoard = GH, lambda *a: None
sys.exit(ne.main([mode, "--state", state]))
"""


@pytest.mark.platforms("posix")
def test_concurrent_report_and_dispatch_processes_do_not_lose_the_dispatch(tmp_path):
    path, signal = tmp_path / "state.json", tmp_path / "report-inside-github"
    _seed(path, FakeGitHub(), time.time() - 600)  # run 100, pending; the report settles it green
    child = tmp_path / "child.py"
    child.write_text(_CHILD, encoding="utf-8")
    env = {**os.environ, "GH_TOKEN": "x"}

    def start(mode):
        argv = [sys.executable, str(child), str(ROOT / "scripts" / "ci"), mode, str(path), str(signal)]
        return subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    report = start("report")
    deadline = time.monotonic() + 30
    while not signal.exists():
        assert report.poll() is None and time.monotonic() < deadline, report.communicate()[0]
        time.sleep(0.02)
    dispatch = start("dispatch")
    outs = [p.communicate(timeout=60)[0] for p in (report, dispatch)]
    assert [report.returncode, dispatch.returncode] == [0, 0], outs
    state = ne.load_state(path)
    # unserialized, the report saves the pending list it loaded before the dispatch: run 5000 is wiped
    assert [e["run_id"] for e in state["pending"]] == [5000], outs
    assert set(state["seen"]) == {100, 5000}


def test_state_round_trip(tmp_path):
    p = tmp_path / "s" / "state.json"
    s = ne.load_state(p)
    s["cards"]["k"] = {"id": "t_1", "n": 1}
    ne.save_state(p, s)
    assert ne.load_state(p)["cards"]["k"] == {"id": "t_1", "n": 1}


# --- workflow wiring -------------------------------------------------------------------------------------------


def _wf(name):
    return YAML(typ="safe").load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8-sig"))


def test_nightly_workflow_is_dispatch_only_and_calls_the_gates_e2e_job():
    wf = _wf("nightly-e2e.yml")
    triggers = wf.get("on", wf.get(True))
    assert set(triggers) == {"workflow_dispatch"}, (
        "no schedule/push: the hub cron dispatches it"
    )
    assert triggers["workflow_dispatch"]["inputs"]["ref"]["default"] == "main"
    call = wf["jobs"]["e2e"]
    assert call["uses"] == "./.github/workflows/tests.yml"
    assert (
        call["with"]["python"] is False
        and call["with"]["e2e"] is True
        and call["with"]["e2e_upgrade"] is False
    )
    # the same definition the gate runs, not a copy
    gate = _wf("upstream-sync.yml")["jobs"]["full-suite"]
    assert gate["uses"] == call["uses"] and gate["with"]["e2e"] is True


def test_python_input_defaults_on_and_gates_only_the_python_lanes():
    tests = _wf("tests.yml")
    assert tests["on"]["workflow_call"]["inputs"]["python"]["default"] is True
    assert tests["jobs"]["test"]["if"] == "inputs.python"
    assert tests["jobs"]["termux-assembly-permissions"]["if"] == "inputs.python"
    assert tests["jobs"]["e2e"]["if"] == "inputs.e2e"
