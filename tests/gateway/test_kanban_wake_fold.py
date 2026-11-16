"""Kanban wakes that pile up behind a busy turn run as ONE turn built from live card state (t_01a1cec8510df47f).

On 2026-11-11 Desktop chat 20261026_170127_f07c32 fell 19 wakes (3.5 h) behind: each Kanban wake
committed its own FIFO row and ran its own 30-80 min turn, and the wakes that finally ran described
cards that had long since been claimed or completed. Real session authority, real state.db FIFO and a
real Kanban board; nothing claims (``_schedule`` is disabled), so every admitted row stays queued
exactly as it does behind a running turn.
"""
import inspect
import json

import pytest

from gateway import kanban_wake_fold, session_automation
from gateway.wake import deliver_wake
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_state_runtime import list_session_admissions

GUIDANCE = ("This is an automatic task-status notification, not a request to decompose the task again. "
            "Inspect the current board before creating follow-up tasks; do not recreate tasks or graphs that "
            "already exist.")
_STATUS = {'review_requested': 'handed off for review',
           'completed': 'completed', 'chat_wait_unclaimed': 'is ready with no worker claiming it',
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
                           source=source, core_update_notifier=True)  # the old notifier's producer: kanban-wake, no card
        await _wake(adapter, source, new, 'completed')
        [row] = _automation(queued())
        text = row['payload']['text']
        assert text.startswith('[IMPORTANT: 2 Kanban task updates')
        assert f'[kanban] Task {old} completed.' in text and f'[kanban] Task {new} completed.' in text
        assert text.count(GUIDANCE) == 1
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_a_wake_past_the_card_cap_queues_alone_instead_of_being_refused(tmp_path, monkeypatch, board):
    """A fold past ``_MAX_CARDS`` distinct cards was refused (``invalid_params``) and raised
    ``WakeNotAccepted``, so the notifier rewound and every later wake to the chat was refused until
    the drain claimed the tail (t_01a1cf7adf592a71). The unfolded wake admits."""
    authority, adapter, source, queued, _human = await _local_chat(tmp_path, monkeypatch)
    try:
        cap = kanban_wake_fold._MAX_CARDS
        for n in range(cap):  # completed wakes never read the board, so the ids need no cards
            await _wake(adapter, source, f't_full{n:04d}', 'completed')
        [full] = _automation(queued())
        assert len(full['payload']['local_automation_v1']['kanban_cards']) == cap
        await _wake(adapter, source, 't_overflow', 'completed')
        await _wake(adapter, source, 't_after', 'completed')  # later wakes fold into the new tail
        rows = _automation(queued())
        assert rows[0]['admission_id'] == full['admission_id']
        assert rows[0]['payload'] == full['payload']  # the full row is untouched
        assert len(rows) == 2
        assert [card['task_id'] for card in rows[1]['payload']['local_automation_v1']['kanban_cards']] == [
            't_overflow', 't_after']
    finally:
        authority.db.close()


