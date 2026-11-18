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
# A full review of a pull outside the core fork: no ship review, whoever completes it.
NOT_CORE_GOOD = {**GOOD, "reviewed_pr": "https://github.com/daveey/babybear/pull/3730"}


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
    # A core PR without a reviewed head key, or without reviewed_pr, does not spell out the ship contract. Since
    # PR1462's `pr`-keyed brief, refusal() still gates such a body when the completion is a review result (it names a
    # core-fork pull); the wording alone never makes a non-review completion refusable.
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
    assert krc.refusal("Investigate the review contract: " + SHIP_BODY, md) is None


@pytest.mark.parametrize("md", [None, {}, {"completion_receipt": {}}])
def test_ship_brief_requires_metadata_even_without_review_keys(md):
    result = krc.completion_refusal(SHIP_BODY, md, worker_run_id=1)
    assert result and result[1] == "review_contract"
    assert "metadata.verdict" in result[0]
    assert "metadata.reviewed_sha" in result[0]


def test_review_brief_landing_handoff_remains_exempt():
    # A real review-and-land card retained the original review brief after landing.
    md = {"published_pr": PR, "merged_sha": SHA, "approved_head": SHA, "review_id": 123}
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
    for var in ("HERMES_SESSION_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_WORKER_TASK"):
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
    ({"completion_receipt": {"version": 1, "items": [{"scope": "review", "disposition": "delivered",
       "evidence": [{"ref": "review evidence", "observed_at": "2026-11-18T00:00:00+00:00",
                     "verification": "reported"}], "remaining": []}]}}, "metadata.verdict"),
    ({}, "metadata.verdict"),
    ({"review_verdict": "approve", "reviewed_sha": SHA, "reviewed_pr": PR}, "metadata.verdict"),
    ({"verdict": "approve", "reviewed_pr": PR}, "metadata.reviewed_sha"),
    ({"verdict": "approve", "reviewed_head": SHA, "reviewed_pr": PR}, "metadata.reviewed_sha"),
])
def test_tool_refuses_unreadable_review_then_accepts_fix(board, monkeypatch, bad, key):
    from tools import kanban_tools as kt
    tid = _claim(board, monkeypatch, SHIP_BODY)
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


# --- a core-fork review whose brief asked for the wrong key -------------------------------------------------
# PR1462's land-lane review brief (t_01a1d004070d3361, by t_01a1cfa4fc605bbd) asked for `pr`, not `reviewed_pr`.
# The reviewer wrote what it was asked for and ship refused the approval. The gate keys on the core-fork pull the
# body names, not on the body spelling the contract keys out.
PR_KEY_PULL = "https://github.com/softmax-universe/daveey-hermes/pull/1462"
PR_KEY_BODY = ("Complete with `kanban_complete`, `metadata` holding `verdict` (`approve` or `request_changes`), "
               "`reviewed_sha` (the 40-hex sha above, only if reviewed), `pr` (the URL) and `findings`.\n"
               f"{PR_KEY_PULL}\n")
PR_KEY_MD = {"verdict": "approve", "reviewed_sha": SHA, "pr": PR_KEY_PULL, "findings": ["none blocking"]}


def test_body_asking_for_pr_key_is_still_gated():  # T1: fails on 2e870e8f71
    msg = krc.refusal(PR_KEY_BODY, dict(PR_KEY_MD))
    assert msg and "reviewed_pr: metadata.reviewed_pr is None" in msg
    assert [p.split(":")[0] for p in krc.problems(PR_KEY_MD)] == ["reviewed_pr"]


def test_refusal_names_its_cause_on_each_path():
    # Contract path: the body spells the keys out.
    msg = krc.refusal(SHIP_BODY, {"review_verdict": "approve", "reviewed_sha": SHA, "reviewed_pr": PR}) or ""
    assert msg.startswith("kanban_complete refused: this card states the ship review contract, ")
    assert "core-fork pull" not in msg and "metadata.verdict" in msg
    # names_core_pull path: the body asked for `pr`, so it never stated the contract; name the pull instead.
    msg = krc.refusal(PR_KEY_BODY, dict(PR_KEY_MD)) or ""
    assert msg.startswith(f"kanban_complete refused: this card reviews a core-fork pull ({PR_KEY_PULL}), ")
    assert "states the ship review contract" not in msg and "metadata.reviewed_pr" in msg


