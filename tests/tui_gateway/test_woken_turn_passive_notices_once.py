"""A woken turn over passive kanban notices sees each notice once and re-writes no row.

Shape of chat 20261027_134353_5ab65c (t_01a1895bbfae2d67): the remote sweep appended two passive
``kanban_notice`` rows (n1, n2) to an idle chat; later a local card's notice woke it (row ``local``,
staged by ``_kb_persist_batch``). The woken turn's history ended user(n1), user(n2), user(local). The
model was sent n2 twice, and after the turn state.db held a NEW merged row n1+n2+n2+local plus copies of
the turn's rows.

Real agent loop, real SessionDB, real Desktop turn path; only the provider is faked.
"""
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hermes_state import SessionDB
from tests.waiting import wait_until
from tui_gateway import server


def _mock_response(content):
    message = SimpleNamespace(content=content, tool_calls=None, reasoning=None,
                              reasoning_content=None, reasoning_details=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")],
                           model="test/model", usage=None)


def _make_agent(home):
    with patch("hermes_cli._early_recovery.restore_interrupted_pull", return_value=False):
        from run_agent import AIAgent
    with (patch("model_tools.get_tool_definitions", return_value=[]),
          patch("model_tools.check_toolset_requirements", return_value={}),
          patch("agent.process_bootstrap.OpenAI")):
        agent = AIAgent(
            api_key="test-key-1234567890", provider="openrouter",
            base_url="https://openrouter.ai/api/v1", model="test/model",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            session_id="wake", platform="desktop", save_trajectories=False,
            session_db=SessionDB(home / "state.db"), skip_background_review=True,
        )
    agent._owns_session_db = True
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    return agent


class _KbText(str):
    """A poller text: ``_kb_persist_batch`` reads its event keys off the string."""


def _notice(task_id: str) -> str:
    return f"[Kanban] {task_id} completed: done."


def _wait_turn(session, events, n):
    wait_until(lambda: sum(1 for k, _ in events if k == "message.complete") >= n, timeout=30)
    if session.get("_run_thread"):
        session["_run_thread"].join(30)


def test_woken_turn_sees_each_passive_notice_once_and_rewrites_no_row(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = SessionDB(home / "state.db")
    db.create_session("wake", source="desktop")
    agent = _make_agent(home)
    session = {
        "agent": agent, "session_key": "wake", "profile_home": str(home),
        "history": [], "history_lock": threading.Lock(), "history_version": 0,
        "running": False, "source": "desktop", "attached_images": [],
        "model_override": "test/model", "cwd": str(tmp_path),
    }
    sid = "wake-desktop"
    monkeypatch.setitem(server._sessions, sid, session)
    events = []
    monkeypatch.setattr(server, "_emit", lambda kind, sid, payload=None: events.append((kind, payload)))
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda *a: None)
    monkeypatch.setattr(server, "_sync_agent_compression_with_config", lambda *a: None)
    sent = []

    def provider(**kwargs):
        sent.append([dict(m) for m in kwargs.get("messages") or ()])
        return _mock_response(content=f"reply {len(sent)}")

    agent.client.chat.completions.create.side_effect = provider
    n1, n2, local = _notice("t_n1"), _notice("t_n2"), _notice("t_local")
    try:
        # 1) An ordinary turn: the chat is live with a stamped in-memory history.
        first = server._methods["prompt.submit"]("first", {"session_id": sid, "text": "hello"})
        assert first["result"]["status"] == "streaming", first
        _wait_turn(session, events, 1)

        # 2) The remote sweep appends two passive notice rows to the idle chat (no turn runs).
        for key, text in (("n1", n1), ("n2", n2)):
            db.record_kanban_notice("wake", text, {"notice_id": f"remote:{key}", "display_text": text,
                                                   "kanban_event_keys": [f"remote:{key}"]})

        # 3) A local card's notice wakes the chat through the real poller tail.
        text = _KbText(local)
        text._kb_keys = ["local:1"]
        verdict, fresh, kwargs = server._kb_persist_batch(session, [text], False)
        assert verdict == "written", verdict
        before = db.get_messages("wake")
        before_ids = {r["id"] for r in before}
        assert server._notif_claim_turn(session)
        assert server._notif_submit("__notif__1", sid, session, "\n".join(fresh), "wake", **kwargs) is not False
        _wait_turn(session, events, 2)

        # The model saw each notice exactly once.
        woken = sent[-1]
        body = "\n".join(m["content"] for m in woken if isinstance(m.get("content"), str))
        assert body.count(n1) == 1, body
        assert body.count(n2) == 1, body
        assert body.count(local) == 1, body

        # state.db gained only the turn's own new rows: no copy of an already-persisted row and no
        # merged user row standing in for the notices.
        after = db.get_messages("wake")
        new_rows = [r for r in after if r["id"] not in before_ids]
        old_keys = {(r["role"], r["content"], r.get("timestamp")) for r in before}
        assert [r for r in new_rows if (r["role"], r["content"], r.get("timestamp")) in old_keys] == []
        assert [r["role"] for r in new_rows] == ["assistant"], [(r["role"], r["content"]) for r in new_rows]
        for t in (n1, n2, local):
            assert sum((r["content"] or "").count(t) for r in after) == 1, [(r["id"], r["content"]) for r in after]

        # The next turn still sends each notice once (the live history was not corrupted either).
        nxt = server._methods["prompt.submit"]("next", {"session_id": sid, "text": "and now?"})
        assert nxt["result"]["status"] == "streaming", nxt
        _wait_turn(session, events, 3)
        body = "\n".join(m["content"] for m in sent[-1] if isinstance(m.get("content"), str))
        assert body.count(n2) == 1, body
    finally:
        if session.get("_run_thread"):
            session["_run_thread"].join(30)
        server._release_active_session_slot(session)
        agent.close()
        db.close()


