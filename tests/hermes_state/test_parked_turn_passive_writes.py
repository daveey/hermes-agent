"""t_01a1d6f94ac774a2: a passive row may not land in a chat whose turn is parked for an update.

The update checkpoint proves its parked turn by "nothing was written after its tool suffix". A
parked turn holds no turn lease, so passive writers (Kanban notices, completion notes, detached
results, legacy prompt rows) used to append freely and void the proof: the turn was dropped on
resume (7 of 7 on David-MBP 2026-11-13). Each passive path now gets the same retryable "busy"
refusal a live turn gives, the checkpoint stays valid, and the deferred row lands after the resume.
"""
import json

import pytest

import hermes_state_runtime as rt
from hermes_state import SessionDB
from hermes_state_errors import SessionTurnLeaseLostError


@pytest.fixture
def parked(tmp_path):
    from hermes_state_update_pause import checkpoint_session_input
    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('s', source='test')
        db.create_session('other', source='test')
        epoch = rt.begin_runtime_epoch(db, instance_id='first')
        rt.admit_session_input(db, epoch=epoch, principal_id='automation:kanban', session_id='s',
                               request_id='one', payload={'text': 'notice batch'})
        row = rt.claim_session_input(db, epoch=epoch, session_id='s')
        db.append_messages_batch('s', [
            {'role': 'user', 'content': 'notice batch'},
            {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': 'call-a', 'type': 'function', 'function': {'name': 'example', 'arguments': '{}'}}]},
            {'role': 'tool', 'content': 'done', 'tool_call_id': 'call-a', 'tool_name': 'example'},
        ])
        checkpoint_session_input(db, epoch=epoch, admission_id=row['admission_id'], generation=row['generation'],
                                 physical_session_id='s', transcript_mark=row['transcript_mark'])
        yield db, epoch, row


def _kanban_notice(db, sid, n=1):
    return db.append_kanban_notice(sid, f'card {n} done', {'notice_id': f'default:t_{n}:0-1',
                                                          'kanban_event_keys': [f'default:{n}']})


def _record_kanban_notice(db, sid, n=2):
    return db.record_kanban_notice(sid, f'card {n} done', {'notice_id': f'default:t_{n}:0-1'})[0]


def _completion_note(db, sid):
    return db.append_completion_note(sid, 'adm-x', 'process finished', 'completion_note', {})


def _delegation(db, sid):
    return db.append_delegation_delivery(sid, 'delegate answered', {'delegation_id': 'd-1'})


def _legacy_prompt(db, sid):
    from hermes_state_prompt_admission import admit_prompt, prompt_digest
    return admit_prompt(db, sid, 'op-1', 'typed while parked', prompt_digest(sid, 'typed while parked'))


PASSIVE = {'append_kanban_notice': _kanban_notice, 'record_kanban_notice': _record_kanban_notice,
           'append_completion_note': _completion_note, 'append_delegation_delivery': _delegation,
           'admit_prompt': _legacy_prompt}


@pytest.mark.parametrize('write', PASSIVE.values(), ids=PASSIVE.keys())
def test_passive_write_defers_and_parked_turn_resumes_then_row_lands(parked, write):
    from hermes_state_update_pause import get_session_checkpoint, resume_session_checkpoint
    db, epoch, row = parked
    before = db.get_messages('s')
    with pytest.raises(SessionTurnLeaseLostError, match='parked for an update'):
        write(db, 's')
    assert db.get_messages('s') == before
    assert get_session_checkpoint(db, session_id='s') is not None, 'the refused write must not void the proof'
    # A new owner (the post-update gateway) keeps the valid checkpoint and resumes it.
    epoch = rt.begin_runtime_epoch(db, instance_id='second')
    assert rt.recover_session_inputs(db, epoch=epoch) == 0
    resumed = resume_session_checkpoint(db, epoch=epoch, admission_id=row['admission_id'])
    assert resumed['update_resume']['admission_id'] == row['admission_id']
    # The retried passive row is delivered once the turn is no longer parked.
    write(db, 's')
    assert len(db.get_messages('s')) == len(before) + 1


def test_destructive_user_rewrite_is_refused_while_parked(parked):
    from hermes_state_update_pause import get_session_checkpoint
    db, _, _ = parked
    with pytest.raises(SessionTurnLeaseLostError):
        db.replace_messages('s', [{'role': 'user', 'content': 'rewritten'}], active_only=True,
                            reject_active_turn_lease=True)
    assert get_session_checkpoint(db, session_id='s') is not None


def test_other_chats_and_unparked_turns_still_take_passive_rows(parked):
    from hermes_state_update_pause import CHECKPOINT_PREFIX
    db, _, row = parked
    assert _kanban_notice(db, 'other') > 0  # a different conversation is not parked
    # A fenced (unknown) head is no longer parked: its receipt is gone, the chat takes rows again.
    db._execute_write(lambda c: c.execute('DELETE FROM state_meta WHERE key=?', (CHECKPOINT_PREFIX + row['admission_id'],)))
    assert _kanban_notice(db, 's') > 0


def test_settled_admission_with_leftover_receipt_does_not_fence(parked):
    db, _, row = parked
    db._execute_write(lambda c: c.execute("UPDATE session_admissions SET status='terminal',outcome='interrupted' "
                                          "WHERE admission_id=?", (row['admission_id'],)))
    assert _kanban_notice(db, 's') > 0


def test_live_turn_owner_write_is_not_refused(parked):
    """Only writers without the turn lease are passive: the lease holder's own rows are not fenced here."""
    db, _, _ = parked
    db._execute_write(lambda c: c.execute(
        "INSERT INTO session_turn_leases(conversation_id, holder, acquired_at, expires_at) "
        "VALUES('s','resumed',0,?)", (2e10,)))
    assert db.append_message('s', 'assistant', 'resumed answer', turn_lease_holder='resumed') > 0


@pytest.mark.parametrize('field', ['physical_session_id', 'physical_chain'])
def test_receipt_physical_tip_is_fenced(parked, field):
    """The notice writers address the conversation's physical tip, which can differ from the logical target."""
    from hermes_state_update_pause import CHECKPOINT_PREFIX
    db, _, row = parked
    db.create_session('tip', source='test')
    key = CHECKPOINT_PREFIX + row['admission_id']
    receipt = json.loads(db._execute_write(lambda c: c.execute('SELECT value FROM state_meta WHERE key=?',
                                                               (key,)).fetchone()[0]))
    receipt['physical_session_id'] = 'tip' if field == 'physical_session_id' else 's'
    receipt['physical_chain'] = ['s', 'tip'] if field == 'physical_chain' else ['s']
    db._execute_write(lambda c: c.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(receipt), key)))
    with pytest.raises(SessionTurnLeaseLostError):
        _kanban_notice(db, 'tip')