def test_core_pull_is_the_first_core_fork_pull():
    other = "https://github.com/daveey/babybear/pull/3730"
    body = f"see {other} and github.com/Daveey/Hermes/pull/7, then {PR_KEY_PULL}"
    assert krc.core_pull(body) == "https://github.com/Daveey/Hermes/pull/7"
    assert krc.core_pull(f"see {other}") is None
    assert krc.core_pull(None) is None


def test_body_asking_for_pr_key_accepts_reviewed_pr():  # T2
    assert krc.refusal(PR_KEY_BODY, {**PR_KEY_MD, "reviewed_pr": PR_KEY_PULL}) is None


@pytest.mark.parametrize("body", [  # T3: the same review on a card that names no core-fork pull
    PR_KEY_BODY.replace(PR_KEY_PULL, "https://github.com/daveey/babybear/pull/3730"),
    PR_KEY_BODY.replace(PR_KEY_PULL, "https://github.com/softmax-universe/daveey-hermes-mods/pull/12"),
    PR_KEY_BODY.replace(PR_KEY_PULL, "PR #1462"),  # a bare number names no pull
])
def test_body_without_core_pull_is_not_gated(body):
    assert krc.refusal(body, dict(PR_KEY_MD)) is None


def test_landing_handoff_on_core_pull_card_is_not_gated():  # T4
    assert krc.refusal(PR_KEY_BODY, {"published_pr": PR_KEY_PULL, "verdict": "approve", "pr": PR_KEY_PULL}) is None


@pytest.mark.parametrize("md", [{"pr": PR_KEY_PULL, "findings": ["x"]}, {"changed_files": ["a.py"]}, {}, None])
def test_no_review_keys_on_core_pull_card_is_not_gated(md):  # T5
    assert krc.refusal(PR_KEY_BODY, md) is None


# Real landing/cleanup handoffs on core-pull cards record the reviewed head without a verdict and without
# published_pr (t_01a1ce4e6eb94bb2, t_01a1ce18d01255fc, t_01a1cd83c4d14ff7): they are not reviews.
LANDING_MDS = [
    {"pr": PR_KEY_PULL, "reviewed_sha": SHA, "landed_sha": SHA, "review_task": "t_x", "pr_state": "MERGED"},
    {"pr": PR_KEY_PULL, "reviewed_head": SHA, "landed_sha": SHA, "impl_task": "t_y"},
    {"merge_commit": SHA, "pr": PR_KEY_PULL, "reviewed_head": SHA, "worktree": "/x"},
    {"new_head": SHA, "review_task": "t_x", "reviewed_pr": PR_KEY_PULL},
]


@pytest.mark.parametrize("md", LANDING_MDS)
def test_verdictless_handoff_on_core_pull_card_is_not_gated(md):
    assert krc.refusal(PR_KEY_BODY, md) is None


@pytest.mark.parametrize("md", LANDING_MDS)
def test_verdictless_handoff_on_contract_card_is_still_gated(md):
    assert krc.refusal(SHIP_BODY, md)


def test_tool_refuses_pr_key_review_then_accepts_reviewed_pr(board, monkeypatch):
    tid = _claim(board, monkeypatch, PR_KEY_BODY)
    out = _complete(dict(PR_KEY_MD))
    assert out.get("error") and out["rejection"] == "review_contract", out
    assert "metadata.reviewed_pr" in out["error"]
    assert _status(board, tid) == "running"
    ok = _complete({**PR_KEY_MD, "reviewed_pr": PR_KEY_PULL})
    assert not ok.get("error"), ok
    assert _status(board, tid) == "done"


