"""Cross-process CLI -> Desktop handoff with real agent loops and SQLite writes.

Only provider/tool discovery and presentation transports are replaced. Admission,
interruption, finalization, persistence and Desktop RPC dispatch remain real.
"""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hermes_state import SessionDB
from tests.waiting import wait_until
from tui_gateway import server

REPO = Path(__file__).resolve().parents[2]


def _mock_response(content):
    message = SimpleNamespace(content=content, tool_calls=None, reasoning=None,
                              reasoning_content=None, reasoning_details=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")],
                           model="test/model", usage=None)


def make_agent(home, platform):
    # Worktrees may store their git metadata in the real Hermes installation.
    # Update recovery is startup maintenance, not part of turn ownership.
    with patch("hermes_cli._early_recovery.restore_interrupted_pull", return_value=False):
        from run_agent import AIAgent
    with (patch("model_tools.get_tool_definitions", return_value=[]),
          patch("model_tools.check_toolset_requirements", return_value={}),
          patch("agent.process_bootstrap.OpenAI")):
        agent = AIAgent(
            api_key="test-key-1234567890", provider="openrouter",
            base_url="https://openrouter.ai/api/v1", model="test/model",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            session_id="handoff", platform=platform, save_trajectories=False,
            session_db=SessionDB(home / "state.db"), skip_background_review=True,
        )
    agent._owns_session_db = True
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    return agent


def cli_process(home):
    from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin
    agent = make_agent(home, "cli")

    def provider(**kwargs):
        owner = agent._session_db.session_turn_lease_owner("handoff")
        (home / "ready.tmp").write_text(json.dumps(owner))
        (home / "ready.tmp").replace(home / "ready.json")
        assert agent._hard_interrupt_requested.wait(30)
        return _mock_response(content="CLI response after cancellation")

    agent.client.chat.completions.create.side_effect = provider
    first = agent.run_conversation("original CLI input exactly once")
    # Exercise the actual CLI post-turn redirect decision: a handoff notice must
    # not become a new user prompt. Then probe the shared gate independently of
    # that loop (queued completions and one-shot continuations also enter here).
    cli = SimpleNamespace(agent=agent)
    pending, _ = CLIChatTurnMixin._chat_resolve_interrupt(
        cli, SimpleNamespace(result=first), threading.current_thread(), None,
        first.get("final_response", ""))
    assert pending is None
    assert cli._last_turn_interrupted
    second = agent.run_conversation("automatic continuation must not write",
                                    conversation_history=first["messages"])
    assert second["failed"] and not second["failure_retryable"]
    assert second["api_calls"] == 0
    assert agent.client.chat.completions.create.call_count == 1
    (home / "finished.json").write_text(json.dumps({"first": first, "second": second}, default=str))
    agent.close()


CHILD = """
from pathlib import Path
import sys
from tests.tui_gateway.test_cli_takeover_full_turn import cli_process
cli_process(Path(sys.argv[1]))
"""


def test_real_cli_turn_hands_off_to_visible_desktop_reply(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = SessionDB(home / "state.db")
    db.create_session("handoff", source="cli")
    desktop = make_agent(home, "desktop")
    session = {
        "agent": desktop, "session_key": "handoff", "profile_home": str(home),
        "history": [], "history_lock": threading.Lock(), "history_version": 0,
        "running": False, "source": "desktop", "attached_images": [],
        "model_override": "test/model", "cwd": str(tmp_path),
    }
    sid = "full-turn-desktop"
    monkeypatch.setitem(server._sessions, sid, session)
    assert server._ensure_active_session_slot(sid, session) is None
    events = []
    monkeypatch.setattr(server, "_emit", lambda kind, sid, payload=None: events.append((kind, payload)))
    # Keep model configuration pinned to the test provider; no production turn
    # or persistence path is replaced.
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda *a: None)
    monkeypatch.setattr(server, "_sync_agent_compression_with_config", lambda *a: None)
    proc = subprocess.Popen([sys.executable, "-c", CHILD, str(home)], cwd=REPO,
                            env=dict(os.environ), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        wait_until(lambda: (home / "ready.json").exists() or proc.poll() is not None, timeout=45)
        assert proc.poll() is None, proc.communicate()
        owner = json.loads((home / "ready.json").read_text())
        assert owner["holder"].startswith(f"pid={proc.pid}:")
        assert not db.try_acquire_session_turn_lease("handoff", "competitor")
        before = db.get_messages("handoff")
        refused = server._methods["prompt.submit"]("refused", {
            "session_id": sid, "text": "Desktop follow-up exactly once"})
        assert refused["error"]["data"]["reason"] == "SESSION_NOT_OWNED"
        assert db.get_messages("handoff") == before
        taken = server._methods["session.take_over"]("take", {"session_id": sid})
        assert taken["result"]["acknowledged"] is True, taken
        assert taken["result"]["previous_owner"]["pid"] == proc.pid
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, (out, err)
        assert (home / "finished.json").exists()
        assert db.session_turn_lease_owner("handoff") is None

        def desktop_provider(**kwargs):
            live = db.session_turn_lease_owner("handoff")
            assert live["holder"] != owner["holder"]
            assert "platform=desktop" in live["holder"]
            assert not db.try_acquire_session_turn_lease("handoff", "competitor")
            return _mock_response(content="Visible Desktop reply after CLI takeover")

        desktop.client.chat.completions.create.side_effect = desktop_provider
        submitted = server._methods["prompt.submit"]("accepted", {
            "session_id": sid, "text": "Desktop follow-up exactly once"})
        assert submitted["result"]["status"] == "streaming", submitted
        wait_until(lambda: any(k == "message.complete" for k, _ in events), timeout=30)
        session["_run_thread"].join(30)
        complete = [p for k, p in events if k == "message.complete"]
        assert len(complete) == 1
        assert complete[0]["status"] == "complete", complete
        assert complete[0]["text"] == "Visible Desktop reply after CLI takeover", complete
        assert desktop.client.chat.completions.create.call_count == 1
        rows = db.get_messages("handoff")
        contents = [r["content"] for r in rows]
        assert contents.count("original CLI input exactly once") == 1
        assert contents.count("Desktop follow-up exactly once") == 1
        assert contents.count("Visible Desktop reply after CLI takeover") == 1
        assert "automatic continuation must not write" not in contents
        assert db.session_turn_lease_owner("handoff") is None
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.communicate(timeout=10)
        if session.get("_run_thread"):
            session["_run_thread"].join(30)
        server._release_active_session_slot(session)
        desktop.close()
        db.close()
