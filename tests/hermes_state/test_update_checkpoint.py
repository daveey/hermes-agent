"""Store-only update parking: no execution or gateway transport in these contracts."""
from tests.hermes_state.raw_rows import raw_append_message
import json
import sqlite3

import pytest

from hermes_state import SessionDB
import hermes_state_runtime as rt


@pytest.fixture
def running(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('s', source='test')
        epoch = rt.begin_runtime_epoch(db, instance_id='first')
        rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id='s',
                               request_id='one', payload={'text': 'original'})
        row = rt.claim_session_input(db, epoch=epoch, session_id='s')
        db.append_messages_batch('s', [
            {'role': 'user', 'content': 'original'},
            {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': 'call-a', 'type': 'function', 'function': {'name': 'example', 'arguments': '{}'}}]},
            {'role': 'tool', 'content': 'done', 'tool_call_id': 'call-a', 'tool_name': 'example'},
        ])
        yield db, epoch, row


def park(running, **changes):
    from hermes_state_update_pause import checkpoint_session_input
    db, epoch, row = running
    return checkpoint_session_input(db, **(dict(epoch=epoch, admission_id=row['admission_id'],
        generation=row['generation'], physical_session_id='s', transcript_mark=row['transcript_mark']) | changes))


def test_checkpoint_survives_owner_change_once_and_blocks_fifo(running):
    from hermes_state_update_pause import get_session_checkpoint, resume_session_checkpoint
    db, epoch, row = running
    receipt = park(running)
    assert receipt['admission_id'] == row['admission_id']
    assert receipt['owner_epoch'] == epoch
    assert receipt['generation'] == row['generation']
    assert receipt['physical_session_id'] == 's'
    assert get_session_checkpoint(db, session_id='s') == receipt
    assert rt.get_session_admission(db, admission_id=row['admission_id'])['status'] == 'started'
    rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id='s',
                           request_id='two', payload={'text': 'later'})
    assert rt.claim_session_input(db, epoch=epoch, session_id='s') is None
    epoch = rt.begin_runtime_epoch(db, instance_id='second')
    assert rt.recover_session_inputs(db, epoch=epoch) == 0
    resumed = resume_session_checkpoint(db, epoch=epoch, admission_id=row['admission_id'])
    assert resumed['update_resume'] == receipt
    assert resumed['payload'] == row['payload']
    assert resumed['generation'] == row['generation'] + 1
    assert resumed['owner_epoch'] == epoch
    assert get_session_checkpoint(db, session_id='s') is None
    with pytest.raises(rt.RuntimeStoreError):
        resume_session_checkpoint(db, epoch=epoch, admission_id=row['admission_id'])
    epoch = rt.begin_runtime_epoch(db, instance_id='third')
    assert rt.recover_session_inputs(db, epoch=epoch) == 1
    assert rt.get_session_admission(db, admission_id=row['admission_id'])['status'] == 'unknown'


@pytest.mark.parametrize('sql', [
    "UPDATE messages SET active=0 WHERE role='tool'",
    "UPDATE messages SET active=0 WHERE role='assistant'",
])
def test_inactive_suffix_is_not_a_checkpoint(running, sql):
    db, _, _ = running
    db._execute_write(lambda c: c.execute(sql))
    with pytest.raises(rt.RuntimeStoreError, match='invalid_checkpoint'):
        park(running)


@pytest.mark.parametrize('field,value', [
    ('owner_epoch', 99), ('generation', 99), ('session_id', 'other'),
    ('physical_session_id', 'other'), ('max_message_id', 999),
    ('suffix_digest', 'wrong'), ('lineage_json', '["other"]'), ('version', True),
    ('transcript_mark', True), ('extra', 'unrecognized'),
])
def test_mismatched_receipt_becomes_unknown(running, field, value):
    from hermes_state_update_pause import CHECKPOINT_PREFIX, get_session_checkpoint
    db, _, row = running
    receipt = park(running)
    receipt[field] = value
    db._execute_write(lambda c: c.execute('UPDATE state_meta SET value=? WHERE key=?',
        (json.dumps(receipt), CHECKPOINT_PREFIX + row['admission_id'])))
    assert get_session_checkpoint(db, session_id='s') is None
    epoch = rt.begin_runtime_epoch(db, instance_id='replacement')
    assert rt.recover_session_inputs(db, epoch=epoch) == 1
    assert get_session_checkpoint(db, session_id='s') is None