def test_cold_resumed_chat_woken_by_sweep_sees_each_passive_notice_once(tmp_path, monkeypatch):
    """The incident shape: the chat was NOT loaded when the passive rows landed. The unloaded sweep's wake
    (``_wake_session_turn``) runs on a history a cold resume materialized with ``repair_alternation=True``, so
    n1;n2 arrive already merged into ONE carrier whose own ``_row_id`` is n1's (n2's id is only in
    ``_absorbed_row_ids``)."""
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = SessionDB(home / "state.db")
    db.create_session("wake", source="desktop")
    agent = _make_agent(home)
    events = []
    monkeypatch.setattr(server, "_emit", lambda kind, sid, payload=None: events.append((kind, payload)))
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda *a: None)
    monkeypatch.setattr(server, "_sync_agent_compression_with_config", lambda *a: None)
    sent = []

    def provider(**kwargs):
        sent.append([dict(m) for m in kwargs.get("messages") or ()])
        return _mock_response(content=f"reply {len(sent)}")

    agent.client.chat.completions.create.side_effect = provider
    n1, n2, local = _notice("t_n1"), _notice("t_n2"), _notice("t_local")
    # An earlier, finished exchange in the durable transcript.
    db.append_message("wake", "user", content="hello")
    db.append_message("wake", "assistant", content="hi")
    # The remote sweep appends two passive notice rows while no backend holds the chat.
    for key, text in (("n1", n1), ("n2", n2)):
        db.record_kanban_notice("wake", text, {"notice_id": f"remote:{key}", "display_text": text,
                                               "kanban_event_keys": [f"remote:{key}"]})
    # Cold resume: the model history the resume hydration builds.
    raw_history, _display = db.get_resume_conversations("wake")
    history = server.canonicalize_replay_history(raw_history)
    ready = threading.Event()
    ready.set()
    session = {
        "agent": agent, "agent_ready": ready, "session_key": "wake", "profile_home": str(home),
        "history": history, "history_lock": threading.Lock(), "history_version": 0,
        "running": False, "source": "desktop", "attached_images": [],
        "model_override": "test/model", "cwd": str(tmp_path),
    }
    sid = "wake-cold"
    monkeypatch.setitem(server._sessions, sid, session)
    before_ids = {r["id"] for r in db.get_messages("wake")}
    try:
        verdict = server._wake_session_turn("wake", local, display_kind="kanban_notice",
                                            display_metadata={"notice_id": "local:1"}, label="kanban-sweep-wake")
        assert verdict == "started", verdict
        _wait_turn(session, events, 1)
        body = "\n".join(m["content"] for m in sent[-1] if isinstance(m.get("content"), str))
        assert body.count(n1) == 1, body
        assert body.count(n2) == 1, body
        assert body.count(local) == 1, body
        after = db.get_messages("wake")
        new_rows = [(r["role"], r["content"]) for r in after if r["id"] not in before_ids]
        assert [role for role, _ in new_rows] == ["user", "assistant"], new_rows
        for t in (n1, n2, local):
            assert sum((r["content"] or "").count(t) for r in after) == 1, [(r["id"], r["content"]) for r in after]
        # The live history the next turn starts from holds each notice once too.
        live = "\n".join(m.get("content") or "" for m in session["history"] if isinstance(m.get("content"), str))
        assert live.count(n2) == 1, live
    finally:
        if session.get("_run_thread"):
            session["_run_thread"].join(30)
        server._release_active_session_slot(session)
        agent.close()
        db.close()
