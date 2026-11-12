"""A revoked token on one pooled account must not give the card up (2026-11-12 11:31Z storm).

The hub re-minted five pooled Anthropic accounts at 11:30:13Z, which revoked the access tokens every
worker still held until its next pull. Each running worker got ``401 OAuth access token has been
revoked``, rotated through the pool (the other accounts were 429-benched for the model), ran out of
entries, and surfaced the LAST error, a 401. ``failure_reason="auth"`` mapped to
``KANBAN_TERMINAL_PROVIDER_EXIT_CODE`` (78), and the dispatcher gave 31 cards up on their first
death (14 to triage). Only a pool whose every account is auth-dead after a re-read is a terminal
configuration problem; one whose other accounts are only quota-benched heals by itself.

Real pool store under a sandboxed HERMES_HOME and the real dispatcher; only the SDK client builder is
stubbed.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

MODEL = "claude-opus-5-5"
REVOKED = "Error code: 401 - {'type': 'error', 'error': {'type': 'authentication_error', " \
          "'message': 'OAuth access token has been revoked.'}}"


class _StubClient:
    def __init__(self, key=None):
        self.key = key
        self.is_closed = False

    def close(self):
        self.is_closed = True


class _Http(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


def _pushed(id_, token, priority, **extra):
    """A hub-minted row: no refresh token, renewed only by the minter's push."""
    return {"id": id_, "label": id_, "auth_type": "oauth", "priority": priority, "source": "manual:hub_minted",
            "access_token": token, "expires_at_ms": int(time.time() * 1000) + 8 * 3_600_000, **extra}


def _quota_benched(id_, token, priority):
    now = time.time()
    return _pushed(id_, token, priority, last_status="exhausted", last_status_at=now, last_error_code=429,
                   last_error_reason="quota", model_cooldowns={MODEL: now + 5 * 86400})


def _revoked(id_, token, priority):
    """A row benched on a 401 for ``token`` (by this or a peer process: the bench is persisted)."""
    from agent.credential_pool import fingerprint_secret_value
    return _pushed(id_, token, priority, last_status="exhausted", last_status_at=time.time(),
                   last_error_code=401, last_error_reason="auth", bench_token_fp=fingerprint_secret_value(token))


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("hermes_cli.auth.is_provider_explicitly_configured", lambda pid: pid == "anthropic")
    monkeypatch.setattr("agent.anthropic_credentials.read_hermes_oauth_credentials", lambda: None)
    monkeypatch.setattr("agent.anthropic_credentials.read_claude_code_credentials", lambda: None)
    from hermes_cli import kanban_workspace_jobs
    monkeypatch.setattr(kanban_workspace_jobs.psutil, "process_iter", lambda: iter(()))
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kb.init_db()
    return home


def _write_pool(home, rows):
    (home / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {"anthropic": rows}}))


def _agent_on(entry_id):
    from agent.credential_pool import load_pool
    from run_agent import AIAgent
    pool = load_pool("anthropic")
    agent = AIAgent.__new__(AIAgent)
    agent.provider, agent.model, agent.api_mode = "anthropic", MODEL, "anthropic_messages"
    agent.base_url = agent._anthropic_base_url = "https://api.anthropic.com"
    agent._oauth_1m_beta_disabled = False
    agent.model_routing = None
    agent._credential_pool = pool
    agent._anthropic_client = _StubClient()
    with patch("agent.anthropic_adapter.build_anthropic_client", side_effect=lambda k, *a, **kw: _StubClient(k)):
        assert agent._swap_credential(next(e for e in pool.entries() if e.id == entry_id)) is True
    return agent


def _recover_from_revoked(agent):
    """The 401 recovery the conversation loop runs: refresh the failing entry, else rotate."""
    from agent.agent_runtime_helpers import recover_with_credential_pool
    from agent.error_classifier import FailoverReason
    with patch("agent.anthropic_adapter.build_anthropic_client", side_effect=lambda k, *a, **kw: _StubClient(k)):
        recovered, _ = recover_with_credential_pool(
            agent, status_code=401, has_retried_429=False, classified_reason=FailoverReason.auth,
            error_context={"message": REVOKED})
    return recovered