def test_tool_leaves_cards_without_contract_alone(board, monkeypatch):
    tid = _claim(board, monkeypatch, "Fix the sidebar scroll. Done when its tests pass.")
    out = _complete({"review_verdict": "approve", "reviewed_head": "abc"})
    assert not out.get("error"), out
    assert _status(board, tid) == "done"


# --- independence: a ship review verdict recorded outside the card's dispatched worker run (agent-9eea612942) ---
# Chat 20261111_090650_1247e7 completed review card t_01a1cce808ae7ea0 (PR #1133) itself with a subagent's verdict:
# run 29959 started and ended in the same second, no worker session. Ship refused it hours later ("its latest run
# did not complete; no completed review record"). The completion is now refused when it is recorded.

@pytest.mark.parametrize("body", [SHIP_BODY, MISNAMED_BODY, HEAD_BODY, PR_KEY_BODY],
                         ids=["ship", "misnamed", "head", "pr_key"])
@pytest.mark.parametrize("md", [GOOD, {**GOOD, "verdict": "request_changes"}, dict(PR_KEY_MD),
                                {"review_verdict": "approve", "reviewed_sha": SHA}, {"approved": True}],
                         ids=["approve", "request_changes", "pr_key", "review_verdict", "approved"])
def test_independence_refuses_outside_verdict_on_ship_review(body, md):
    msg = krc.independence_refusal(body, md, None) or ""
    card = (f"this is a ship review card for {krc.core_pull(body)}" if krc.states_contract(body)
            else f"this card names core pull {krc.core_pull(body)}")
    assert msg.startswith(f"{card}; `hermes-core-wt ship` accepts only")
    assert "no completed review record" in msg and "kanban_comment" in msg
    assert krc.independence_refusal(body, md, 7) is None  # the card's own dispatched worker run


@pytest.mark.parametrize("body,md", [
    (SHIP_BODY, {"published_pr": PR, "verdict": "approve", "reviewed_sha": SHA, "reviewed_pr": PR}),  # landing
    (SHIP_BODY, {"reviewed_sha": SHA, "reviewed_pr": PR, "landed_sha": SHA}),  # no verdict key
    (PR_KEY_BODY, LANDING_MDS[0]),
    (SHIP_BODY, {"changed_files": ["a.py"]}), (SHIP_BODY, {}), (SHIP_BODY, None),
    ("Fix the sidebar scroll.", NOT_CORE_GOOD),  # not a ship review card
    (PR_KEY_BODY.replace(PR_KEY_PULL, "https://github.com/daveey/babybear/pull/3730"), dict(PR_KEY_MD)),
    (None, NOT_CORE_GOOD),
], ids=["landing", "no_verdict", "pr_key_landing", "no_review_key", "empty", "none", "not_ship", "babybear", "no_body"])
def test_independence_leaves_non_reviews_alone(body, md):
    assert krc.independence_refusal(body, md, None) is None


def _outside_card(kb, body):
    """A ready card nobody claimed: the shape t_01a1cce808ae7ea0 had when the chat completed it."""
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        return kb.create_task(conn, title="Independent exact-head review PR1349", body=body, assignee="test-worker")


def _runs(kb, tid):
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0]


@pytest.mark.parametrize("body", [SHIP_BODY, PR_KEY_BODY], ids=["contract", "core_pull"])
def test_tool_refuses_outside_approval_on_ship_review(board, body):
    from tools import kanban_tools as kt
    tid = _outside_card(board, body)
    md = {**PR_KEY_MD, "reviewed_pr": PR_KEY_PULL} if body is PR_KEY_BODY else dict(GOOD)
    out = json.loads(kt._handle_complete({"task_id": tid, "summary": "APPROVE (subagent verdict)", "metadata": md}))
    assert out.get("error") and out["rejection"] == "review_independence", out
    assert out["rejection"] in kt.COMPLETE_REJECTION_CLASSES
    assert "accepts only a review completed by a dispatched reviewer run" in out["error"]
    assert _status(board, tid) == "ready" and _runs(board, tid) == 0  # nothing changed


