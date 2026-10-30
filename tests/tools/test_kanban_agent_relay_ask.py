"""A human ask that only asks David to relay a message to an agent/chat is refused (self-heal
agent-c03353a3cf). Replays hub t_01a188c8ddf998ac (blocked event 81905, 2026-10-29 10:53 PT): the
worker asked David to "ask for the broker Gemini secret repair from your own Hermes chat on the
AWS-authorized Mac"; the chat then did the repair itself in 10 minutes. The wall was a routing gap
(the MacBook did not advertise that AWS account), not a decision only David can make."""
import json
import time

import pytest


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """A claimed card in an isolated board, as a dispatcher-spawned worker sees it."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid


def _status(tid):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        return kb.get_task(conn, tid).status
    finally:
        conn.close()


# Exact action of event 81905, plus the unblock/archive tail the ask lint now requires, so only the
# relay check can refuse it.
RELAY_81905 = {
    "action": ("Ask for the broker Gemini secret repair from your own Hermes chat on the AWS-authorized Mac, "
               "then unblock this card. Unblock = verify the hub vend; archive = drop the Gemini repair."),
    "why_human": "No registered gateway holds broker-account Secrets Manager capability",
    "interaction": "external",
    "scope": "Only gemini.inference mapped broker secret",
    "evidence": "capability routing returned no_capability_holder",
}
REAL_APPROVAL = {**RELAY_81905, "interaction": "approval",
                 "action": ("Approve the $40 EC2 launch in this chat. Unblock = launch it and run the soak; "
                            "archive = no launch, nothing is spent.")}

RELAY_VARIANTS = [
    "From your own Hermes Desktop chat on the Mac, ask: \"Repair token-broker gemini.inference\". "
    "Unblock = verify; archive = drop it.",
    "Ask your Hermes chat to rerun the force-push with --force-with-lease. Unblock = continue; archive = drop.",
    "Tell your agent on the MacBook to repair the secret. Unblock = verify; archive = drop.",
    "In your own Hermes CLI chat, request: \"Resume t_x and provision the host\". Unblock = go; archive = stop.",
]
REAL_ASKS = [
    REAL_APPROVAL["action"],
    "Sign in to github.com/settings/billing in the Chrome window and raise the Actions limit. "
    "Unblock = rerun CI; archive = leave CI red.",
    "Plug the Pi into the hallway outlet. Unblock = the agent pairs it; archive = skip the device.",
    "Reply A or B in this chat (A asks the vendor's agent for a refund, B keeps it). Unblock = do it; "
    "archive = keep the order.",
]


@pytest.mark.parametrize("action", [RELAY_81905["action"], *RELAY_VARIANTS])
def test_relay_ask_is_refused_by_the_lint(action):
    from hermes_cli.kanban_block_policy import user_ask_lint_error
    err = user_ask_lint_error({**RELAY_81905, "action": action})
    assert err and err.startswith("agent_relay_ask"), err
    assert "needs=" in err and "cross_session" in err


@pytest.mark.parametrize("action", REAL_ASKS)
def test_real_human_asks_still_pass(action):
    from hermes_cli.kanban_block_policy import user_ask_lint_error
    assert user_ask_lint_error({**RELAY_81905, "action": action}) is None


def test_block_entry_refuses_the_81905_relay_ask(worker_env):
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_block({"reason": "Gemini broker key invalid", "kind": "needs_input",
                                     "human_request": RELAY_81905}))
    assert d.get("rejection") == "agent_relay_ask", d
    assert _status(worker_env) == "running"


def test_block_entry_still_accepts_a_real_approval(worker_env):
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_block({"reason": "needs spend approval", "kind": "needs_input",
                                     "human_request": REAL_APPROVAL}))
    assert d.get("ok") is True, d
    assert _status(worker_env) == "blocked"


def test_create_entry_refuses_a_relay_ask(worker_env, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_create({"title": "Repair broker Gemini key", "initial_status": "blocked",
                                      "human_request": RELAY_81905, "requires": [], "placement": "local"}))
    assert "error" in d and "agent_relay_ask" in d["error"], d


# --- (c) routing: once the MacBook advertises the broker account, the need routes there -----------

def test_macbook_holding_broker_account_admin_gets_the_secrets_need(tmp_path):
    from hermes_cli import kanban_capability_routing as kcr
    now = time.time()
    macbook = {"schema": 1, "gateway": "macbook", "refreshed_at": now, "capabilities": [
        {"name": "aws:767406518141:admin", "ok": True, "checked_at": now},
        {"name": "aws:751442549699:admin", "ok": True, "checked_at": now},
        {"name": "aws:111005867451:admin", "ok": True, "checked_at": now}]}
    out = kcr.plan(["aws:751442549699:secrets-write"], now=now, cfg={}, local={}, remote={"macbook": macbook},
                   self_gw="hub")
    assert out["routes"] == [{"need": "aws:751442549699:secrets-write", "gateway": "macbook", "checked_at": now}]
    # Before: the MacBook advertised only 767406518141, so the same need had no holder.
    macbook["capabilities"] = macbook["capabilities"][:1]
    with pytest.raises(kcr.CapabilityRouteError) as exc:
        kcr.plan(["aws:751442549699:secrets-write"], now=now, cfg={}, local={}, remote={"macbook": macbook},
                 self_gw="hub")
    assert exc.value.code == "no_capability_holder"
    assert "relay" in str(exc.value)  # the refusal must not invite a "go ask your chat" human ask
