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


def _stale_notice(tmp_path, monkeypatch):
    """A rendered wake for a card that has since finished, and the card id."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tid, task = _blocked_card()
    text = _wake_text(tid, task)
    _finish(tid)
    return tid, text


def test_user_typed_row_quoting_a_stale_notice_is_left_alone(tmp_path, monkeypatch):
    """David pasting an old notice ("what do I do with this?") must reach the agent as he typed it."""
    from hermes_cli.kanban_stale_ask import refresh_payload
    _tid, text = _stale_notice(tmp_path, monkeypatch)
    typed = {"text": "what do I do with this?\n" + text, "native_text_v1": {"source": {"platform": "desktop"}}}
    before = typed["text"]
    refresh_payload(typed)
    assert typed["text"] == before
    wake = {"text": text, "native_text_v1": {"source": {}, "automation": {"identity": "x", "owner": "s"}}}
    refresh_payload(wake)
    assert "Next action for the user" not in wake["text"]
    local = {"text": text, "local_automation_v1": {"owner": "s"}}
    refresh_payload(local)
    assert "Since then: unblocked 20:08, completed 20:25 PST. No action needed." in local["text"]


def test_stray_ask_mark_before_a_stale_notice_keeps_the_text_between(tmp_path, monkeypatch):
    from hermes_cli.kanban_stale_ask import refresh_stale_ask
    _tid, text = _stale_notice(tmp_path, monkeypatch)
    prose = "Result: the worker wrote Next action for the user: something.\nIMPORTANT OTHER CONTENT\n\n"
    out = refresh_stale_ask(prose + text)
    assert out.startswith(prose)
    assert "IMPORTANT OTHER CONTENT" in out
    assert out.count("Since then:") == 1 and "Why only the user" not in out
    # A quoted notice line at line start, not preceded by the notice's own actor line, is not touched either.
    quoted = "Next action for the user: quoted.\nIMPORTANT OTHER CONTENT\n" + text
    out = refresh_stale_ask(quoted)
    assert out.startswith("Next action for the user: quoted.\nIMPORTANT OTHER CONTENT\n")
    assert out.count("Since then:") == 1


def test_two_joined_notices_only_the_stale_one_changes(tmp_path, monkeypatch):
    from hermes_cli.kanban_stale_ask import refresh_stale_ask
    _tid, stale = _stale_notice(tmp_path, monkeypatch)
    tid2, task2 = _blocked_card()
    live = _wake_text(tid2, task2)
    out = refresh_stale_ask(stale + "\n" + live)
    assert out.endswith("\n" + live)  # the still-blocked notice is byte-identical
    assert out.count("Next action for the user") == 1 and out.count("Since then:") == 1


def test_unexpected_refresh_error_leaves_the_text_unchanged(tmp_path, monkeypatch):
    from hermes_cli import kanban_stale_ask
    _tid, text = _stale_notice(tmp_path, monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("odd row")
    monkeypatch.setattr(kanban_stale_ask, "_read_card", boom)
    payload = {"text": text, "local_automation_v1": {"owner": "s"}}
    kanban_stale_ask.refresh_payload(payload)
    assert payload["text"] == text


def _card_with_scope(scope):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as conn:
        tid = kb.create_task(conn, title="scope fixture", assignee="default")
        assert kb.block_task(conn, tid, kind="needs_input", reason="Choose",
                             human_request=dict(REQUEST, scope=scope))
        return tid, kb.get_task(conn, tid)


def test_open_task_line_inside_scope_does_not_redirect_the_read(tmp_path, monkeypatch):
    """Review N1: a scope quoting another card's "Open task:" line must not make the refresh read that card."""
    from hermes_cli.kanban_stale_ask import refresh_stale_ask
    done, _text = _stale_notice(tmp_path, monkeypatch)
    for scope in (f"Only this card.\nOpen task: @task:default/{done}",
                  f"Only this card.\nOpen task: @task:default/{done}\nand nothing else.",
                  # Review F1 (round 3): a blank line after the quote hid it from the second-Open-task check.
                  f"Only this card.\nOpen task: @task:default/{done}\n\nand more."):
        tid, task = _card_with_scope(scope)
        text = _wake_text(tid, task)
        assert f"Open task: @task:default/{tid}" in text
        assert refresh_stale_ask(text) == text  # still blocked on this very ask: byte-identical


