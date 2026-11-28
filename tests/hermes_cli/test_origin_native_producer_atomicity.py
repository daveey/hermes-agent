"""Native source creation must not silently strand a future worker's children."""
import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_origin_admission as admission, kanban_origin_native as native
from tests.hermes_cli.test_origin_native_admission import turn as turn  # noqa: PLC0414 -- pytest fixture reexport
from tests.hermes_cli.test_origin_native_admission import snapshot


@pytest.mark.parametrize('failure', ['timeout', 'revoked'])
def test_source_receipt_failure_rolls_back_and_retry_can_forward(turn, monkeypatch, failure):
    from agent import origin_observation_budget as budget
    from hermes_cli.kanban_origin_forward import authorize

    captured = native.capture(None)
    assert captured is not None
    original = native.record

    @budget.bounded_record
    def interrupted(*args):
        if failure == 'timeout':
            raise TimeoutError('origin_observation_timeout')
        # Otherwise recording declined an observation without raising.

    with kbc.connect_closing() as conn:
        before = snapshot(conn)
        monkeypatch.setattr(native, 'record', interrupted)
        with pytest.raises(admission.AdmissionRefused, match='native receipt'):
            kb.create_task(conn, title='native source', idempotency_key='producer-key', native_origin=captured)
        assert snapshot(conn) == before
        monkeypatch.setattr(native, 'record', original)
        source = kb.create_task(conn, title='native source', idempotency_key='producer-key', native_origin=captured)
        binding = native._receipt(conn, source)
        assert binding is not None
        origin, owner = binding['origin'], binding['owner']
        accepted = authorize(ledger=conn_db_absent(), origin=origin, creator=owner,
            gateway='here', profile='default', sender='gw:worker-a3', run_uid='live-run',
            lease={'task_id': source, 'holder': 'worker-a3', 'run_uid': 'live-run', 'expires': 2000},
            now_ms=1000, session_exists=lambda sid: sid == origin['session'],
            native_lookup=lambda supplied_origin, supplied_owner: native.lookup(
                supplied_origin, supplied_owner, gateway='here'))
        assert accepted == binding


def test_real_tool_revocation_rolls_back_source(turn, monkeypatch):
    from tests.hermes_cli.test_origin_native_admission import create
    original = native.record

    def revoke(conn, task_id, captured, created_at):
        turn.release()
        return original(conn, task_id, captured, created_at)

    monkeypatch.setattr(native, 'record', revoke)
    with kbc.connect_closing() as conn:
        before = snapshot(conn)
    result = create()
    assert not result.get('ok'), result
    assert 'native receipt' in str(result)
    with kbc.connect_closing() as conn:
        assert snapshot(conn) == before


def test_idempotent_unreceipted_source_requires_adoption(turn):
    captured = native.capture(None)
    with kbc.connect_closing() as conn:
        source = kb.create_task(conn, title='legacy source', idempotency_key='legacy-key')
        before = snapshot(conn)
        with pytest.raises(admission.AdmissionRefused, match='native receipt'):
            kb.create_task(conn, title='legacy source', idempotency_key='legacy-key', native_origin=captured)
        assert snapshot(conn) == before
        assert native._receipt(conn, source) is None


@pytest.mark.parametrize('change', ['missing', 'origin', 'owner', 'primary'])
def test_receipt_commit_guard_rejects_each_mismatch(turn, monkeypatch, change):
    captured = native.capture(None)
    with kbc.connect_closing() as conn:
        source = kb.create_task(conn, title='guard source', native_origin=captured)
        binding = native._receipt(conn, source)
        assert binding is not None
        scope = native._primary_scope(conn)
        if change == 'missing':
            binding = None
        elif change == 'origin':
            binding['origin'] = dict(binding['origin'], session='different-chat')
        elif change == 'owner':
            binding['owner'] = dict(binding['owner'], task_id='different-task')
        else:
            monkeypatch.setattr(native, '_primary_scope', lambda db: None)
        monkeypatch.setattr(native, '_receipt', lambda db, task: binding)
        with pytest.raises(admission.AdmissionRefused, match='native receipt'):
            native.require_created_receipt(conn, source, captured, scope=scope)


def test_legacy_plain_context_does_not_mint_receipt(turn):
    captured = native.capture(None)
    assert captured is not None
    with kbc.connect_closing() as conn:
        source = kb.create_task(conn, title='not a capability', native_origin=dict(captured))
        assert native._receipt(conn, source) is None


def conn_db_absent():
    from hermes_constants import get_hermes_home
    return get_hermes_home() / 'kanban/absent-origin-ledger.db'
