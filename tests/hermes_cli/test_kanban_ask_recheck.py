"""Standing human asks are re-checked: a void ask (relay lint, every named card closed) goes back to agent triage."""
import json
import time

import pytest

GEN3 = ("Request gen3 replacement provisioning from an owner Hermes chat so the supported system-config approval "
        "flow can run, then unblock this card.")
PHONE = ("Make one brief call from your cell phone to Baby Bear's number ending 0588, then reply called and "
         "unblock this card.")


def _ask(action, *, why="only David can", evidence="checked the board"):
    return {"action": action, "why_human": why, "interaction": "reply", "scope": "this card", "evidence": evidence}


@pytest.fixture
def conn(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    c = kbc.connect()
    yield c
    c.close()


def _blocked(conn, request, *, title="worker-test"):
    from hermes_cli import kanban_db as kb
    tid = kb.create_task(conn, title=title, assignee="test-worker")
    kb.claim_task(conn, tid)
    assert kb.block_task(conn, tid, reason="waiting on David", kind="needs_input", human_request=request)
    assert kb.get_task(conn, tid).status == "blocked"
    return tid


def _set_status(conn, tid, status):
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
    conn.commit()


def _events(conn, tid):
    return [json.loads(r["payload"] or "{}") for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'ask_rechecked' ORDER BY id", (tid,))]


def _sweep(conn):
    from hermes_cli.kanban_block_policy import sweep_standing_asks
    return sweep_standing_asks(conn)


def test_relay_lint_failure_moves_card_to_triage(conn):
    from hermes_cli import kanban_db as kb
    tid = _blocked(conn, _ask(GEN3))
    assert _sweep(conn) == [tid]
    task = kb.get_task(conn, tid)
    assert task.status == "triage" and task.human_request is None
    (ev,) = _events(conn, tid)
    assert ev["code"] == "agent_relay_ask" and "agent_relay_ask" in ev["reason"]
    assert ev["human_request"]["action"] == GEN3


def test_route_gap_failure_moves_card_to_triage(conn):
    tid = _blocked(conn, _ask("Run the 8 drain test files on the MacBook and paste the results here. Unblock = "
                              "close; archive = drop.", why="No agent route to the MacBook",
                              evidence="needs=['host:macbook'] -> no_capability_holder"))
    assert _sweep(conn) == [tid]
    assert _events(conn, tid)[0]["code"] == "agent_route_gap"


def test_all_named_cards_closed_moves_card_to_triage(conn):
    from hermes_cli import kanban_db as kb
    a = kb.create_task(conn, title="named a")
    b = kb.create_task(conn, title="named b")
    tid = _blocked(conn, _ask(f"Approve the deploy once {a} lands. Unblock = deploy; archive = drop.",
                              evidence=f"waiting on {b}"))
    assert _sweep(conn) == []  # both named cards open: the ask stands
    _set_status(conn, a, "done")
    assert _sweep(conn) == []  # one still open
    _set_status(conn, b, "archived")
    assert _sweep(conn) == [tid]
    assert kb.get_task(conn, tid).status == "triage"
    (ev,) = _events(conn, tid)
    assert ev["code"] == "named_cards_closed" and sorted(ev["named"]) == sorted([a, b])
    assert a in ev["reason"] and b in ev["reason"]


def test_named_id_not_on_this_board_is_untouched(conn):
    tid = _blocked(conn, _ask("Approve once macbook/t_0123456789abcdef is done. Unblock = go; archive = drop."))
    assert _sweep(conn) == []
    from hermes_cli import kanban_db as kb
    assert kb.get_task(conn, tid).status == "blocked"


def test_real_ask_is_untouched(conn):
    from hermes_cli import kanban_db as kb
    tid = _blocked(conn, _ask(PHONE, why="needs David's phone", evidence="bb voice line has no agent dialer"))
    assert _sweep(conn) == []
    task = kb.get_task(conn, tid)
    assert task.status == "blocked" and task.human_request["action"] == PHONE
    assert _events(conn, tid) == []


def test_one_escalation_per_block_event(conn):
    from hermes_cli import kanban_db as kb
    tid = _blocked(conn, _ask(GEN3))
    assert _sweep(conn) == [tid]
    # Put it back as it was without a new block event: later ticks must not escalate again.
    conn.execute("UPDATE tasks SET status = 'blocked', human_request = ? WHERE id = ?",
                 (json.dumps(_ask(GEN3)), tid))
    conn.commit()
    assert _sweep(conn) == []
    assert _sweep(conn) == []
    assert len(_events(conn, tid)) == 1
    # A new block event with a different void ask is checked again.
    _set_status(conn, tid, "ready")
    kb.claim_task(conn, tid)
    other = "Ask your Hermes chat to rotate the key. Unblock = rotate; archive = drop."
    assert kb.block_task(conn, tid, reason="again", kind="needs_input", human_request=_ask(other))
    assert _sweep(conn) == [tid]
    assert len(_events(conn, tid)) == 2


def test_reblocked_same_ask_after_recheck_is_not_escalated_again(conn):
    """An agent that re-checked the ask and re-blocked it unchanged judged it still stands: no recheck loop."""
    from hermes_cli import kanban_db as kb
    a = kb.create_task(conn, title="named a")
    _set_status(conn, a, "done")
    request = _ask(f"Approve the rollout now that {a} is done. Unblock = roll out; archive = drop.")
    tid = _blocked(conn, request)
    assert _sweep(conn) == [tid]
    _set_status(conn, tid, "ready")
    kb.claim_task(conn, tid)
    assert kb.block_task(conn, tid, reason="still needed", kind="needs_input", human_request=request)
    assert _sweep(conn) == []
    assert kb.get_task(conn, tid).status == "blocked"


def test_card_with_live_run_is_untouched(conn):
    from hermes_cli import kanban_db as kb
    tid = _blocked(conn, _ask(GEN3))
    run_id = conn.execute("SELECT MAX(id) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0]
    conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, tid))
    conn.commit()
    assert _sweep(conn) == []
    assert kb.get_task(conn, tid).status == "blocked"


def test_triaged_recheck_reads_as_agent_work():
    from hermes_cli import kanban_wait_info as kwi
    assert "ask_rechecked" in kwi.PARK_EVENT_KINDS and "ask_rechecked" in kwi.AGENT_TRIAGE_EVENTS


def test_relay_pattern_matches_owner_chat_phrasings():
    from hermes_cli.kanban_block_policy import _RELAY, agent_relay_ask_error
    assert agent_relay_ask_error(GEN3)
    for text in ("Request it from an owner chat, then unblock.",
                 "Ask from any owner chat. Unblock = go; archive = drop.",
                 "Run it from an owner Hermes Desktop or CLI chat. Unblock = go; archive = drop."):
        assert _RELAY.search(text), text
    for text in ("Reply 'go' in the chat (or name a different agreement formula). Unblock = build all PRs per the "
                 "plan; archive = drop the build.",
                 "Yes/no on ONE more E1 reboot-matrix EC2 launch. Unblock (yes) = launch once; Archive (no) = hand "
                 "off without it.",
                 PHONE):
        assert not _RELAY.search(text), text


def test_dispatch_tick_runs_the_sweep(conn):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd
    tid = _blocked(conn, _ask(GEN3))
    res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, dry_run=False)
    assert tid in res.ask_rechecked
    assert kb.get_task(conn, tid).status == "triage"