def test_tool_outside_independence_precedes_shape_refusal(board):
    from tools import kanban_tools as kt
    tid = _outside_card(board, MISNAMED_BODY)
    md = {"review_verdict": "approve", "reviewed_sha": SHA, "reviewed_pr": PR}  # also unreadable by ship
    out = json.loads(kt._handle_complete({"task_id": tid, "summary": "APPROVE", "metadata": md}))
    assert out["rejection"] == "review_independence", out


def test_tool_dispatched_worker_completes_same_card(board, monkeypatch):
    tid = _claim(board, monkeypatch, SHIP_BODY)
    out = _complete(dict(GOOD))
    assert not out.get("error"), out
    assert _status(board, tid) == "done"


@pytest.mark.parametrize("body,md", [
    ("Fix the sidebar scroll. Done when its tests pass.", NOT_CORE_GOOD),  # not a ship review card
    (PR_KEY_BODY, dict(LANDING_MDS[0])),  # a verdictless landing handoff on a core-pull card
    ("Investigate the review contract: " + SHIP_BODY, {"changed_files": ["a.py"]}),
], ids=["not_ship", "verdictless_landing", "no_review_key"])
def test_tool_outside_non_review_completions_still_complete(board, body, md):
    from tools import kanban_tools as kt
    tid = _outside_card(board, body)
    out = json.loads(kt._handle_complete({"task_id": tid, "summary": "Closed from the chat", "metadata": md}))
    assert not out.get("error"), out
    assert _status(board, tid) == "done"


def _cli_complete(tid, md, force=False):
    import argparse
    from hermes_cli import kanban as cli
    return cli._cmd_complete(argparse.Namespace(task_ids=[tid], summary="APPROVE (subagent verdict)", result=None,
                                                metadata=json.dumps(md), force=force))


@pytest.mark.parametrize("force", [False, True])
def test_cli_refuses_outside_approval_on_ship_review(board, capsys, force):
    tid = _outside_card(board, SHIP_BODY)
    assert _cli_complete(tid, dict(GOOD), force=force) != 0
    assert "review_independence" in capsys.readouterr().err
    assert _status(board, tid) == "ready" and _runs(board, tid) == 0


def test_cli_outside_non_review_completion_still_completes(board):
    tid = _outside_card(board, PR_KEY_BODY)
    assert _cli_complete(tid, dict(LANDING_MDS[0])) == 0
    assert _status(board, tid) == "done"


def test_cli_from_the_cards_dispatched_worker_completes(board, monkeypatch):
    tid = _claim(board, monkeypatch, SHIP_BODY)  # HERMES_KANBAN_TASK + HERMES_KANBAN_RUN_ID name this card's run
    assert _cli_complete(tid, dict(GOOD)) == 0
    assert _status(board, tid) == "done"


def test_dashboard_done_refuses_outside_approval_on_ship_review(board):
    from types import SimpleNamespace
    from hermes_cli import kanban_db_connect as kbc
    from plugins.kanban.dashboard import plugin_api
    done = plugin_api._STATUS_HANDLERS["done"]
    tid = _outside_card(board, SHIP_BODY)
    with kbc.connect() as conn:
        with pytest.raises(ValueError, match="review_independence"):
            done(conn, tid, SimpleNamespace(result=None, summary="APPROVE", metadata=dict(GOOD)))
        assert kb_status(board, conn, tid) == "ready"
        assert done(conn, tid, SimpleNamespace(result="closed", summary="closed", metadata={"note": "x"}))
        assert kb_status(board, conn, tid) == "done"


def kb_status(kb, conn, tid):
    return kb.get_task(conn, tid).status


# --- a dispatched worker's terminal shell (CLI fallback) -----------------------------------------------------
# A worker's terminal shell carries HERMES_KANBAN_WORKER_TASK and HERMES_SESSION_ID, not HERMES_KANBAN_TASK /
# HERMES_KANBAN_RUN_ID. Its `hermes kanban complete` (the fallback on kanban tool DB errors) closes the card's own
# claimed run, whose owner_admitted session is the reviewer's: ship's _review_verdict accepts that record. The env
# alone is never trusted: the shell must descend from the run's live bound worker and carry its admitted session.

