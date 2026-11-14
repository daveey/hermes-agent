"""Fold checkpoint custody: pre-claim history, physical lineage and pending observers."""
from threading import RLock
from types import SimpleNamespace

import pytest

from gateway.session_fold_pending import release_checkpointed
from hermes_state import SessionDB
import hermes_state_runtime as rt
from hermes_state_update_pause import checkpoint_session_input, get_session_checkpoint
from tests.gateway.test_unknown_resolution_physical_worker import _local_reset


@pytest.mark.parametrize('user_location', ['at_mark', 'physical_target', 'unrelated'])
def test_checkpoint_settles_only_new_chain_history_and_publishes_pending(tmp_path, user_location):
    """A completed tool suffix alone does not prove the folded user text was saved.

    The user may be in the physical lineage target rather than the logical owner.
    Release must publish the committed FIFO before returning, not rely on a later
    park/claim notification to correct an observer's stale pending list.
    """
    with SessionDB(tmp_path / 'state.db') as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='fold-checkpoint')
        logical = _local_reset(db, epoch, 'logical', 'physical')
        db.create_session('unrelated', source='test')
        old_user = db.append_message(logical, role='user', content='previous turn')
        head = rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id=logical,
                                     request_id='head', payload={'text': 'head'})
        notice = rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id=logical,
                                       request_id='notice', payload={'text': 'folded notice'})
        row = rt.claim_session_input(db, epoch=epoch, session_id=logical)
        assert row['admission_id'] == head['admission_id']
        assert row['transcript_mark'] == old_user
        rt.steer_session_input(db, epoch=epoch, admission_id=notice['admission_id'],
                               generation=row['generation'])
        if user_location != 'at_mark':
            target = 'physical' if user_location == 'physical_target' else 'unrelated'
            db.append_message(target, role='user', content='head\n\nfolded notice')
        db.append_messages_batch('physical', [
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'call'}]},
            {'role': 'tool', 'content': 'done', 'tool_call_id': 'call'},
        ])
        receipt = checkpoint_session_input(
            db, epoch=epoch, admission_id=row['admission_id'], generation=row['generation'],
            physical_session_id='physical', transcript_mark=row['transcript_mark'],
            folded=(notice['admission_id'],))
        assert receipt['physical_chain'] == ['physical']
        assert get_session_checkpoint(db, session_id=logical) == receipt
        settled = rt.get_session_admission(db, admission_id=notice['admission_id'])
        durable = user_location == 'physical_target'
        assert settled['status'] == ('terminal' if durable else 'queued')
        assert settled['outcome'] == ('steered' if durable else None)
        assert settled['seq'] == notice['seq']

        published, acks = [], []
        ref = SimpleNamespace(session_id=logical)

        def publish_pending(published_ref):
            assert published_ref is ref
            published.append([
                item['admission_id'] for item in rt.list_session_admissions(db, session_id=logical)
            ])

        authority = SimpleNamespace(
            db=db, folded_transfers={logical: (row['generation'], {notice['admission_id']})},
            steer_transfers={logical: (row['generation'], {notice['admission_id']: False})},
            _publish_pending=publish_pending,
            _settle_cancelled=lambda ref, admission_id, outcome: acks.append((admission_id, outcome)))
        live = SimpleNamespace(event_stream=SimpleNamespace(lock=RLock()))
        release_checkpointed(authority, live, ref, row)
        expected_pending = [head['admission_id']] + ([] if durable else [notice['admission_id']])
        assert published == [expected_pending]
        assert acks == ([(notice['admission_id'], 'steered')] if durable else [])
        assert not authority.folded_transfers
        assert not authority.steer_transfers
        # Releasing again cannot acknowledge or publish the same transfer twice.
        release_checkpointed(authority, live, ref, row)
        assert published == [expected_pending]
        assert acks == ([(notice['admission_id'], 'steered')] if durable else [])
