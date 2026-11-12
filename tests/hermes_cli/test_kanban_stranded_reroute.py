"""A ready card stranded on runs_on labels no enrolled machine carries gets a worker route. [daveey]

Self-heal agent-7489fcf5cd: hub card t_01a1a521638d0a78 sat ``ready`` for 128 h with ``runs_on=["hermes-hub"]``, a
label no Board machine has (``labels_mismatch``). The worker that fixed the create-time check (PR #971) could not
re-route or finish it (``kanban_complete`` refused ``not_owner``; ``kanban_move`` of another card only repins a
worker-first card the caller created), so it asked David to "mark t_01a1a521638d0a78 Done". Covered here:

* ``kanban_move(task_id=<stranded card>)`` from any scoped worker re-routes it, only when the registry is known, the
  old labels are impossible and the new ones are satisfiable; the old labels are kept in a comment;
* the stranded_in_ready diagnostic names ``impossible_runs_on`` and the reroute;
* the ownership refusal names that route;
* an ask that only asks David to edit/complete/archive a card is refused (``board_op_ask``).
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_creator_repin as kcr
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_worker_node import HUB, MACHINES, WORKER, Gateway

OTHER_WORKER_TASK = "t_01a1c0c6a7d4f3af"  # the PR #971 worker: neither creator nor repin supervisor
REASON = "runs_on ['hermes-hub'] matches no enrolled machine (labels_mismatch); the hub is machine:hub"


@pytest.fixture
def svc():
    return FakeService(lease_runs=True, machines=MACHINES)


@pytest.fixture
def hub(tmp_path, monkeypatch, svc):
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    sync.consume_dispatcher_wake()
    g = Gateway(tmp_path, HUB, svc, monkeypatch, pickup=False)
    yield g
    g.conn.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


def _legacy_card(hub, labels):
    """A row written before PR #971's create-time check: create, then set the stored labels directly."""
    hub.enter()
    tid = kb.create_task(hub.conn, title="Close out the stuck verify", assignee="default")
    with kb.write_txn(hub.conn):
        wn.set_runs_on(hub.conn, tid, labels)
    hub.round()
    return tid


def _runs_on(conn, tid):
    return json.loads(conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0] or "null")


def test_any_worker_reroutes_an_impossible_label_card_and_it_gets_claimed(hub):
    tid = _legacy_card(hub, ["hermes-hub"])
    hub.tick()
    assert kb.get_task(hub.conn, tid).status == "ready"  # stranded: no machine carries "hermes-hub"
    ask = kb.create_task(hub.conn, title="For you: close the stuck card", assignee="default")
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET status = 'blocked', human_request = ? WHERE id = ?",
                         (json.dumps({"action": f"Mark `{tid}` Done in Hermes Kanban."}), ask))
    res = kcr.creator_repin(hub.conn, tid, OTHER_WORKER_TASK, runs_on=["machine:hub"], reason=REASON)
    assert res.get("ok"), res
    assert res["superseded_asks"] == [ask]
    assert any("Superseded" in c.body for c in kb.list_comments(hub.conn, ask))
    assert kb.get_task(hub.conn, ask).status == "blocked"  # a human gate is noted, never released by an agent
    assert _runs_on(hub.conn, tid) == ["machine:hub"]
    notes = [c.body for c in kb.list_comments(hub.conn, tid)]
    assert any("['hermes-hub']" in n and OTHER_WORKER_TASK in n for n in notes), notes  # undo trail
    hub.round()
    hub.tick()
    assert kb.get_task(hub.conn, tid).status == "running"


def test_a_satisfiable_card_is_not_rerouted(hub):
    tid = _legacy_card(hub, ["linux"])  # hub and worker-1 both carry it: a deliberate placement
    res = kcr.creator_repin(hub.conn, tid, OTHER_WORKER_TASK, runs_on=["machine:hub"], reason=REASON)
    assert not res.get("ok") and res["reason_code"] == "not_creator_task", res
    assert _runs_on(hub.conn, tid) == ["linux"]


def test_new_labels_must_be_satisfiable(hub):
    tid = _legacy_card(hub, ["hermes-hub"])
    res = kcr.creator_repin(hub.conn, tid, OTHER_WORKER_TASK, runs_on=["also-not-a-label"], reason=REASON)
    assert not res.get("ok"), res
    assert _runs_on(hub.conn, tid) == ["hermes-hub"]


def test_unknown_registry_refuses(hub):
    tid = _legacy_card(hub, ["hermes-hub"])
    wn.set_registry_fetch(lambda: (_ for _ in ()).throw(OSError("board down")))
    res = kcr.creator_repin(hub.conn, tid, OTHER_WORKER_TASK, runs_on=["machine:hub"], reason=REASON)
    assert not res.get("ok"), res
    assert _runs_on(hub.conn, tid) == ["hermes-hub"]