@pytest.mark.parametrize('raw', [None, '{', '[]', 'null', '"x"', '{"deep":' + '[' * 1500 + '0' + ']' * 1500 + '}'])
def test_missing_or_corrupt_receipt_never_preserves_started(running, raw):
    from hermes_state_update_pause import CHECKPOINT_PREFIX
    db, _, row = running
    if raw is not None:
        db._execute_write(lambda c: c.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
            (CHECKPOINT_PREFIX + row['admission_id'], raw)))
    epoch = rt.begin_runtime_epoch(db, instance_id='replacement')
    assert rt.recover_session_inputs(db, epoch=epoch) == 1
    assert rt.get_session_admission(db, admission_id=row['admission_id'])['status'] == 'unknown'


@pytest.mark.parametrize('mutation', ['append', 'edit', 'generation', 'lineage', 'steering', 'worker'])
def test_changed_checkpoint_is_invalid_before_resume_and_recovery(running, mutation):
    from hermes_state_update_pause import get_session_checkpoint, resume_session_checkpoint
    db, epoch, row = running
    park(running)
    def add_steering():
        steer = rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id='s',
            request_id='steer', payload={'text': 'possibly delivered'}, intent='steer')
        rt.steer_session_input(db, epoch=epoch, admission_id=steer['admission_id'], generation=row['generation'])
    actions = {
        'append': lambda: raw_append_message(db, 's', 'assistant', 'later'),
        'edit': lambda: db._execute_write(lambda c: c.execute("UPDATE messages SET content='different' WHERE role='tool'")),
        'generation': lambda: db._execute_write(lambda c: c.execute(
            "UPDATE sessions SET runtime_generation=runtime_generation+1 WHERE id='s'")),
        'lineage': lambda: db._execute_write(lambda c: c.execute(
            "UPDATE session_admissions SET lineage_json='[\"s\",\"other\"]'")),
        'steering': add_steering,
        'worker': lambda: rt.register_worker_execution(db, epoch=epoch, execution_id='worker', session_id='s',
            generation=row['generation'], kind='compute', adoption_secret='secret'),
    }
    actions[mutation]()
    assert get_session_checkpoint(db, session_id='s') is None
    with pytest.raises(rt.RuntimeStoreError, match='invalid_checkpoint'):
        resume_session_checkpoint(db, epoch=epoch, admission_id=row['admission_id'])
    epoch = rt.begin_runtime_epoch(db, instance_id='replacement')
    assert rt.recover_session_inputs(db, epoch=epoch) == 1


@pytest.mark.parametrize('mutation', ['missing', 'wrong_id', 'duplicate', 'no_calls', 'malformed', 'stale_mark', 'huge', 'too_many'])
def test_checkpoint_requires_complete_bounded_new_tool_suffix(running, mutation):
    db, _, _ = running
    changes = {}
    statements = {
        'missing': ("DELETE FROM messages WHERE role='tool'", ()),
        'wrong_id': ("UPDATE messages SET tool_call_id='other' WHERE role='tool'", ()),
        'no_calls': ("UPDATE messages SET tool_calls=NULL WHERE role='assistant'", ()),
        'malformed': ("UPDATE messages SET tool_calls='{' WHERE role='assistant'", ()),
        'huge': ("UPDATE messages SET content=? WHERE role='tool'", ('x' * (4 * 1024 * 1024),)),
    }
    if mutation in statements:
        db._execute_write(lambda c: c.execute(*statements[mutation]))
    if mutation == 'duplicate':
        db.append_message('s', role='tool', content='duplicate', tool_call_id='call-a')
    if mutation == 'too_many':
        db.append_messages_batch('s', [{'role': 'tool', 'content': 'done', 'tool_call_id': f'call-{i}'} for i in range(130)])
    if mutation == 'stale_mark':
        changes['transcript_mark'] = db._read_one('SELECT MAX(id) FROM messages')[0]
    with pytest.raises(rt.RuntimeStoreError, match='invalid_checkpoint'):
        park(running, **changes)


def test_checkpoint_rejects_steering_and_workers_at_creation(running):
    db, epoch, row = running
    rt.register_worker_execution(db, epoch=epoch, execution_id='worker', session_id='s',
        generation=row['generation'], kind='compute', adoption_secret='secret')
    with pytest.raises(rt.RuntimeStoreError, match='invalid_checkpoint'):
        park(running)
    db._execute_write(lambda c: c.execute("UPDATE worker_executions SET status='terminal'"))
    steer = rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id='s',
        request_id='steer', payload={'text': 'maybe delivered'}, intent='steer')
    rt.steer_session_input(db, epoch=epoch, admission_id=steer['admission_id'], generation=row['generation'])
    with pytest.raises(rt.RuntimeStoreError, match='invalid_checkpoint'):
        park(running)