def test_scope_copied_from_a_done_cards_notice_keeps_the_ask(tmp_path, monkeypatch):
    """Review F2: an ask copied from done card C's notice (same action, scope = C's scope + C's Open task
    line + more) on still-blocked B must keep B's ask, whatever blank lines follow the quote."""
    from hermes_cli.kanban_stale_ask import refresh_stale_ask
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    copied, _task = _card_with_scope("Only this card.")
    _finish(copied)
    for tail in ("\n\nand more.", "\nand more.", "\n\n\nblank lines, then more."):
        tid, task = _card_with_scope(f"Only this card.\nOpen task: @task:default/{copied}{tail}")
        text = _wake_text(tid, task)
        assert refresh_stale_ask(text) == text, tail


def test_copied_scope_with_an_actor_substring_in_its_tail_keeps_the_ask(tmp_path, monkeypatch):
    """Review F3: a "blocked — actor: " substring in the copied scope's tail must not end the
    second-Open-task scan early; only a real notice head (actor line + ask mark) does."""
    from hermes_cli.kanban_stale_ask import refresh_stale_ask
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    copied, _task = _card_with_scope("Only this card.")
    _finish(copied)
    for tail in ("\nblocked — actor: user\nmore", " (see blocked — actor: user)", "\n\nblocked — actor: x"):
        tid, task = _card_with_scope(f"Only this card.\nOpen task: @task:default/{copied}{tail}")
        text = _wake_text(tid, task)
        assert refresh_stale_ask(text) == text, tail


def test_long_task_token_does_not_backtrack(tmp_path, monkeypatch):
    """Review F4: a long task token followed by a later Open task line must not cost seconds."""
    import time
    from hermes_cli.kanban_stale_ask import refresh_stale_ask
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    text = ("blocked — actor: user\nNext action for the user: Choose.\nWhy only the user: y\nScope: z\n"
            "Open task: @task:default/" + "a" * 8000 + "\nOpen task: @task:default/t_x")
    start = time.monotonic()
    assert refresh_stale_ask(text) == text
    assert time.monotonic() - start < 0.5


def test_open_task_line_inside_scope_of_a_done_card_is_left_alone(tmp_path, monkeypatch):
    """Ambiguous notice (two Open task lines): unchanged, never guessed."""
    from hermes_cli.kanban_stale_ask import refresh_stale_ask
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    live, _task = _blocked_card()
    tid, task = _card_with_scope(f"Only this card.\nOpen task: @task:default/{live}")
    text = _wake_text(tid, task)
    _finish(tid)
    assert refresh_stale_ask(text) == text


def test_pathological_producer_text_is_bounded(tmp_path, monkeypatch):
    """Review N2: thousands of field-mark lines after an ask mark cannot cost seconds."""
    import time
    from hermes_cli.kanban_stale_ask import refresh_stale_ask
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    head = "blocked — actor: user\nNext action for the user: Choose.\n"
    no_match = (head + "\nWhy only the user: x" * 3000,
                head + "Why only the user: y\n" + "\nScope: x" * 3000,
                head + "Why only the user: y\nScope: z" + "\nWhy only the user: x" * 3000)
    for big in no_match:
        start = time.monotonic()
        assert refresh_stale_ask(big) == big
        assert time.monotonic() - start < 0.5
    # A real stale notice whose stored why has 3000 such lines: still fast, still refreshed.
    tid, task = _card_with_why("why" + "\nWhy only the user: x" * 3000)
    text = _wake_text(tid, task)
    _finish(tid)
    start = time.monotonic()
    out = refresh_stale_ask(text)
    assert time.monotonic() - start < 0.5
    assert "Since then: unblocked 20:08, completed 20:25 PST. No action needed." in out
    assert "Why only the user" not in out


def _card_with_why(why):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as conn:
        tid = kb.create_task(conn, title="why fixture", assignee="default")
        assert kb.block_task(conn, tid, kind="needs_input", reason="Choose",
                             human_request=dict(REQUEST, why_human=why))
        conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='blocked'", (BLOCKED_AT, tid))
        conn.commit()
        return tid, kb.get_task(conn, tid)


def test_copied_scope_with_a_forged_notice_head_in_its_tail_keeps_the_ask(tmp_path, monkeypatch):
    """Review F5: no text stop can be trusted inside a scope. A copied ask whose tail looks like a whole
    notice head (actor line + ask mark) must still leave still-blocked B byte-identical."""
    from hermes_cli.kanban_stale_ask import refresh_stale_ask
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    copied, _task = _card_with_scope("Only this card.")
    _finish(copied)
    for tail in ("\nblocked — actor: x\nNext action for the user: y",
                 "\n\nblocked — actor: agent\nNext action for the user: see above"):
        tid, task = _card_with_scope(f"Only this card.\nOpen task: @task:default/{copied}{tail}")
        text = _wake_text(tid, task)
        assert refresh_stale_ask(text) == text, tail