WORKER_SESSION = "local-reviewer-session"


def _worker_shell_card(kb, monkeypatch, body, *, bound_pid=None, session=WORKER_SESSION, env_session=WORKER_SESSION,
                       bound=True, spawn=None, title="Independent exact-head review PR1349"):
    """``spawn=(pid, fingerprint)`` records the claim's spawned worker (``tasks.worker_pid``); ``bound=False`` leaves
    the run without a ``worker_bound`` event, so only that spawn pid can prove the shell."""
    import os
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title=title, body=body, assignee="test-worker")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
        with kb.write_txn(conn):
            if session:
                kb._append_event(conn, tid, "owner_admitted", {"session_id": session, "pid": 1}, run_id=run_id)
            if bound:
                kb._append_event(conn, tid, "worker_bound", {"pid": bound_pid or os.getppid()}, run_id=run_id)
            if spawn:
                conn.execute("UPDATE tasks SET worker_pid = ?, worker_started_at = ? WHERE id = ?", (*spawn, tid))
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_KANBAN_WORKER_TASK", tid)
    if env_session:
        monkeypatch.setenv("HERMES_SESSION_ID", env_session)
    return tid, run_id


def _closing_run(kb, tid):
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        return conn.execute("SELECT id, outcome, status FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                            (tid,)).fetchone()


@pytest.mark.parametrize("force", [False, True])
def test_cli_from_the_cards_worker_shell_completes_its_own_run(board, monkeypatch, force):
    tid, run_id = _worker_shell_card(board, monkeypatch, SHIP_BODY)
    assert _cli_complete(tid, dict(GOOD), force=force) == 0
    assert _status(board, tid) == "done"
    row = _closing_run(board, tid)
    assert (row["id"], row["outcome"], row["status"]) == (run_id, "completed", "done")  # the record ship reads
    assert _runs(board, tid) == 1  # no synthesized run


# The proof is card-agnostic (run_for's docstring): a plain, non-review card's proven worker shell also gets its run
# id, so its no---force CLI completion is fenced to that run and closes it instead of hitting the live-claim refusal
# (the recorded spawn pid is alive with its fingerprint, so without that run id complete_task raises LiveClaimError).

def test_cli_worker_shell_completes_a_plain_card_without_force(board, monkeypatch):
    import os
    from hermes_cli import kanban_db_connect as kbc
    tid, run_id = _worker_shell_card(board, monkeypatch, "Fix the sidebar scroll. Done when its tests pass.",
                                     title="Fix the sidebar scroll", spawn=(os.getppid(), _fingerprint(os.getppid())))
    with kbc.connect() as conn:  # precondition: without the run id this completion would be refused
        assert board._claim_is_live(conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())
    assert _cli_complete(tid, {"changed_files": ["a.py"]}, force=False) == 0
    assert _status(board, tid) == "done"
    row = _closing_run(board, tid)
    assert (row["id"], row["outcome"], row["status"]) == (run_id, "completed", "done")
    assert _runs(board, tid) == 1


def _foreign_pid():
    import subprocess
    import sys
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


@pytest.mark.parametrize("case", ["not_descendant", "session_mismatch", "no_session_env", "no_admitted_session"])
def test_cli_worker_env_alone_is_not_trusted(board, monkeypatch, capsys, case):
    proc = _foreign_pid() if case == "not_descendant" else None
    try:
        tid, _ = _worker_shell_card(
            board, monkeypatch, SHIP_BODY, bound_pid=proc.pid if proc else None,
            session=None if case == "no_admitted_session" else WORKER_SESSION,
            env_session={"session_mismatch": "local-other-chat", "no_session_env": None}.get(case, WORKER_SESSION))
        if case == "no_session_env":
            monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
        assert _cli_complete(tid, dict(GOOD), force=True) != 0
    finally:
        if proc:
            proc.kill()
            proc.wait()
    err = capsys.readouterr().err
    assert "review_independence" in err
    assert "CLI fallback cannot record a review ship accepts" in err and "kanban_block" in err
    assert "Leave the card for a worker" not in err  # wrong advice for the card's own worker
    assert _status(board, tid) == "running"


# No worker_bound event (the bind never landed): the claim's spawned worker_pid proves the shell, but only while its
# start-time fingerprint still matches (a recycled pid is a stranger) and never as pid 1 (every process descends
# from init).

def _fingerprint(pid):
    from hermes_cli.kanban_db_dispatch import _process_fingerprint
    return _process_fingerprint(pid)


def test_cli_worker_shell_proven_by_the_fingerprinted_spawn_pid(board, monkeypatch):
    import os
    tid, run_id = _worker_shell_card(board, monkeypatch, SHIP_BODY, bound=False,
                                     spawn=(os.getppid(), _fingerprint(os.getppid())))
    assert _cli_complete(tid, dict(GOOD), force=False) == 0  # fenced to the proven run, no --force needed
    assert _status(board, tid) == "done"
    row = _closing_run(board, tid)
    assert (row["id"], row["outcome"], row["status"]) == (run_id, "completed", "done")
    assert _runs(board, tid) == 1


@pytest.mark.parametrize("case", ["fingerprint_mismatch", "init_pid"])
def test_cli_worker_shell_spawn_pid_fallback_refuses(board, monkeypatch, capsys, case):
    import os
    if case == "fingerprint_mismatch":
        fp = _fingerprint(os.getppid())
        assert fp and "|" in fp
        spawn = (os.getppid(), "another-boot|" + fp.partition("|")[2])  # same pid number, foreign instantiation
    else:
        spawn = (1, _fingerprint(1))
    tid, _ = _worker_shell_card(board, monkeypatch, SHIP_BODY, bound=False, spawn=spawn)
    assert _cli_complete(tid, dict(GOOD), force=True) != 0
    err = capsys.readouterr().err
    assert "review_independence" in err and "CLI fallback cannot record a review ship accepts" in err
    assert _status(board, tid) == "running"


def test_cli_worker_env_on_an_unclaimed_card_is_refused(board, monkeypatch, capsys):
    tid = _outside_card(board, SHIP_BODY)
    monkeypatch.setenv("HERMES_KANBAN_WORKER_TASK", tid)
    monkeypatch.setenv("HERMES_SESSION_ID", WORKER_SESSION)
    assert _cli_complete(tid, dict(GOOD), force=True) != 0
    assert "CLI fallback cannot record a review ship accepts" in capsys.readouterr().err
    assert _status(board, tid) == "ready" and _runs(board, tid) == 0


def test_cli_worker_env_for_another_card_keeps_the_outside_advice(board, monkeypatch, capsys):
    tid = _outside_card(board, SHIP_BODY)
    monkeypatch.setenv("HERMES_KANBAN_WORKER_TASK", "t_0123456789abcdef")
    assert _cli_complete(tid, dict(GOOD), force=True) != 0
    err = capsys.readouterr().err
    assert "Leave the card for a worker" in err and "CLI fallback" not in err


def test_independence_names_a_core_pull_card_softly():
    """A card that only names a core pull (no stated contract) is not called a ship review card (nit 1)."""
    msg = krc.independence_refusal(PR_KEY_BODY, dict(PR_KEY_MD), None) or ""
    assert msg.startswith(f"this card names core pull {PR_KEY_PULL}; ")
    assert krc.independence_refusal(SHIP_BODY, dict(GOOD), None).startswith(f"this is a ship review card for {PR};")


# --- a brief that names the core pull only in prose (agent-8a7de4b89f) ---------------------------------------
# Review card t_01a1cd9ac1af23fa named PR #1123 only as "softmax-universe/daveey-hermes PR #1123", so core_pull(body)
# was None and independence never applied; the orchestrator chat completed it with reviewed_pr = the core pull URL
# and a delegate_task verdict, and ship refused the synthesized run at land time.
PROSE_BODY = ("Independent exact-head delta review of softmax-universe/daveey-hermes PR #1123 (review_required flag).\n"
              "Done when: an approve or request_changes verdict that names the exact full SHA reviewed (reviewed_sha).")


@pytest.mark.parametrize("pr", [PR, "softmax-universe/daveey-hermes#1349", " https://github.com/Daveey/Hermes/pull/9 "])
def test_independence_refuses_outside_verdict_named_only_by_reviewed_pr(pr):
    md = {**GOOD, "reviewed_pr": pr}
    assert krc.core_pull(PROSE_BODY) is None
    msg = krc.independence_refusal(PROSE_BODY, md, None) or ""
    assert msg.startswith(f"this completion reviews core pull {pr.strip()}; `hermes-core-wt ship` accepts only")
    assert krc.independence_refusal(PROSE_BODY, md, 7) is None  # the card's own dispatched worker run
    assert krc.refusal(PROSE_BODY, md) is None  # the shape gate on such a card is unchanged


@pytest.mark.parametrize("md", [
    {**GOOD, "reviewed_pr": "https://github.com/daveey/babybear/pull/3730"},  # another repository
    {**GOOD, "reviewed_pr": "softmax-universe/babybear#12"},
    {**GOOD, "reviewed_pr": 1123}, {**GOOD, "reviewed_pr": "PR #1123"}, {**GOOD, "reviewed_pr": None},  # no pull
    {"reviewed_sha": SHA, "reviewed_pr": PR},  # no verdict: a landing/cleanup handoff
    {"published_pr": PR, "verdict": "approve", "reviewed_pr": PR},  # a landing handoff
    {"verdict": "approve", "reviewed_sha": SHA},  # names no pull anywhere
], ids=["babybear_url", "babybear_ref", "int", "prose", "none", "no_verdict", "landing", "no_pr"])
def test_independence_reviewed_pr_rule_needs_a_core_pull_verdict(md):
    assert krc.independence_refusal(PROSE_BODY, md, None) is None


@pytest.mark.parametrize("md,want", [
    ({"reviewed_pr": PR}, PR), ({"reviewed_pr": "softmax-universe/daveey-hermes#3"}, "softmax-universe/daveey-hermes#3"),
    ({"reviewed_pr": "https://github.com/daveey/babybear/pull/1"}, None), ({"reviewed_pr": "x#1"}, None),
    ({}, None), (None, None), ("reviewed_pr", None),
])
def test_reviewed_core_pull(md, want):
    assert krc.reviewed_core_pull(md) == want


def test_tool_refuses_outside_approval_named_only_by_reviewed_pr(board):  # RED before agent-8a7de4b89f
    from tools import kanban_tools as kt
    tid = _outside_card(board, PROSE_BODY)
    out = json.loads(kt._handle_complete({"task_id": tid, "summary": "APPROVE (subagent verdict)",
                                          "metadata": {**GOOD, "reviewed_pr": PR}}))
    assert out.get("error") and out["rejection"] == "review_independence", out
    assert f"this completion reviews core pull {PR}" in out["error"]
    assert _status(board, tid) == "ready" and _runs(board, tid) == 0  # nothing changed, no synthesized run


def test_cli_refuses_outside_approval_named_only_by_reviewed_pr(board, capsys):
    tid = _outside_card(board, PROSE_BODY)
    assert _cli_complete(tid, {**GOOD, "reviewed_pr": PR}, force=True) != 0
    assert "review_independence" in capsys.readouterr().err
    assert _status(board, tid) == "ready" and _runs(board, tid) == 0


def test_tool_dispatched_worker_completes_prose_review_card(board, monkeypatch):
    tid = _claim(board, monkeypatch, PROSE_BODY)
    out = _complete({**GOOD, "reviewed_pr": PR})
    assert not out.get("error"), out
    assert _status(board, tid) == "done"