def test_settlement_deletes_only_its_receipt_and_unblocks_fifo(running):
    from hermes_state_update_pause import CHECKPOINT_PREFIX, get_session_checkpoint
    db, epoch, row = running
    park(running)
    db._execute_write(lambda c: c.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
        (CHECKPOINT_PREFIX + 'unrelated', '{}')))
    rt.settle_session_input(db, epoch=epoch, admission_id=row['admission_id'], generation=row['generation'], outcome='interrupted')
    assert get_session_checkpoint(db, session_id='s') is None
    assert db._read_one('SELECT value FROM state_meta WHERE key=?', (CHECKPOINT_PREFIX + row['admission_id'],)) is None
    assert db._read_one('SELECT value FROM state_meta WHERE key=?', (CHECKPOINT_PREFIX + 'unrelated',))[0] == '{}'


def test_checkpoint_and_resume_writes_rollback_atomically(running):
    from hermes_state_update_pause import get_session_checkpoint, resume_session_checkpoint
    db, epoch, row = running
    db._execute_write(lambda c: c.execute("CREATE TRIGGER refuse_checkpoint BEFORE INSERT ON state_meta BEGIN SELECT RAISE(ABORT,'checkpoint fixture'); END"))
    with pytest.raises(sqlite3.IntegrityError, match='checkpoint fixture'):
        park(running)
    assert get_session_checkpoint(db, session_id='s') is None
    db._execute_write(lambda c: c.execute('DROP TRIGGER refuse_checkpoint'))
    receipt = park(running)
    assert park(running) == receipt
    db._execute_write(lambda c: c.execute("CREATE TRIGGER refuse_consume BEFORE DELETE ON state_meta BEGIN SELECT RAISE(ABORT,'consume fixture'); END"))
    with pytest.raises(sqlite3.IntegrityError, match='consume fixture'):
        resume_session_checkpoint(db, epoch=epoch, admission_id=row['admission_id'])
    assert get_session_checkpoint(db, session_id='s') == receipt
    assert rt.get_session_admission(db, admission_id=row['admission_id'])['generation'] == row['generation']
    assert db.get_session('s')['runtime_generation'] == row['generation']


def test_resume_obeys_transactional_gate_and_stale_owner_fence(running):
    from hermes_state_update_pause import get_session_checkpoint, resume_session_checkpoint
    db, epoch, row = running
    receipt = park(running)
    def refused(conn):
        raise rt.RuntimeStoreError('admission_closed')
    with pytest.raises(rt.RuntimeStoreError, match='admission_closed'):
        resume_session_checkpoint(db, epoch=epoch, admission_id=row['admission_id'], _guard=refused)
    assert get_session_checkpoint(db, session_id='s') == receipt
    new_epoch = rt.begin_runtime_epoch(db, instance_id='new')
    with pytest.raises(rt.RuntimeStoreError, match='stale_epoch'):
        park(running)
    with pytest.raises(rt.RuntimeStoreError, match='stale_epoch'):
        resume_session_checkpoint(db, epoch=epoch, admission_id=row['admission_id'])
    resumed = resume_session_checkpoint(db, epoch=new_epoch, admission_id=row['admission_id'])
    with pytest.raises(rt.RuntimeStoreError, match='stale_generation'):
        rt.settle_session_input(db, epoch=new_epoch, admission_id=row['admission_id'], generation=row['generation'], outcome='completed')
    assert resumed['generation'] > row['generation']


def test_local_logical_owner_uses_validated_physical_target(tmp_path):
    from tests.gateway.test_unknown_resolution_physical_worker import _local_reset
    from hermes_state_update_pause import checkpoint_session_input, get_session_checkpoint, resume_session_checkpoint
    with SessionDB(tmp_path / 'local.db') as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='local')
        logical = _local_reset(db, epoch, 'logical', 'physical')
        rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id=logical,
            request_id='turn', payload={'text': 'original', 'local_automation_v1': {'target': 'physical'}})
        row = rt.claim_session_input(db, epoch=epoch, session_id=logical)
        db.append_messages_batch('physical', [
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'a'}]},
            {'role': 'tool', 'content': 'done', 'tool_call_id': 'a'},
        ])
        args = dict(epoch=epoch, admission_id=row['admission_id'], generation=row['generation'],
                    transcript_mark=row['transcript_mark'])
        with pytest.raises(rt.RuntimeStoreError, match='invalid_checkpoint'):
            checkpoint_session_input(db, physical_session_id=logical, **args)
        # A separate physical fence must never be wound backwards by resume.
        db._execute_write(lambda c: c.execute("UPDATE sessions SET runtime_generation=7 WHERE id='physical'"))
        with pytest.raises(rt.RuntimeStoreError, match='invalid_checkpoint'):
            checkpoint_session_input(db, physical_session_id='physical', **args)
        db._execute_write(lambda c: c.execute("UPDATE sessions SET runtime_generation=0 WHERE id='physical'"))
        receipt = checkpoint_session_input(db, physical_session_id='physical', **args)
        assert receipt['session_id'] == logical
        assert receipt['physical_generation'] == 0
        assert get_session_checkpoint(db, session_id=logical) == receipt
        assert get_session_checkpoint(db, session_id='physical') is None
        epoch = rt.begin_runtime_epoch(db, instance_id='replacement')
        assert rt.recover_session_inputs(db, epoch=epoch) == 0
        result = resume_session_checkpoint(db, epoch=epoch, admission_id=row['admission_id'])
        assert result['payload'] == row['payload']
        assert db.get_session('physical')['runtime_generation'] == result['generation']
        assert db.get_session(logical)['runtime_generation'] == result['generation']