def test_a_claimed_card_is_not_rerouted(hub):
    tid = _legacy_card(hub, ["hermes-hub"])
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET claim_lock = 'someone:1' WHERE id = ?", (tid,))
    res = kcr.creator_repin(hub.conn, tid, OTHER_WORKER_TASK, runs_on=["machine:hub"], reason=REASON)
    assert not res.get("ok"), res
    assert _runs_on(hub.conn, tid) == ["hermes-hub"]


# --- diagnostic -----------------------------------------------------------------------------------------------

def _ready_row(labels, age=6 * 3600, now=1_800_000_000):
    return {"id": "t_01a1a521638d0a78", "status": "ready", "assignee": "default", "claim_lock": None,
            "runs_on": json.dumps(labels), "created_at": now - age}


def test_stranded_diagnostic_names_impossible_runs_on_and_the_reroute():
    from hermes_cli import kanban_diagnostics as kd
    now = 1_800_000_000
    reg = {"me": HUB, "machines": MACHINES}
    diags = kd.compute_task_diagnostics(_ready_row(["hermes-hub"]), [], [], now=now, config={"registry": reg})
    [d] = [d for d in diags if d.kind == "stranded_in_ready"]
    assert d.data["held_reason"] == "impossible_runs_on", d.data
    assert d.data["runs_on"] == ["hermes-hub"]
    assert "kanban_move(task_id=" in d.detail and "t_01a1a521638d0a78" in d.detail
    # A satisfiable label keeps the generic stranded text; an unknown registry never claims impossibility.
    ok = kd.compute_task_diagnostics(_ready_row(["linux"]), [], [], now=now, config={"registry": reg})
    assert all(d.data.get("held_reason") is None for d in ok if d.kind == "stranded_in_ready")
    unknown = kd.compute_task_diagnostics(_ready_row(["hermes-hub"]), [], [], now=now, config={"registry": None})
    assert all(d.data.get("held_reason") is None for d in unknown if d.kind == "stranded_in_ready")


# --- the ownership refusal names the route ---------------------------------------------------------------------

def test_not_owner_refusal_names_the_reroute(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_TASK", OTHER_WORKER_TASK)
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_complete({"task_id": "t_01a1a521638d0a78", "summary": "verified elsewhere"}))
    assert d.get("rejection") == "not_owner", d
    assert "kanban_move(task_id=" in d["error"] and "kanban_supervisor_request" in d["error"], d


# --- board-op asks ---------------------------------------------------------------------------------------------

BOARD_OP_ASKS = [
    # the 7489fcf5cd ask, with the unblock/archive tail the ask lint already requires
    "One board action is required: mark `t_01a1a521638d0a78` Done in Hermes Kanban. Unblock = the chain "
    "finishes; archive = leave it ready.",
    "Mark t_01a1a521638d0a78 done. Unblock = continue; archive = drop it.",
    "Archive card t_01a1a819f78f7cf2 on the hub board. Unblock = proceed; archive = keep it.",
    "Edit t_01a17c8fcd5e795a runs_on to ['machine:hub'] in the Kanban dashboard. Unblock = resume; archive = drop.",
    "Please move t_01a1a521638d0a78 to Done on the board. Unblock = finish; archive = leave it.",
]
REAL_ASKS = [
    "Approve the $40 EC2 launch in this chat. Unblock = launch it and run the soak; archive = no launch.",
    "Sign in to github.com/settings/billing in Chrome and raise the Actions limit. Unblock = rerun CI; "
    "archive = leave CI red.",
    "Reply A or B in this chat: A ships t_01a1a521638d0a78's PR now, B holds it. Unblock = do the pick; "
    "archive = hold.",
    "Plug the Pi into the hallway outlet. Unblock = the agent pairs it; archive = skip the device.",
]
HR = {"why_human": "workers cannot complete a card they do not own (not_owner)", "interaction": "reply",
      "scope": "one card", "evidence": "kanban_complete refused not_owner"}


@pytest.mark.parametrize("action", BOARD_OP_ASKS)
def test_board_op_ask_is_refused(action):
    from hermes_cli.kanban_block_policy import ask_lint_check, user_ask_lint_error
    err = user_ask_lint_error({**HR, "action": action})
    assert err and err.startswith("board_op_ask"), err
    assert "kanban_move(task_id=" in err
    assert ask_lint_check({**HR, "action": action})[2] == "board_op_ask"


@pytest.mark.parametrize("action", REAL_ASKS)
def test_real_human_ask_passes(action):
    from hermes_cli.kanban_block_policy import user_ask_lint_error
    assert user_ask_lint_error({**HR, "action": action}) is None