async def _drain(authority, row, monkeypatch):
    """Run the queued ``row`` the way the drain does once it claims it; return the turn's text."""
    from gateway.session_ingress import execute_admission
    from gateway.session_contract import SessionRef
    seen = []

    async def turn(event):
        seen.append(event.text)
        return 'ok'
    monkeypatch.setattr(authority.runner, '_handle_message', turn)
    descriptor = kanban_wake_fold._descriptor(row['payload'])
    from tests.gateway.session_claims import claim
    ref = SessionRef(authority.profile_id, descriptor['owner'])
    await execute_admission(authority, ref, claim(authority, ref, row))
    assert seen, 'the queued wake never ran as a turn'
    return seen[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('after', ['changes_requested', 'implementation_running', 'done', 'blocked',
                                  'review', 'review_running'])
@pytest.mark.parametrize('stage', ['fold', 'drain'])
async def test_review_wake_uses_current_review_state_at_drain(tmp_path, monkeypatch, board, after, stage):
    authority, adapter, source, queued, _human = await _local_chat(tmp_path, monkeypatch)
    try:
        tid = board('design before implementation')
        conn = kbc.connect()
        try:
            assert kb.claim_task(conn, tid)
            assert kb.request_review(conn, tid, summary='Design only; not implemented')
            await _wake(adapter, source, tid, 'review_requested')
            [row] = _automation(queued())
            if after != 'review':
                assert kb.claim_review_task(conn, tid)
            if after in {'changes_requested', 'implementation_running'}:
                assert kb.request_changes(conn, tid, reason='Implement the reviewed design')[0]
                if after == 'implementation_running':
                    assert kb.claim_task(conn, tid)
            elif after == 'done':
                _complete(tid, 'Review approved')
            elif after == 'blocked':
                _block(tid)
            task = kb.get_task(conn, tid)
            assert task is not None
            status = task.status
        finally:
            conn.close()
        if stage == 'fold':
            await _wake(adapter, source, board('another card'), 'completed')
            [folded] = _automation(queued())
            text = folded['payload']['text']
        else:
            text = await _drain(authority, row, monkeypatch)
        if after in {'review', 'review_running'}:
            assert row['payload']['text'].removesuffix('\n\n' + GUIDANCE) in text
            assert 'Note: an earlier wake' not in text
        else:
            assert f'Live status: {status}.' in text
            assert 'Note: an earlier wake for this card said "handed off for review"' in text
            if after == 'done':
                assert f'[kanban] Task {tid} completed.' in text
    finally:
        authority.db.close()


@pytest.mark.parametrize('status', ['done', 'ready', 'blocked', None, 'unreadable'])
def test_replica_only_card_live_state(tmp_path, monkeypatch, board, status):
    import sqlite3
    from hermes_cli import kanban_replica_freshness as fresh
    from hermes_cli import kanban_db_sync as sync
    tid = 't_01a1d59e5543daa1'
    path = tmp_path / 'replica.db'
    conn = kbc.connect()
    try:
        sync.apply_sync_schema_steps(conn)
        assert kb.get_task(conn, tid) is None
    finally:
        conn.close()
    with sqlite3.connect(path) as replica:
        if status != 'unreadable':
            replica.execute('CREATE TABLE tasks(id TEXT, status TEXT, ready_since INTEGER, sync_version INTEGER, result TEXT)')
            replica.execute('CREATE TABLE task_runs(id INTEGER, task_id TEXT, summary TEXT, ended_at INTEGER, started_at INTEGER)')
            if status:
                replica.execute('INSERT INTO tasks VALUES (?, ?, NULL, 12, ?)', (tid, status, 'Fallback result'))
                replica.execute('INSERT INTO task_runs VALUES (1, ?, ?, 20, 10)', (tid, 'Replica handoff'))
    monkeypatch.setattr(fresh, '_replica', lambda _: (path, 0))
    card = {'task_id': tid, 'board': '', 'kinds': ['blocked'],
            'text': 'Remote blocked\nNext action for the user: choose'}
    expected = (None, '') if status in (None, 'unreadable') else (status, 'Replica handoff' if status == 'done' else '')
    assert kanban_wake_fold._live_task(card) == expected
    if status in ('blocked', None, 'unreadable'):
        assert kanban_wake_fold.refresh_card(card, strict=True) == card


def test_review_wake_with_newer_running_replica_does_not_infer_run_lane(monkeypatch, board):
    from hermes_cli import kanban_replica_freshness as fresh
    tid = board('replica reviewer')
    card = {'task_id': tid, 'board': '', 'kinds': ['review_requested'],
            'text': f'[kanban] Task {tid} handed off for review.'}
    monkeypatch.setattr(fresh, 'newer_rows', lambda *_: {tid: {'status': 'running'}})
    assert kanban_wake_fold.refresh_card(card) == card


@pytest.mark.asyncio
async def test_a_lone_unclaimed_wait_claimed_before_the_drain_runs_with_its_live_status(tmp_path, monkeypatch, board):
    """Row 3605 (2026-11-12, chat 20261026_170127_f07c32) waited behind a running turn with no later
    wake to fold into it, so it would have told the chat a card claimed 15 min earlier had no worker."""
    authority, adapter, source, queued, _human = await _local_chat(tmp_path, monkeypatch)
    try:
        tid = board('waited on')
        await _wake(adapter, source, tid, 'chat_wait_unclaimed')
        [row] = _automation(queued())
        stored = json.loads(json.dumps(row['payload']))
        _claim(tid)
        text = await _drain(authority, row, monkeypatch)
        assert text.startswith(f'[kanban] Task {tid} claimed by a worker (running).\n')
        assert 'Note: an earlier wake for this card said "is ready with no worker claiming it"' in text
        assert 'Live status: running.' in text and f'Title: card {tid}' in text
        assert text.count(GUIDANCE) == 1 and text.endswith(GUIDANCE)
        [after] = [r for r in list_session_admissions(authority.db, session_id=stored['local_automation_v1']['owner'],
                                                     pending_only=False) if r['admission_id'] == row['admission_id']]
        assert after['payload'] == stored  # the stored row is never rewritten
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_a_lone_wait_whose_premise_still_holds_runs_byte_identical(tmp_path, monkeypatch, board):
    authority, adapter, source, queued, _human = await _local_chat(tmp_path, monkeypatch)
    try:
        tid = board('still waiting')
        await _wake(adapter, source, tid, 'chat_wait_unclaimed')
        [row] = _automation(queued())
        original = row['payload']['text']
        assert await _drain(authority, row, monkeypatch) == original
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_a_folded_row_is_relabelled_again_at_the_claim(tmp_path, monkeypatch, board):
    """Folded while both cards still waited; one is claimed and the other completed before the drain."""
    authority, adapter, source, queued, _human = await _local_chat(tmp_path, monkeypatch)
    try:
        first, second, done = board('first'), board('second'), board('done meanwhile')
        await _wake(adapter, source, first, 'chat_wait_unclaimed')
        await _wake(adapter, source, done, 'chat_wait_unclaimed')
        _claim(second)
        await _wake(adapter, source, second, 'chat_wait_unclaimed')  # relabelled running at fold time
        [row] = _automation(queued())
        assert f'[kanban] Task {first} is ready with no worker claiming it.' in row['payload']['text']
        _claim(first)
        _complete(done, 'shipped it')
        text = await _drain(authority, row, monkeypatch)
        assert text.startswith('[IMPORTANT: 3 Kanban task updates arrived')
        assert text.count('Kanban task updates arrived') == 1
        assert f'[kanban] Task {first} claimed by a worker (running).' in text
        assert f'[kanban] Task {done} completed.' in text and 'Result: shipped it' in text
        assert f'[kanban] Task {second} claimed by a worker (running).' in text
        assert 'is ready with no worker claiming it.\n' not in text
        assert text.count('Note: an earlier wake for this card said') == 3
        assert text.count(GUIDANCE) == 1 and text.endswith(GUIDANCE)
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_an_unreadable_board_leaves_the_claimed_wake_unchanged(tmp_path, monkeypatch, board):
    """Even a relabel the fold already made stays: the as-delivered card text never comes back."""
    authority, adapter, source, queued, _human = await _local_chat(tmp_path, monkeypatch)
    try:
        first, second = board('waited on'), board('claimed before its wake folded')
        await _wake(adapter, source, first, 'chat_wait_unclaimed')
        _claim(second)
        await _wake(adapter, source, second, 'chat_wait_unclaimed')
        [row] = _automation(queued())
        original = row['payload']['text']
        assert f'[kanban] Task {second} claimed by a worker (running).' in original
        _claim(first)

        def unreadable(card):
            raise OSError('board unreadable')
        monkeypatch.setattr(kanban_wake_fold, '_live_task', unreadable)
        assert await _drain(authority, row, monkeypatch) == original
    finally:
        authority.db.close()


def test_refresh_claimed_leaves_unparsable_or_foreign_rows_alone(tmp_path, monkeypatch, board):
    tid = board('waited on')
    _claim(tid)
    body = f'[kanban] Task {tid} is ready with no worker claiming it.\nTitle: x\nBoard: default'
    card = {'task_id': tid, 'board': '', 'kinds': ['chat_wait_unclaimed'], 'text': body}
    identity = json.dumps(['kanban-wake', 'x'])
    # A row whose text no longer contains the stored card (a producer truncated it) is left as is.
    odd = {'text': 'something else\n\n' + GUIDANCE,
           'local_automation_v1': {'identity': identity, 'kanban_cards': [card]}}
    before = json.loads(json.dumps(odd))
    kanban_wake_fold.refresh_claimed(odd)
    assert odd == before
    # Another producer's row is never touched, cards or not.
    other = {'text': body, 'local_automation_v1': {'identity': json.dumps(['loop-tick', 'x']), 'kanban_cards': [card]}}
    kanban_wake_fold.refresh_claimed(other)
    assert other['text'] == body
    # A typed row is never touched.
    typed = {'text': body}
    kanban_wake_fold.refresh_claimed(typed)
    assert typed == {'text': body}
    # The native envelope relabels the same way.
    native = {'text': body + '\n\n' + GUIDANCE,
              'native_text_v1': {'automation': {'identity': identity, 'kanban_cards': [card]}}}
    kanban_wake_fold.refresh_claimed(native)
    assert native['text'].startswith(f'[kanban] Task {tid} claimed by a worker (running).\n')
    assert native['text'].endswith('Board: default\n\n' + GUIDANCE)
    # A one-line card (the TUI owner's notice) with no guidance.
    line = f'[kanban] Task {tid} is ready with no worker claiming it.'
    tui = {'text': line, 'local_automation_v1': {'identity': identity, 'kanban_cards': [{**card, 'text': line}]}}
    kanban_wake_fold.refresh_claimed(tui)
    assert tui['text'].startswith(f'[kanban] Task {tid} claimed by a worker (running).\nNote: ')
    assert tui['text'].endswith('Live status: running.')


@pytest.mark.parametrize('suffix', [' (edited)', '-more', '\tx'])
def test_refresh_claimed_leaves_a_row_whose_last_card_tail_runs_on_mid_line_alone(tmp_path, monkeypatch, board, suffix):
    """The stored text has the last card's tail, but that line continues past it: not the shape
    ``_compose`` built, so the text is not guidance-split and rebuilt (t_01a1d02c1e9e0768)."""
    tid = board('waited on')
    _claim(tid)  # the premise no longer holds: a rebuild would relabel it
    body = f'[kanban] Task {tid} is ready with no worker claiming it.\nTitle: x\nBoard: default'
    card = {'task_id': tid, 'board': '', 'kinds': ['chat_wait_unclaimed'], 'text': body}
    identity = json.dumps(['kanban-wake', 'x'])
    for text in (body + suffix, body + suffix + '\n\n' + GUIDANCE):
        payload = {'text': text, 'local_automation_v1': {'identity': identity, 'kanban_cards': [card]}}
        kanban_wake_fold.refresh_claimed(payload)
        assert payload['text'] == text
    # Control: the same row without the run-on suffix is relabelled, so the guard is what holds it.
    payload = {'text': body + '\n\n' + GUIDANCE, 'local_automation_v1': {'identity': identity, 'kanban_cards': [card]}}
    kanban_wake_fold.refresh_claimed(payload)
    assert payload['text'].startswith(f'[kanban] Task {tid} claimed by a worker (running).\n')


def test_the_relabel_reads_the_notifiers_db_under_the_env_pin(tmp_path, monkeypatch, board):
    """A card on a non-default board relabels from the DB the notifier read it from: on a box that pins
    ``HERMES_KANBAN_DB`` that is the pinned file, never a per-slug DB the read would create."""
    tid = board('waited on')
    _claim(tid)
    card = {'task_id': tid, 'board': 'side-board', 'kinds': ['chat_wait_unclaimed'],
            'text': f'[kanban] Task {tid} is ready with no worker claiming it.\nBoard: side-board'}
    side_db = kb.board_own_db_path('side-board')
    refreshed = kanban_wake_fold.refresh_card(card)
    assert refreshed['text'].startswith(f'[kanban] Task {tid} claimed by a worker (running).')
    assert not side_db.exists() and not side_db.parent.exists()


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


@pytest.mark.parametrize('unpushed', [False, True], ids=['pushed', 'unpushed-local-change'])
def test_the_relabel_reads_the_fresher_replica_status(tmp_path, monkeypatch, unpushed):
    """(d) t_01a1d390d19239dd: a live board lagging after a sync stall still read the card ready, so a queued
    ``chat_wait_unclaimed`` wake kept its false premise. The pulled replica holds a newer revision: done.
    With a local change of the card not yet pushed (pending outbox op), the live row is the newer one and stays."""
    import sqlite3

    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_session_activity as ksa
    from hermes_cli import kanban_sync_client as ksc
    from tests.hermes_cli import kanban_sync_support as support
    home = support.kanban_home(tmp_path, monkeypatch)
    for key in ('HERMES_KANBAN_DB', 'HERMES_KANBAN_BOARD'):
        monkeypatch.delenv(key, raising=False)
    support.write_sync_config(home, True)
    ksa._reset_cache_for_tests()
    conn = kbc.connect(home / 'kanban.db')
    try:
        ksc.ensure_identity(conn, 'default', 'macbook')
        sync.set_mode(conn, 'primary')
        tid = kb.create_task(conn, title='waited on', assignee='worker')
        conn.execute('UPDATE tasks SET sync_version = 5 WHERE id = ?', (tid,))
        if not unpushed:
            conn.execute('DELETE FROM kanban_sync_outbox')
        conn.commit()
    finally:
        conn.close()
    path = ksc.shadow_path('default')
    path.parent.mkdir(parents=True, exist_ok=True)
    rep = sqlite3.connect(path)
    rep.executescript(
        'CREATE TABLE kanban_sync_state (cursor INTEGER, pulled_at REAL);'
        'CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, ready_since INTEGER, sync_version INTEGER, '
        'result TEXT);'
        'CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, summary TEXT, started_at INTEGER, '
        'ended_at INTEGER);')
    rep.execute("INSERT INTO tasks VALUES (?, 'done', NULL, 7, NULL)", (tid,))
    rep.execute("INSERT INTO task_runs (task_id, summary, started_at, ended_at) VALUES (?, 'landed PR #1', 1, 2)",
                (tid,))
    rep.commit()
    rep.close()
    card = {'task_id': tid, 'board': '', 'kinds': ['chat_wait_unclaimed'],
            'text': f'[kanban] Task {tid} is ready with no worker claiming it.\nBoard: default'}
    try:
        refreshed = kanban_wake_fold.refresh_card(card)
    finally:
        ksa._reset_cache_for_tests()
    lines = refreshed['text'].split('\n')
    if unpushed:
        assert lines[0] == f'[kanban] Task {tid} is ready with no worker claiming it.'
        assert refreshed['kinds'] == ['chat_wait_unclaimed']
        return
    assert lines[0] == f'[kanban] Task {tid} completed.'
    assert 'Live status: done.' in lines[1]
    assert lines[2] == 'Result: landed PR #1'
    assert refreshed['kinds'] == ['completed']
