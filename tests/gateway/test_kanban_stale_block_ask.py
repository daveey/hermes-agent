"""A Kanban block notice queued in a chat's FIFO re-reads its card when it is delivered (t_01a1ce7ed2addb09).

The wake text is rendered when the card blocks, then waits behind the running turn. Observed: the
notice for t_01a1cd4dbbc27e50 (blocked 19:26, unblocked 20:08, done 20:25 PST) reached its Desktop
chat at 22:14 still saying "Next action for the user: Reply ...".
"""
from types import SimpleNamespace

import pytest

from tests.gateway.test_session_end_delegations import _local

REQUEST = dict(action="Reply 'accept offline coverage' or 'require disposable Docker proof'. Unblock = apply it; "
                      "archive = drop the remaining acceptance work.",
               why_human="Accepting offline coverage changes the acceptance criterion.", interaction="reply",
               scope="Only the standalone Docker integration acceptance.", evidence="No Docker endpoint on either host.")
BLOCKED_AT, UNBLOCKED_AT, DONE_AT = 1794453987, 1794456521, 1794457554  # 19:26:27, 20:08:41, 20:25:54 PST


def _blocked_card():
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    kb.init_db()
    with connect_closing() as conn:
        tid = kb.create_task(conn, title="state CI fixture", assignee="default")
        assert kb.block_task(conn, tid, kind="needs_input", reason="Choose coverage", human_request=dict(REQUEST))
        conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='blocked'", (BLOCKED_AT, tid))
        conn.commit()
        return tid, kb.get_task(conn, tid)


def _finish(tid):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as conn:
        assert kb.unblock_task(conn, tid)
        assert kb.complete_task(conn, tid, result="fixed")
        for kind, ts in (("unblocked", UNBLOCKED_AT), ("completed", DONE_AT)):
            conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind=?", (ts, tid, kind))
        conn.commit()


def _wake_text(tid, task):
    """The wake the gateway notifier renders at block time (``_fmt_blocked`` body under the wake header)."""
    from gateway.kanban_watchers_notifier import _fmt_blocked
    ev = SimpleNamespace(kind="blocked", payload={"reason": "Choose coverage", "actor": f"agent (task {tid})"})
    _msg, handoff, _ = _fmt_blocked(ev, SimpleNamespace(task=task, task_id=tid, board_slug="default",
                                                        head=f"Kanban {tid}"))
    return (f"[kanban] Task {tid} blocked; needs attention.\nTitle: state CI fixture\n\n"
            f"Check the result or decide the next step.\nResult: {handoff}\n\nThis is an automatic notification.")


async def _deliver_later(tmp_path, monkeypatch, then):
    """Admit the wake while the card is blocked, run ``then``, execute the queued row; return the turn's text."""
    from gateway.session_ingress import execute_admission
    from gateway.wake import deliver_wake
    from hermes_state_runtime import list_session_admissions
    runner, authority, ref, _ = await _local(tmp_path, monkeypatch)
    tid, task = _blocked_card()
    text = _wake_text(tid, task)
    assert "Next action for the user: " in text
    live = authority.sessions[ref.session_id]
    await deliver_wake(runner._delivery_adapter_for(live.source), text=text, session_id=ref.session_id,
                       source=live.source)
    then(tid)
    row = [r for r in list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)
           if tid in str(r['payload'])][-1]
    seen = []

    async def turn(event):
        seen.append(event.text)
        return 'ok'
    monkeypatch.setattr(runner, '_handle_message', turn)
    await execute_admission(authority, ref, row)
    assert seen, 'the queued wake never ran as a turn'
    return text, seen[0]


@pytest.mark.asyncio
async def test_blocked_wake_delivered_after_card_finished_drops_the_ask(tmp_path, monkeypatch):
    original, delivered = await _deliver_later(tmp_path, monkeypatch, _finish)
    assert "Next action for the user" not in delivered
    assert "Why only the user" not in delivered
    assert "Since then: unblocked 20:08, completed 20:25 PST. No action needed." in delivered
    # The notice itself stays: header, scope and task link are unchanged.
    head = original.split("Next action for the user")[0]
    assert delivered.startswith(head)
    assert "Scope: Only the standalone Docker integration acceptance." in delivered
    assert "Open task: @task:default/" in delivered


@pytest.mark.asyncio
async def test_blocked_wake_for_card_still_blocked_keeps_the_full_ask(tmp_path, monkeypatch):
    original, delivered = await _deliver_later(tmp_path, monkeypatch, lambda tid: None)
    assert delivered == original
    assert f"Next action for the user: {REQUEST['action']}" in delivered


def test_reblocked_card_points_at_the_newer_ask(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli.kanban_stale_ask import refresh_stale_ask
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tid, task = _blocked_card()
    text = _wake_text(tid, task)
    with connect_closing() as conn:
        assert kb.unblock_task(conn, tid)
        assert kb.block_task(conn, tid, kind="needs_input", reason="New question",
                             human_request=dict(REQUEST, action="Pick B. Unblock = do B; archive = drop."))
    out = refresh_stale_ask(text)
    assert "Next action for the user" not in out
    assert "blocked again" in out and "replaced by a newer one" in out


def test_remote_or_unreadable_cards_are_left_alone(tmp_path, monkeypatch):
    from hermes_cli.kanban_stale_ask import refresh_stale_ask
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    remote = ("blocked — actor: user\nNext action for the user: Choose.\nWhy only the user: Policy.\n"
              "Scope: One.\nOpen task: @task:default@hub-ec2/t_remote")
    assert refresh_stale_ask(remote) == remote
    missing = remote.replace("@hub-ec2", "")
    assert refresh_stale_ask(missing) == missing
