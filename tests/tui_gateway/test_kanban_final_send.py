"""A chat a worker was refused into is subscribed to the worker's card and gets its message on completion.

The cross_session refusal (session-mentions plugin) subscribes the chat (``tui`` row for a local chat,
``hub`` row ``<gw>/<profile>/<sid>`` for a chat on another gateway). On completion that chat gets the
standard "✔ Kanban <id> done" notice; a ``FINAL SEND for @session:...:`` line addressed to it replaces
the generic summary.
"""

from types import SimpleNamespace

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli.kanban_final_send import final_send_for_chat, parse_final_sends
from tui_gateway.server import _collect_kanban_notifications, _format_kanban_event_text

CHAT = "20261023_204125_cb2658"
OTHER = "20261022_204322_4d8ce1"
MSG = "Result: the deploy is live.\nSecond line of the message."


def _task(chat_id: str = CHAT, platform: str = "tui") -> str:
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="worker card", assignee="default")
        kbn.add_notify_sub(conn, task_id=tid, platform=platform, chat_id=chat_id)
        return tid
    finally:
        conn.close()


def _complete(tid: str, summary: str) -> None:
    conn = kbc.connect()
    try:
        kb.complete_task(conn, tid, summary=summary)
    finally:
        conn.close()


def test_subscribed_chat_gets_the_standard_done_notice():
    tid = _task()
    _complete(tid, "shipped the fix")
    texts = _collect_kanban_notifications({"session_key": CHAT})
    assert len(texts) == 1
    assert texts[0].startswith("✔ ") and f"Kanban {tid} done" in texts[0]
    assert "shipped the fix" in texts[0]


def test_final_send_line_addressed_to_the_chat_replaces_the_summary():
    tid = _task()
    _complete(tid, f"Did the work.\nFINAL SEND for @session:default@macbook/{CHAT}: {MSG}")
    (text,) = _collect_kanban_notifications({"session_key": CHAT})
    assert f"Kanban {tid} done" in text
    assert MSG in text
    assert "Did the work." not in text


def test_final_send_for_another_chat_is_not_shown_here():
    tid = _task()
    _complete(tid, f"generic summary\nFINAL SEND for @session:default@macbook/{OTHER}: private to the other chat")
    (text,) = _collect_kanban_notifications({"session_key": CHAT})
    assert "generic summary" in text
    assert "private to the other chat" not in text


def test_two_final_sends_each_reach_only_their_own_chat():
    summary = (
        f"FINAL SEND for @session:default@macbook/{CHAT}: for the first\n"
        f"FINAL SEND for @session:default/{OTHER}: for the second"
    )
    sends = parse_final_sends(summary)
    assert sends == {CHAT: "for the first", OTHER: "for the second"}


def test_hub_subscription_row_matches_by_session_id():
    ev = SimpleNamespace(kind="completed", payload={"summary": "x", "final_sends": {CHAT: MSG}})
    task = SimpleNamespace(title="worker card", assignee="default", result=None)
    sub = {"task_id": "t_abc", "chat_id": f"macbook/default/{CHAT}"}
    text = _format_kanban_event_text(sub, task, ev, "")
    assert MSG in text and "Kanban t_abc done" in text
    other = {"task_id": "t_abc", "chat_id": f"macbook/default/{OTHER}"}
    assert MSG not in _format_kanban_event_text(other, task, ev, "")


def test_final_send_first_line_is_not_broadcast_to_other_subscribers():
    tid = _task()
    _complete(tid, f"FINAL SEND for @session:default@macbook/{OTHER}: private message")
    (text,) = _collect_kanban_notifications({"session_key": CHAT})
    assert "private message" not in text


def test_result_fallback_does_not_broadcast_addressed_text():
    from gateway.kanban_watchers_notifier import _fmt_completed
    task = SimpleNamespace(title="worker", result=f"FINAL SEND for @session:default/{OTHER}: private result")
    ev = SimpleNamespace(kind="completed", payload={"summary": None})
    text = _format_kanban_event_text({"task_id": "t_example", "chat_id": CHAT}, task, ev, "")
    assert "private result" not in text
    text, _, _ = _fmt_completed(ev, SimpleNamespace(task=task, head="card", title="worker"))
    assert "private result" not in text


def test_final_send_only_replaces_completed_events():
    ev = SimpleNamespace(kind="blocked", payload={"reason": "need input", "final_sends": {CHAT: MSG}})
    task = SimpleNamespace(title="t", assignee="default", result=None)
    text = _format_kanban_event_text({"task_id": "t_abc", "chat_id": CHAT}, task, ev, "")
    assert "blocked" in text and MSG not in text


def test_parser_accepts_decorated_and_bare_refs_and_ignores_junk():
    assert parse_final_sends("**FINAL SEND for @session:default/abc**: hi") == {"abc": "hi"}
    assert parse_final_sends("- `FINAL SEND for @session:p@gw/abc`: hi\nmore") == {"abc": "hi\nmore"}
    assert parse_final_sends("FINAL SEND for nobody: hi") == {}
    assert parse_final_sends("FINAL SEND for @session:default/abc:   ") == {}
    assert parse_final_sends(None) == {}
    assert final_send_for_chat({"final_sends": {"abc": "hi"}}, "gw/default/abc") == "hi"
    assert final_send_for_chat({}, "abc") is None
