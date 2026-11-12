"""Kanban wakes that pile up behind a busy turn run as ONE turn built from live card state (t_01a1cec8510df47f).

On 2026-11-11 Desktop chat 20261026_170127_f07c32 fell 19 wakes (3.5 h) behind: each Kanban wake
committed its own FIFO row and ran its own 30-80 min turn, and the wakes that finally ran described
cards that had long since been claimed or completed. Real session authority, real state.db FIFO and a
real Kanban board; nothing claims (``_schedule`` is disabled), so every admitted row stays queued
exactly as it does behind a running turn.
"""
import inspect

import pytest

from gateway import kanban_wake_fold, session_automation
from gateway.wake import deliver_wake
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_state_runtime import list_session_admissions

GUIDANCE = ("This is an automatic task-status notification, not a request to decompose the task again. "
            "Inspect the current board before creating follow-up tasks; do not recreate tasks or graphs that "
            "already exist.")
_STATUS = {'completed': 'completed', 'chat_wait_unclaimed': 'is ready with no worker claiming it',
           'blocked': 'blocked; needs attention'}


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'board.db'))

    def task(title, **kw):
        conn = kbc.connect()
        try:
            return kb.create_task(conn, title=title, assignee='worker', **kw)
        finally:
            conn.close()
    return task


def _complete(tid, summary):
    conn = kbc.connect()
    try:
        assert kb.complete_task(conn, tid, summary=summary, force=True)
    finally:
        conn.close()


def _block(tid):
    conn = kbc.connect()
    try:
        assert kb.block_task(conn, tid, reason='needs a decision', kind='needs_input', human_request=dict(
            action='Choose A. Unblock = proceed; archive = drop it.', why_human='Owner decision',
            interaction='reply', scope='Fixture', evidence='Fixture decision'))
    finally:
        conn.close()


def _claim(tid):
    conn = kbc.connect()
    try:
        assert kb.claim_task(conn, tid) is not None
    finally:
        conn.close()


async def _wake(adapter, source, tid, kind):
    """What the notifier's ``wake()`` hands ``deliver_wake`` for one event of card ``tid``."""
    body = f'[kanban] Task {tid} {_STATUS[kind]}.\nTitle: card {tid}\nAssignee: @worker\nBoard: default'
    kwargs = {}
    if 'kanban_card' in inspect.signature(deliver_wake).parameters:  # absent on the parent: RED shows behaviour
        kwargs['kanban_card'] = {'task_id': tid, 'board': '', 'kinds': [kind], 'text': body}
    await deliver_wake(adapter, text=body + '\n\n' + GUIDANCE, source=source, **kwargs)


async def _local_chat(tmp_path, monkeypatch):
    from tests.gateway.test_session_end_delegations import _local
    runner, authority, ref, _actor = await _local(tmp_path, monkeypatch)
    live = authority.sessions[ref.session_id]
    adapter = runner._delivery_adapter_for(live.source)

    def queued():
        return [row for row in list_session_admissions(authority.db, session_id=ref.session_id)
                if row['status'] == 'queued']
    return authority, adapter, live.source, queued, None


async def _discord_chat(tmp_path, monkeypatch):
    from tests.gateway.test_completion_fold import _discord_session
    authority, human, complete, queued = await _discord_session(tmp_path, monkeypatch)
    runner = authority.runner
    from gateway.config import Platform
    adapter = runner.adapters[Platform.DISCORD]
    adapter.gateway_runner = runner  # what the adapter registry sets at connect (``run_adapters``)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    human.complete = complete
    return authority, adapter, source, queued, human


def _automation(rows):
    return [row for row in rows if row['principal_id'].startswith('automation:')]