def test_physical_generation_change_invalidates_receipt(running):
    # Same-session case also proves the normal generation fence; the local test
    # above requires a distinct physical-generation binding in the proof itself.
    receipt = park(running)
    assert receipt['physical_generation'] == receipt['generation']


@pytest.mark.parametrize('physical_change', ['generation', 'tip'])
def test_local_physical_binding_revalidated_at_resume(tmp_path, physical_change):
    from tests.gateway.test_unknown_resolution_physical_worker import _local_reset

    from hermes_state_update_pause import checkpoint_session_input, get_session_checkpoint, resume_session_checkpoint
    with SessionDB(tmp_path / 'local.db') as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='local')
        logical = _local_reset(db, epoch, 'logical', 'physical')
        rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id=logical,
            request_id='turn', payload={'text': 'original'})
        row = rt.claim_session_input(db, epoch=epoch, session_id=logical)
        db.append_messages_batch('physical', [
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'a'}]},
            {'role': 'tool', 'content': 'done', 'tool_call_id': 'a'},
        ])
        checkpoint_session_input(db, epoch=epoch, admission_id=row['admission_id'],
            generation=row['generation'], transcript_mark=row['transcript_mark'], physical_session_id='physical')
        if physical_change == 'generation':
            db._execute_write(lambda c: c.execute("UPDATE sessions SET runtime_generation=7 WHERE id='physical'"))
        else:
            assert db.try_acquire_compression_lock('physical', 'fixture-holder')
            db.publish_compression_child(parent_session_id='physical', child_session_id='new-tip',
                source='cli', messages=[{'role': 'assistant', 'content': 'summary'}],
                compression_lock_holder='fixture-holder')
        assert get_session_checkpoint(db, session_id=logical) is None
        with pytest.raises(rt.RuntimeStoreError, match='invalid_checkpoint'):
            resume_session_checkpoint(db, epoch=epoch, admission_id=row['admission_id'])
        epoch = rt.begin_runtime_epoch(db, instance_id='replacement')
        assert rt.recover_session_inputs(db, epoch=epoch) == 1


def test_restart_uses_durable_receipt_not_memory(running):
    from hermes_state_update_pause import get_session_checkpoint, resume_session_checkpoint
    db, _, row = running
    path = db.db_path
    receipt = park(running)
    db.close()
    with SessionDB(path) as reopened:
        epoch = rt.begin_runtime_epoch(reopened, instance_id='reopened')
        assert rt.recover_session_inputs(reopened, epoch=epoch) == 0
        assert get_session_checkpoint(reopened, session_id='s') == receipt
        assert resume_session_checkpoint(reopened, epoch=epoch, admission_id=row['admission_id'])['update_resume'] == receipt


def test_multiple_tool_results_complete_suffix_is_supported(running):
    db, _, _ = running
    calls = [{'id': 'call-a'}, {'id': 'call-b'}]
    db._execute_write(lambda c: c.execute("UPDATE messages SET tool_calls=? WHERE role='assistant'", (json.dumps(calls),)))
    db.append_message('s', role='tool', content='second done', tool_call_id='call-b')
    assert park(running)['max_message_id'] == db._read_one('SELECT MAX(id) FROM messages')[0]


def test_old_binary_recovery_remains_fail_safe(running):
    from hermes_state_update_pause import get_session_checkpoint, resume_session_checkpoint
    db, _, row = running
    park(running)
    epoch = rt.begin_runtime_epoch(db, instance_id='old-binary')
    # Exact pre-feature started -> unknown recovery statement, ignoring state_meta.
    db._execute_write(lambda c: c.execute("UPDATE session_admissions SET status='unknown' WHERE status='started' AND owner_epoch!=?", (epoch,)))
    assert get_session_checkpoint(db, session_id='s') is None
    with pytest.raises(rt.RuntimeStoreError, match='invalid_checkpoint'):
        resume_session_checkpoint(db, epoch=epoch, admission_id=row['admission_id'])
