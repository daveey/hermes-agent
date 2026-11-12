"""Ship review contract gate on kanban_complete. [daveey]

`hermes-core-wt ship --review-task` reads only metadata.verdict / reviewed_sha / reviewed_pr. Review workers completed
contract cards with `review_verdict`, `review_outcome` or `reviewed_head`; the card showed done and approved, ship
refused it later and a new review card was filed for the same head (PR1349, PR1222, PR1123). The completion is now
refused while the reviewer can still fix it.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_review_contract as krc

SHA = "2abb5479f9d9ddfe4ddab49df44d69e8867e8f25"
PR = "https://github.com/softmax-universe/daveey-hermes/pull/1349"
# The ship-filed review card's own body line (hermes-core-wt-land request_review_card).
SHIP_BODY = (f"Independent exact-head review of {PR} at **{SHA}**.\n\nFinish with kanban_complete metadata exactly:\n"
             f'  {{"verdict": "approve" | "request_changes", "reviewed_sha": "{SHA}", "reviewed_pr": "{PR}"}}\n')
# The real PR1349 review brief (t_01a1cf47771fda0d) asked for the wrong key itself; it still states the contract.
MISNAMED_BODY = (f"Do an independent exact-head review of PR {PR} at head **{SHA}**.\n"
                 f'Approve with kanban_complete metadata `{{"review_verdict": "approve", "reviewed_sha": "{SHA}", '
                 f'"reviewed_pr": "{PR}"}}`')
# PR1123's first brief named reviewed_head and no reviewed_sha (t_01a1ce2d6a3b3e75).
HEAD_BODY = ("Exact-head review of https://github.com/daveey/hermes/pull/1123 at 3ee406abf686.\n"
             "Complete with `reviewed_pr=https://github.com/daveey/hermes/pull/1123`, `reviewed_head=<sha>`, "
             "`verdict` approve or request_changes.")
GOOD = {"verdict": "approve", "reviewed_sha": SHA, "reviewed_pr": PR}


# --- contract detection --------------------------------------------------------------------------------------

@pytest.mark.parametrize("body", [SHIP_BODY, MISNAMED_BODY, HEAD_BODY,
                                  SHIP_BODY.replace("softmax-universe/daveey-hermes", "Daveey/Hermes")])
def test_states_contract(body):
    assert krc.states_contract(body)


@pytest.mark.parametrize("body", [
    None, "",
    # This card's own body: discusses the keys but names no core-fork pull request.
    "refuse kanban_complete unless the metadata block with `verdict` / `reviewed_sha` / `reviewed_pr` is exact",
    # Another repository's review keeps its own contract (babybear accepts APPROVE).
    SHIP_BODY.replace("softmax-universe/daveey-hermes", "softmax-universe/babybear"),
    SHIP_BODY.replace("daveey-hermes", "daveey-hermes-mods"),
    # A core PR without a reviewed head key, or without reviewed_pr, is not the ship contract.
    SHIP_BODY.replace("reviewed_sha", "head"),
    SHIP_BODY.replace("reviewed_pr", "pr"),
])
def test_does_not_state_contract(body):
    assert not krc.states_contract(body)


# --- metadata problems ---------------------------------------------------------------------------------------

def test_exact_metadata_has_no_problems():
    assert krc.problems(GOOD) == []
    assert krc.problems({**GOOD, "verdict": "request_changes"}) == []
    assert krc.problems({**GOOD, "reviewed_pr": "softmax-universe/daveey-hermes#1349"}) == []


@pytest.mark.parametrize("md,key,hint", [
    ({"review_verdict": "approve", "reviewed_sha": SHA, "reviewed_pr": PR}, "verdict", "review_verdict is not read"),
    ({"review_outcome": "approved", "reviewed_sha": SHA, "reviewed_pr": PR}, "verdict", "review_outcome is not read"),
    ({"approved": True, "reviewed_sha": SHA, "reviewed_pr": PR}, "verdict", "approved is not read"),
    ({"approval": "yes", "reviewed_sha": SHA, "reviewed_pr": PR}, "verdict", "approval is not read"),
    ({**GOOD, "verdict": "APPROVE"}, "verdict", "'APPROVE'"),
    ({**GOOD, "verdict": "changes_requested"}, "verdict", "'changes_requested'"),
    ({"verdict": "approve", "reviewed_head": SHA, "reviewed_pr": PR}, "reviewed_sha", "reviewed_head is not read"),
    ({**GOOD, "reviewed_sha": SHA[:10]}, "reviewed_sha", repr(SHA[:10])),
    ({**GOOD, "reviewed_sha": SHA + "0"}, "reviewed_sha", "40-hex lowercase"),
    ({**GOOD, "reviewed_sha": SHA.upper()}, "reviewed_sha", "40-hex lowercase"),
    ({**GOOD, "reviewed_sha": None}, "reviewed_sha", "None"),
    ({**GOOD, "reviewed_pr": 1349}, "reviewed_pr", "1349"),
    ({**GOOD, "reviewed_pr": "the PR"}, "reviewed_pr", "'the PR'"),
    ({**GOOD, "reviewed_pr": f" {PR}"}, "reviewed_pr", "must name the pull"),
    ({**GOOD, "reviewed_pr": f"{PR}/files"}, "reviewed_pr", "must name the pull"),
    ({k: v for k, v in GOOD.items() if k != "reviewed_pr"}, "reviewed_pr", "None"),
])
def test_problems_name_the_key(md, key, hint):
    found = krc.problems(md)
    assert len(found) == 1, found
    assert found[0].startswith(f"{key}: metadata.{key} is ")
    assert hint in found[0]


def test_no_alias_hint_without_alias():
    found = krc.problems({"reviewed_sha": SHA, "reviewed_pr": PR, "summary_verdict": "x"})
    assert found == ['verdict: metadata.verdict is None; it must be exactly "approve" or "request_changes"']
    found = krc.problems({"verdict": "approve", "reviewed_pr": PR})
    assert "is not read" not in found[0]


def test_non_dict_metadata_names_every_key():
    assert [p.split(":")[0] for p in krc.problems(None)] == ["verdict", "reviewed_sha", "reviewed_pr"]
    assert [p.split(":")[0] for p in krc.problems(["verdict"])] == ["verdict", "reviewed_sha", "reviewed_pr"]


# --- refusal composition -------------------------------------------------------------------------------------

def test_refusal_on_contract_card():
    msg = krc.refusal(MISNAMED_BODY, {"review_verdict": "approve", "reviewed_sha": SHA, "reviewed_pr": PR})
    assert msg and "metadata.verdict is None" in msg and "review_verdict is not read" in msg
    assert '"verdict": "approve" | "request_changes"' in msg and "Nothing changed" in msg


def test_refusal_none_for_exact_metadata():
    assert krc.refusal(SHIP_BODY, GOOD) is None


def test_refusal_none_without_contract():
    assert krc.refusal("Fix the sidebar. Done when tests pass.", {"review_verdict": "approve"}) is None


@pytest.mark.parametrize("md", [
    None, {}, {"changed_files": ["a.py"], "tests_run": 3},  # a card that only discusses the contract
    {"published_pr": PR, "reviewed_head": SHA, "landed_sha": SHA},  # the landing handoff of an implementation card
])
def test_refusal_none_when_not_a_review_result(md):
    assert krc.refusal(SHIP_BODY, md) is None


@pytest.mark.parametrize("key", ["verdict", "review_verdict", "review_outcome", "reviewed_sha", "reviewed_head",
                                 "reviewed_pr", "approved", "approval"])
def test_each_review_key_makes_a_review_result(key):
    assert krc.is_review_result({key: "x"})
    assert krc.refusal(SHIP_BODY, {key: "x"})


# --- through the kanban_complete tool ------------------------------------------------------------------------

@pytest.fixture
def board(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    for var in ("HERMES_SESSION_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return kb


def _claim(kb, monkeypatch, body):
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Independent exact-head review PR1349", body=body, assignee="test-worker")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid


def _complete(metadata):
    from tools import kanban_tools as kt
    return json.loads(kt._handle_complete({"summary": "APPROVE: reviewed at the exact head", "metadata": metadata}))


def _status(kb, tid):
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        return kb.get_task(conn, tid).status


@pytest.mark.parametrize("bad,key", [
    ({"review_verdict": "approve", "reviewed_sha": SHA, "reviewed_pr": PR}, "metadata.verdict"),
    ({"verdict": "approve", "reviewed_pr": PR}, "metadata.reviewed_sha"),
    ({"verdict": "approve", "reviewed_head": SHA, "reviewed_pr": PR}, "metadata.reviewed_sha"),
])
def test_tool_refuses_unreadable_review_then_accepts_fix(board, monkeypatch, bad, key):
    from tools import kanban_tools as kt
    tid = _claim(board, monkeypatch, MISNAMED_BODY)
    out = _complete(bad)
    assert out.get("error") and out["rejection"] == "review_contract"
    assert out["rejection"] in kt.COMPLETE_REJECTION_CLASSES
    assert key in out["error"]
    assert _status(board, tid) == "running"  # nothing changed: the same run retries
    ok = _complete(dict(GOOD))
    assert ok.get("ok") is not False and not ok.get("error"), ok
    assert _status(board, tid) == "done"


def test_tool_accepts_exact_review(board, monkeypatch):
    tid = _claim(board, monkeypatch, SHIP_BODY)
    out = _complete(dict(GOOD))
    assert not out.get("error"), out
    assert _status(board, tid) == "done"


def test_tool_leaves_cards_without_contract_alone(board, monkeypatch):
    tid = _claim(board, monkeypatch, "Fix the sidebar scroll. Done when its tests pass.")
    out = _complete({"review_verdict": "approve", "reviewed_head": "abc"})
    assert not out.get("error"), out
    assert _status(board, tid) == "done"