@pytest.mark.asyncio
@pytest.mark.parametrize('chat', [_local_chat, _discord_chat], ids=['local', 'discord'])
async def test_five_wakes_queued_during_a_turn_run_as_one_turn(tmp_path, monkeypatch, board, chat):
    authority, adapter, source, queued, _human = await chat(tmp_path, monkeypatch)
    try:
        tids = [board(f'card {n}') for n in range(5)]
        for tid in tids:
            await _wake(adapter, source, tid, 'completed')
        rows = _automation(queued())
        assert len(rows) == 1, [row['payload']['text'][:60] for row in rows]
        text = rows[0]['payload']['text']
        assert text.startswith('[IMPORTANT: 5 Kanban task updates arrived while this session was busy.')
        for tid in tids:  # the line follow-through and ask-gate match stays intact for every card
            assert f'[kanban] Task {tid} completed.' in text
        assert text.count(GUIDANCE) == 1 and text.endswith(GUIDANCE)
        assert text.count('Kanban task updates arrived') == 1  # re-folding replaces the header
    finally:
        authority.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('stale_read', [False, True])
async def test_a_wake_queued_after_a_human_prompt_never_folds_ahead_of_it(tmp_path, monkeypatch, board, stale_read):
    """``stale_read``: the prompt commits after the tail was read but before the fold is written."""
    authority, adapter, source, queued, human = await _discord_chat(tmp_path, monkeypatch)
    try:
        first, second, third = board('first'), board('second'), board('third')
        await _wake(adapter, source, first, 'completed')
        await _wake(adapter, source, second, 'completed')  # folds: both wait at the tail
        if stale_read:
            tail = queued()[-1]
            monkeypatch.setattr(kanban_wake_fold, 'waiting_wake', lambda *args: tail)
        await human('queued follow-up')
        await _wake(adapter, source, third, 'completed')
        rows = queued()
        assert [row['payload']['text'].split('\n')[0] for row in rows] == [
            'running turn', '[IMPORTANT: 2 Kanban task updates arrived while this session was busy. Each card below '
            'shows its state when this batch was built; an earlier premise that no longer holds is relabelled. Treat '
            'them as one batch and give one consolidated response.]', 'queued follow-up',
            f'[kanban] Task {third} completed.']
        assert third not in rows[1]['payload']['text']
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_a_claimed_wake_is_never_rewritten(tmp_path, monkeypatch, board):
    authority, adapter, source, queued, _human = await _local_chat(tmp_path, monkeypatch)
    try:
        first, second = board('first'), board('second')
        await _wake(adapter, source, first, 'completed')
        tail, real = queued()[-1], kanban_wake_fold.waiting_wake
        reads = []  # the next wake read the tail before the claim; later wakes read the real tail
        monkeypatch.setattr(kanban_wake_fold, 'waiting_wake',
                            lambda *args: real(*args) if reads else reads.append(1) or tail)
        authority.db._execute_write(lambda conn: conn.execute(  # the drain claims it after the tail was read
            "UPDATE session_admissions SET status='started',owner_epoch=?,generation=1 WHERE admission_id=?",
            (authority.epoch, tail['admission_id'])))
        await _wake(adapter, source, second, 'completed')
        third = board('third')
        await _wake(adapter, source, third, 'completed')
        rows = {row['admission_id']: row for row in list_session_admissions(
            authority.db, session_id=tail['payload']['local_automation_v1']['owner'], pending_only=False)}
        assert rows[tail['admission_id']]['payload'] == tail['payload']  # the claimed row is untouched
        [row] = queued()  # the wakes after the claim still fold among themselves
        assert f'[kanban] Task {second} completed.' in row['payload']['text']
        assert f'[kanban] Task {third} completed.' in row['payload']['text']
        assert first not in row['payload']['text']
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_an_unclaimed_wait_for_a_card_completed_before_the_fold_says_completed(tmp_path, monkeypatch, board):
    authority, adapter, source, queued, _human = await _local_chat(tmp_path, monkeypatch)
    try:
        waited, other, running = board('waited on'), board('other'), board('claimed meanwhile')
        await _wake(adapter, source, waited, 'chat_wait_unclaimed')
        await _wake(adapter, source, running, 'chat_wait_unclaimed')
        _complete(waited, 'shipped the fix')
        _claim(running)
        await _wake(adapter, source, other, 'completed')
        [row] = _automation(queued())
        text = row['payload']['text']
        assert f'[kanban] Task {waited} completed.' in text
        assert 'Result: shipped the fix' in text
        assert f'[kanban] Task {running} claimed by a worker (running).' in text
        assert 'is ready with no worker claiming it.\n' not in text  # no stale premise left as a headline
        assert text.count('Note: an earlier wake for this card said') == 2
        # The stored cards stay as delivered, so a later rebuild relabels from live state again.
        cards = row['payload']['local_automation_v1']['kanban_cards']
        assert [card['kinds'] for card in cards] == [['chat_wait_unclaimed']] * 2 + [['completed']]
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_two_events_for_one_card_give_one_line(tmp_path, monkeypatch, board):
    authority, adapter, source, queued, _human = await _local_chat(tmp_path, monkeypatch)
    try:
        tid = board('one card')
        await _wake(adapter, source, tid, 'chat_wait_unclaimed')
        _block(tid)
        await _wake(adapter, source, tid, 'blocked')
        [row] = _automation(queued())
        text = row['payload']['text']
        assert text.count(f'[kanban] Task {tid}') == 1
        assert text.startswith(f'[kanban] Task {tid} blocked; needs attention.')
        assert 'Kanban task updates arrived' not in text  # one card: no batch header
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_process_completion_folding_is_unchanged_and_never_crosses_a_wake(tmp_path, monkeypatch, board):
    authority, adapter, source, queued, human = await _discord_chat(tmp_path, monkeypatch)
    try:
        first, second = board('first'), board('second')
        await human.complete('proc_a')
        await human.complete('proc_b')
        await _wake(adapter, source, first, 'completed')
        await _wake(adapter, source, second, 'completed')
        await human.complete('proc_c')
        third = board('third')
        await _wake(adapter, source, third, 'completed')
        rows = queued()
        heads = [row['payload']['text'].split('\n')[0] for row in rows]
        assert heads[:2] == ['running turn', '[IMPORTANT: 2 background process results arrived while this session '
                             'was busy. Treat them as one batch and give one consolidated response; preserve failures '
                             'and actionable results.]']
        assert heads[2].startswith('[IMPORTANT: 2 Kanban task updates arrived')
        assert heads[3:] == ['[IMPORTANT: proc_c finished]', f'[kanban] Task {third} completed.']
        assert 'proc_' not in rows[2]['payload']['text'] and 'Kanban' not in rows[1]['payload']['text']
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_a_wake_queued_before_the_upgrade_still_folds(tmp_path, monkeypatch, board):
    """A row admitted by the old code carries no cards: its text is folded as one card."""
    authority, adapter, source, queued, _human = await _local_chat(tmp_path, monkeypatch)
    try:
        old, new = board('old'), board('new')
        await deliver_wake(adapter, text=f'[kanban] Task {old} completed.\nTitle: old\nBoard: default\n\n{GUIDANCE}',
                           source=source)
        await _wake(adapter, source, new, 'completed')
        [row] = _automation(queued())
        text = row['payload']['text']
        assert text.startswith('[IMPORTANT: 2 Kanban task updates')
        assert f'[kanban] Task {old} completed.' in text and f'[kanban] Task {new} completed.' in text
        assert text.count(GUIDANCE) == 1
    finally:
        authority.db.close()


def test_card_metadata_rejects_malformed_cards():
    from hermes_state_runtime import RuntimeStoreError
    assert kanban_wake_fold.card_metadata({}) == {}
    for bad in ([], [{'text': 1}], [{'text': 'x', 'extra': 1}], [{'text': 'x', 'kinds': 'completed'}], 'x'):
        with pytest.raises(RuntimeStoreError):
            kanban_wake_fold.card_metadata({'kanban_cards': bad})
    assert 'kanban_cards' in session_automation._AUTOMATION_METADATA


def test_every_wake_kind_has_a_status_label():
    """``chat_wait_unclaimed`` joined the wake kinds with no label, so every such wake told the chat
    "Task <id> gateway.kanban.wake.chat_wait_unclaimed." (state.db message 873383, 2026-11-11)."""
    from agent.i18n import t
    from gateway.kanban_watchers_notifier import _WAKE_KINDS
    # failure_finalized has no label by design: build_wake_text renders it through describe_failure.
    labelled = [kind for kind in _WAKE_KINDS if kind != 'failure_finalized']
    missing = [kind for kind in labelled if t(f'gateway.kanban.wake.{kind}') == f'gateway.kanban.wake.{kind}']
    assert not missing
