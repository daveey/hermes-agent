"""Selective FIFO eligibility is transactional and never bypasses execution fences."""
import json

import pytest

from gateway.session_notifier_drain import eligible, notifier_wake
from hermes_state import SessionDB
import hermes_state_runtime as rt


def wake_payload(identity, local=False):
    descriptor = {'identity': identity}
    return {'local_automation_v1': descriptor} if local else {'native_text_v1': {'automation': descriptor}}


@pytest.mark.parametrize('local', [False, True])
def test_exact_head_hold_flip_and_restart(monkeypatch, tmp_path, local):
    from agent import core_update_drain
    held = [True]
    monkeypatch.setattr(core_update_drain, 'active_hold', lambda: {} if held[0] else None)
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        db.create_session('s', source='test')
        epoch = rt.begin_runtime_epoch(db, instance_id='first')
        identity = json.dumps(['kanban-wake', 'route', 'one'])
        wake = rt.admit_session_input(db, epoch=epoch, session_id='s', principal_id='automation:route',
                                     request_id=identity, payload=wake_payload(identity, local))
        human = rt.admit_session_input(db, epoch=epoch, session_id='s', principal_id='human',
                                      request_id='human', payload={'text': identity})
        args = dict(epoch=epoch, session_id='s', _eligible=eligible)
        # A wake preflight completed before the hold: it cannot authorize the human behind it.
        assert rt.claim_session_input(db, **args, head=wake['admission_id']) is False
        # A human preflight completed while held, then the hold cleared: FIFO is restored.
        held[0] = False
        assert rt.claim_session_input(db, **args, head=human['admission_id']) is False
        held[0] = True
        claimed = rt.claim_session_input(db, **args, head=human['admission_id'])
        assert claimed['admission_id'] == human['admission_id']
        assert rt.claim_session_input(db, **args) is None  # started input always fences
        rt.settle_session_input(db, epoch=epoch, admission_id=claimed['admission_id'],
                                generation=claimed['generation'], outcome='completed')
        db.close()
        db = SessionDB(db_path=tmp_path / 'state.db')
        epoch = rt.begin_runtime_epoch(db, instance_id='replacement')
        assert rt.recover_session_inputs(db, epoch=epoch) == 0
        args['epoch'] = epoch
        assert rt.claim_session_input(db, **args) is None
        assert rt.get_session_admission(db, admission_id=wake['admission_id'])['status'] == 'queued'
        held[0] = False
        claimed = rt.claim_session_input(db, **args)
        assert claimed['admission_id'] == wake['admission_id']
        rt.settle_session_input(db, epoch=epoch, admission_id=claimed['admission_id'],
                                generation=claimed['generation'], outcome='completed')
        assert rt.claim_session_input(db, **args) is None
    finally:
        db.close()


@pytest.mark.parametrize('kind', ['human', 'other-route', 'other-producer', 'mismatch', 'malformed', 'shape'])
def test_non_notifier_inputs_never_filtered(kind, monkeypatch):
    from agent import core_update_drain
    monkeypatch.setattr(core_update_drain, 'active_hold', lambda: {'pid': 1})
    identity = json.dumps(['kanban-wake', 'route', 'one'])
    row = {'principal_id': 'automation:route', 'request_id': identity, 'payload': wake_payload(identity)}
    identities = {'other-producer': json.dumps(['loop-tick', 'route', 'one']),
                  'malformed': 'not json', 'shape': json.dumps({'kanban-wake': True})}
    mutations: dict[str, dict] = {'human': {'principal_id': 'human'},
                 'other-route': {'principal_id': 'automation:other'},
                 'mismatch': {'request_id': 'other'}}
    mutations.update({key: {'request_id': value, 'payload': wake_payload(value)}
                      for key, value in identities.items()})
    row.update(mutations[kind])
    assert not notifier_wake(row)
    assert eligible(row)


@pytest.mark.parametrize('root_index', [0, 1])
def test_window_fence_without_drain_marker(tmp_path, monkeypatch, root_index):
    from agent import core_update_drain
    from gateway.session_notifier_drain import held
    roots = [tmp_path / 'installation' / 'core-update', tmp_path / 'profile' / 'core-update']
    monkeypatch.setattr(core_update_drain, 'hold_paths', lambda: [root / 'drain-hold' for root in roots])
    assert not held()
    marker = roots[root_index] / 'holds' / 'hermes-sync'
    marker.parent.mkdir(parents=True)
    marker.touch()
    assert core_update_drain.active_hold() is None
    assert held()
    marker.unlink()
    assert not held()