def _worker_exit_code(agent):
    """The turn the worker surfaces once recovery found nothing, and the exit code it books."""
    from agent.error_classifier import classify_api_error
    from agent.turn_recovery import nonretryable_client_error_result
    from hermes_cli.turn_exit import turn_exit_code
    from tests.agent.test_failed_turn_chat_copy import _Agent

    stub = _Agent()
    stub.provider, stub.model, stub._credential_pool = "anthropic", MODEL, agent._credential_pool
    error = _Http(401, REVOKED)
    classified = classify_api_error(error, provider="anthropic", model=MODEL)
    result = nonretryable_client_error_result(
        stub, error, classified, status_code=401, api_kwargs=None, api_messages=[], messages=[],
        conversation_history=None, api_call_count=1, approx_tokens=10, provider="anthropic",
        base_url="https://api.anthropic.com", model=MODEL)
    assert result["failure_reason"] == "auth"
    return turn_exit_code(result, kanban_worker=True)


def _dispatch_worker_death(code):
    with kbc.connect() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="worker on the pool", assignee="a", max_retries=5)
        kb.claim_task(conn, tid, claimer=f"{host}:w0")
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (72000, tid))
        conn.commit()
        kbd._record_worker_exit(72000, code << 8)
        kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        gave_up = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='gave_up'", (tid,)).fetchone()
        return task, gave_up


def test_revoked_account_with_a_healthy_sibling_rotates_in_place(home):
    _write_pool(home, [_pushed("acct-A", "sk-ant-oat01-revoked", 0), _pushed("acct-B", "sk-ant-oat01-ok", 1)])
    agent = _agent_on("acct-A")
    assert _recover_from_revoked(agent) is True
    assert agent._credential_pool_entry_id == "acct-B"


def test_revoked_account_beside_quota_benched_ones_requeues_the_card_without_giving_up(home):
    """The 11:31Z shape: the revoked account is the last error, the rest are 429-benched for the model."""
    _write_pool(home, [_pushed("acct-A", "sk-ant-oat01-revoked", 0), _quota_benched("acct-C", "sk-ant-oat01-c", 1),
                       _quota_benched("acct-D", "sk-ant-oat01-d", 2)])
    agent = _agent_on("acct-A")
    assert _recover_from_revoked(agent) is False  # nothing left to rotate to right now

    code = _worker_exit_code(agent)
    assert code == kb.KANBAN_RATE_LIMIT_EXIT_CODE, code

    task, gave_up = _dispatch_worker_death(code)
    assert gave_up is None
    assert task.status == "ready" and task.consecutive_failures == 0


def test_revoked_sibling_whose_replacement_was_pushed_meanwhile_recovers_in_the_turn(home):
    """Every other entry is 429-walled and an earlier-revoked one has its replacement on disk now:
    the re-read adopts it and the turn continues on it."""
    _write_pool(home, [_pushed("acct-A", "sk-ant-oat01-revoked", 0), _revoked("acct-B", "sk-ant-oat01-b-old", 1),
                       _quota_benched("acct-C", "sk-ant-oat01-c", 2)])
    agent = _agent_on("acct-A")
    _write_pool(home, [_pushed("acct-A", "sk-ant-oat01-revoked", 0), _pushed("acct-B", "sk-ant-oat01-b-new", 1),
                       _quota_benched("acct-C", "sk-ant-oat01-c", 2)])
    assert _recover_from_revoked(agent) is True
    assert agent._credential_pool_entry_id == "acct-B"
    assert agent._anthropic_api_key == "sk-ant-oat01-b-new"


def test_every_account_revoked_after_a_reread_stays_terminal(home):
    _write_pool(home, [_pushed("acct-A", "sk-ant-oat01-revoked", 0), _revoked("acct-B", "sk-ant-oat01-b-old", 1)])
    agent = _agent_on("acct-A")
    assert _recover_from_revoked(agent) is False

    code = _worker_exit_code(agent)
    assert code == kb.KANBAN_TERMINAL_PROVIDER_EXIT_CODE

    task, gave_up = _dispatch_worker_death(code)
    assert gave_up is not None and task.status != "ready"