@pytest.mark.parametrize('raw', ['{', '[]', 'x' * 9000], ids=['truncated', 'list', 'oversized'])
def test_unreadable_receipt_still_fences_its_logical_target(parked, raw):
    from hermes_state_update_pause import CHECKPOINT_PREFIX
    db, _, row = parked
    db._execute_write(lambda c: c.execute('UPDATE state_meta SET value=? WHERE key=?',
                                          (raw, CHECKPOINT_PREFIX + row['admission_id'])))
    with pytest.raises(SessionTurnLeaseLostError):
        _kanban_notice(db, 's')
    assert _kanban_notice(db, 'other') > 0


def test_neighbouring_state_meta_keys_are_not_receipts(parked):
    """The receipt scan is a key range; a key just past the prefix range is not a checkpoint."""
    from hermes_state_update_pause import CHECKPOINT_PREFIX, parked_update_turn
    db, _, row = parked
    db._execute_write(lambda c: c.execute('DELETE FROM state_meta WHERE key=?', (CHECKPOINT_PREFIX + row['admission_id'],)))
    db._execute_write(lambda c: c.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                                          (CHECKPOINT_PREFIX[:-1] + '/' + row['admission_id'], '{}')))
    with db._read_ctx() as conn:
        assert parked_update_turn(conn, ['s']) is None