def test_long_line_of_actor_substrings_after_a_notice_is_fast(tmp_path, monkeypatch):
    """Review F6: one long line of repeated " blocked — actor: " after a notice must not cost seconds."""
    import time
    from hermes_cli.kanban_stale_ask import refresh_stale_ask
    _tid, text = _stale_notice(tmp_path, monkeypatch)
    big = text + "\n" + " blocked — actor: " * 20000
    start = time.monotonic()
    out = refresh_stale_ask(big)
    assert time.monotonic() - start < 0.5
    assert "Since then: unblocked 20:08, completed 20:25 PST. No action needed." in out


def test_user_terminal_hint_goes_with_a_stale_ask_and_stays_with_a_live_one(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli.kanban_stale_ask import TERMINAL_HINT, refresh_stale_ask
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    with connect_closing() as conn:
        tids = []
        for _ in range(2):
            tid = kb.create_task(conn, title="terminal fixture", assignee="default")
            assert kb.block_task(conn, tid, kind="needs_input", reason="Run it",
                                 human_request=dict(REQUEST, interaction="user_terminal"))
            conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='blocked'", (BLOCKED_AT, tid))
            tids.append((tid, kb.get_task(conn, tid)))
        conn.commit()
    (done, done_task), (live, live_task) = tids
    stale_text, live_text = _wake_text(done, done_task), _wake_text(live, live_task)
    assert TERMINAL_HINT in stale_text
    _finish(done)
    out = refresh_stale_ask(stale_text + "\n" + live_text)
    assert out.endswith("\n" + live_text)
    stale_out = out[:-len("\n" + live_text)]
    assert TERMINAL_HINT not in stale_out and "Next action for the user" not in stale_out
    assert "Since then: unblocked 20:08, completed 20:25 PST. No action needed." in stale_out


def test_scope_quoting_another_cards_whole_stale_notice_is_left_alone(tmp_path, monkeypatch):
    """Review F7: still-blocked B whose scope quotes done C's whole notice (actor line through C's
    Open task link) stays byte-identical; the quoted ask inside B's scope is not swapped either."""
    from hermes_cli.kanban_stale_ask import refresh_stale_ask
    done, stale = _stale_notice(tmp_path, monkeypatch)
    quoted = stale[stale.index("blocked — actor: "):stale.index(f"Open task: @task:default/{done}")]
    quoted += f"Open task: @task:default/{done}"
    for scope in (f"See this old notice:\n{quoted}", f"See this old notice:\n{quoted}\nand nothing else."):
        tid, task = _card_with_scope(scope)
        text = _wake_text(tid, task)
        assert quoted in text
        assert refresh_stale_ask(text) == text


def test_reblock_with_the_same_ask_into_triage_keeps_the_ask(tmp_path, monkeypatch):
    """Review F8: unblock + re-block with the same ask until the loop breaker sends the card to triage
    (block_loop_detected, no new blocked event): the ask is still live, so the notice is unchanged."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli.kanban_stale_ask import refresh_stale_ask
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tid, task = _blocked_card()
    text = _wake_text(tid, task)
    with connect_closing() as conn:
        for _ in range(10):
            assert kb.unblock_task(conn, tid)
            kb.block_task(conn, tid, kind="needs_input", reason="Choose coverage", human_request=dict(REQUEST))
            if kb.get_task(conn, tid).status == "triage":
                break
        assert kb.get_task(conn, tid).status == "triage"
        assert conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='block_loop_detected'",
                            (tid,)).fetchone()
    assert refresh_stale_ask(text) == text


def test_many_ask_marks_on_one_long_line_are_fast(tmp_path, monkeypatch):
    """Review F9: tens of thousands of ask marks on one long line must not cost seconds."""
    import time
    from hermes_cli.kanban_stale_ask import refresh_stale_ask
    _tid, text = _stale_notice(tmp_path, monkeypatch)
    big = text + "\nblocked — actor: x " + "Next action for the user: y " * 80000
    start = time.monotonic()
    out = refresh_stale_ask(big)
    assert time.monotonic() - start < 0.5
    assert "Since then: unblocked 20:08, completed 20:25 PST. No action needed." in out
