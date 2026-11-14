"""t_01a1d6f94ac774a2: every real passive appender keeps its row across a turn parked for an update.

The state layer now refuses a plain ``append_message`` / ``append_messages_batch`` while a started turn is
parked (``SessionTurnLeaseLostError``). Each caller below parks a real update checkpoint, is invoked for
real, and must (a) land no row while parked (the proof stays valid), (b) lose nothing, and (c) deliver the
row exactly once after the resume, through the transcript spool that already survives a restart.
"""
import json
import threading
import time
import types

import pytest

import hermes_state_runtime as rt
from gateway import shutdown_flush
from hermes_state_update_pause import get_session_checkpoint, resume_session_checkpoint
from tests.hermes_state import test_parked_turn_passive_writes as _passive

parked = _passive.parked  # the real parked checkpoint fixture, shared rather than copied


@pytest.fixture
def spool_home(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    import hermes_constants
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: tmp_path / 'home', raising=False)
    (tmp_path / 'home').mkdir()
    return tmp_path / 'home'


def _spool_files():
    return list(shutdown_flush._get_flush_dir().glob('pending-*.json'))


def _resume(db, row):
    epoch = rt.begin_runtime_epoch(db, instance_id='after-update')
    assert rt.recover_session_inputs(db, epoch=epoch) == 0
    resume_session_checkpoint(db, epoch=epoch, admission_id=row['admission_id'])
    assert get_session_checkpoint(db, session_id='s') is None


def _contents(db, **where):
    return [m['content'] for m in db.get_messages('s') if all(m.get(k) == v for k, v in where.items())]


def _assert_proof_intact(db):
    assert get_session_checkpoint(db, session_id='s') is not None


def test_mirror_defers_to_spool_then_lands_once(parked, spool_home, monkeypatch):
    import hermes_state_registry
    from gateway.mirror import mirror_to_session
    db, _, row = parked
    monkeypatch.setattr(hermes_state_registry, 'acquire', lambda: db)
    monkeypatch.setattr(hermes_state_registry, 'release_or_close', lambda d: None)
    before = db.get_messages('s')
    assert mirror_to_session('telegram', '1', 'cron says hi', session_id='s', role='user') is True
    assert db.get_messages('s') == before and len(_spool_files()) == 1
    _assert_proof_intact(db)
    assert shutdown_flush.replay_deferred_rows(db, 's') == (0, 1)  # still parked: nothing lost, nothing landed
    assert len(_spool_files()) == 1
    _resume(db, row)
    assert shutdown_flush.replay_deferred_rows(db, 's') == (1, 0)
    assert _contents(db).count('cron says hi') == 1 and not _spool_files()
    assert shutdown_flush.replay_deferred_rows(db, 's') == (0, 0)


def test_mirror_without_a_spool_reports_the_refusal_not_success(parked, spool_home, monkeypatch):
    import hermes_state_registry
    from gateway.mirror import mirror_to_session
    db, _, _ = parked
    monkeypatch.setattr(hermes_state_registry, 'acquire', lambda: db)
    monkeypatch.setattr(hermes_state_registry, 'release_or_close', lambda d: None)
    monkeypatch.setattr(shutdown_flush, 'spool_dropped_transcript_message', lambda *a: None)
    assert mirror_to_session('telegram', '1', 'x', session_id='s') is False
    assert 'x' not in _contents(db)


def test_gateway_store_defers_without_counting_a_failure_then_drains(parked, spool_home, monkeypatch):
    from gateway.session import SessionStore
    db, _, row = parked
    store = object.__new__(SessionStore)
    store._db = db
    store._transcript_retry_lock = threading.Lock()
    store._dirty_transcripts = {}
    store._transcript_append_failures = {}
    monkeypatch.setattr(store, '_db_for_session_id', lambda sid: db, raising=False)
    store.append_to_transcript('s', {'role': 'user', 'content': 'retry me'})
    assert 'retry me' not in _contents(db)
    assert store._transcript_append_failures.get('s', 0) == 0, 'lease busy must not feed the stall escalation'
    assert store._dirty_transcripts.get('s', []) == [] and len(_spool_files()) == 1
    _assert_proof_intact(db)
    store.drain_deferred_transcript('s')  # still parked: refused again, files kept
    assert len(_spool_files()) == 1 and 'retry me' not in _contents(db)
    _resume(db, row)
    store.drain_deferred_transcript('s')
    assert _contents(db).count('retry me') == 1 and not _spool_files()


def test_gateway_store_keeps_order_across_deferred_rows(parked, spool_home, monkeypatch):
    from gateway.session import SessionStore
    db, _, row = parked
    store = object.__new__(SessionStore)
    store._db = db
    store._transcript_retry_lock = threading.Lock()
    store._dirty_transcripts = {}
    store._transcript_append_failures = {}
    monkeypatch.setattr(store, '_db_for_session_id', lambda sid: db, raising=False)
    for text in ('first', 'second'):
        store.append_to_transcript('s', {'role': 'user', 'content': text})
    _resume(db, row)
    store.append_to_transcript('s', {'role': 'user', 'content': 'third'})  # the next live append drains first
    assert _contents(db)[-3:] == ['first', 'second', 'third'] and not _spool_files()


def test_bot_dm_reply_is_consumed_then_deferred_and_lands_after_resume(parked, spool_home, monkeypatch):
    from tools import bot_mode_dm
    from tools import process_registry_notifications as notes
    from tools.process_registry import process_registry
    db, _, row = parked
    done = threading.Event()
    proc = types.SimpleNamespace(_completion_event=done)
    monkeypatch.setattr(process_registry, 'get', lambda pid: proc)
    monkeypatch.setattr(process_registry, 'is_completion_consumed', lambda pid: False)
    monkeypatch.setattr(process_registry, '_exit_snapshot', lambda p, s: {'status': s})
    monkeypatch.setattr(notes, 'format_process_notification', lambda evt: 'REPLY FROM RECIPIENT')
    monkeypatch.setattr(notes, 'process_completion_display_text', lambda evts: 'reply')
    agent = types.SimpleNamespace(_session_db=db, session_id='s')
    assert bot_mode_dm._persist_reply_when_done('proc-1', agent) is True
    done.set()
    deadline = time.time() + 10
    while not _spool_files() and time.time() < deadline:
        time.sleep(0.05)
    assert len(_spool_files()) == 1 and 'REPLY FROM RECIPIENT' not in _contents(db)
    _assert_proof_intact(db)
    _resume(db, row)
    assert shutdown_flush.replay_deferred_rows(db, 's') == (1, 0)
    assert _contents(db, display_kind='process_complete') == ['REPLY FROM RECIPIENT']


def test_shutdown_spool_replay_holds_back_while_parked_then_recovers(parked, spool_home):
    db, _, row = parked
    assert shutdown_flush.spool_dropped_transcript_message('s', {'role': 'user', 'content': 'spooled at shutdown'})
    recovered, held_back = shutdown_flush.recover_pending_spool(db)
    assert (recovered, held_back) == (0, {'s'}) and len(_spool_files()) == 1
    assert 'spooled at shutdown' not in _contents(db)
    _assert_proof_intact(db)
    _resume(db, row)
    assert shutdown_flush.recover_pending_spool(db) == (1, set()) and not _spool_files()
    assert _contents(db).count('spooled at shutdown') == 1


def test_shutdown_pending_message_moves_to_the_transcript_spool_while_parked(parked, spool_home):
    """A pending-message file is not drained after boot, so it must not sit as one: it becomes a spool row."""
    db, _, row = parked
    payload = {'session_key': 'agent:main:x', 'ts': 1700000000, 'data': {'text': 'typed at shutdown', 'session_id': 's'}}
    shutdown_flush._write_payload(shutdown_flush._get_flush_dir(), payload)
    recovered, _held_back = shutdown_flush.recover_pending_spool(db)
    assert recovered == 1 and 'typed at shutdown' not in _contents(db)
    files = _spool_files()
    assert len(files) == 1
    saved = json.loads(files[0].read_text())
    assert saved['reason'] == shutdown_flush.TRANSCRIPT_CAP_DROP_REASON and saved['data']['session_id'] == 's'
    _resume(db, row)
    assert shutdown_flush.replay_deferred_rows(db, 's') == (1, 0)
    assert _contents(db).count('typed at shutdown') == 1


def _tui_session(db, key='s'):
    return {'agent': types.SimpleNamespace(_session_db=db, session_id=key), 'history': [], 'session_key': key,
            'history_lock': threading.RLock(), 'running': False}


def test_tui_stop_notice_survives_the_parked_turn_and_is_not_written_twice(parked, spool_home):
    from tui_gateway import server
    db, _, row = parked
    session = _tui_session(db)
    session['_stop_issuer'] = 'Desktop'
    entry = server._record_stop_notice(session, 'interrupted')
    assert entry in session['history'] and entry.get('_db_persisted') is not False
    assert _contents(db, display_kind='turn_stopped') == [] and len(_spool_files()) == 1
    _assert_proof_intact(db)
    _resume(db, row)
    assert shutdown_flush.replay_deferred_rows(db, 's') == (1, 0)
    notices = _contents(db, display_kind='turn_stopped')
    assert notices == ['[Turn stopped by Desktop before it finished.]']
    from agent.context_compressor import _DB_PERSISTED_MARKER
    assert entry[_DB_PERSISTED_MARKER] is True, 'the spool owns the row: no flush may write a second copy'


def test_tui_model_switch_marker_is_atomic_when_the_turn_is_parked(parked, spool_home):
    from agent.context_compressor import _DB_PERSISTED_MARKER
    from tui_gateway import server
    db, _, row = parked
    # Seed an older pivot BEFORE the checkpoint-bound assistant/tool suffix.
    db._execute_write(lambda c: c.execute("UPDATE messages SET display_kind='model_switch', content='m1' "
                                          "WHERE session_id='s' AND role='user'"))
    _assert_proof_intact(db)
    session = _tui_session(db)
    active = lambda: [m['content'] for m in db.get_messages('s') if m.get('display_kind') == 'model_switch']
    first = active()
    assert len(first) == 1 and 'm1' in first[0]
    server._append_model_switch_marker(session, model='m2', provider='p')  # parked: refused, nothing half-applied
    assert active() == first, 'the previous pivot must stay active when the new one cannot land'
    live = [h for h in session['history'] if h.get('display_kind') == 'model_switch']
    assert len(live) == 1 and 'm2' in live[0]['content'] and live[0][_DB_PERSISTED_MARKER]
    _assert_proof_intact(db)
    _resume(db, row)
    # The rebuilt owner has no SessionStore and no old in-memory history (the TUI path).
    import asyncio
    from gateway.session_settlement_recovery import drain_deferred_rows
    authority = types.SimpleNamespace(db=db, runner=None, physical_target=lambda ref: 's')
    asyncio.run(drain_deferred_rows(authority, types.SimpleNamespace(session_id='s')))
    assert len(active()) == 1 and 'm2' in active()[0] and not _spool_files()
    asyncio.run(drain_deferred_rows(authority, types.SimpleNamespace(session_id='s')))
    assert len(active()) == 1


@pytest.mark.parametrize('entrypoint', ['settlement', 'store', 'boot', 'append'])
def test_deferred_parent_rows_follow_compression_after_resume(parked, spool_home, monkeypatch, entrypoint):
    import asyncio
    from gateway.session import SessionStore
    from gateway.session_settlement_recovery import drain_deferred_rows
    db, _, row = parked
    for message in [
        {'role': 'user', 'content': 'first'},
        {'role': 'user', 'content': 'new pivot', 'display_kind': 'model_switch'},
        {'role': 'user', 'content': 'second'},
    ]:
        assert shutdown_flush.defer_row_for_parked_turn('s', message)
    _resume(db, row)
    for parent, child in [('s', 'mid'), ('mid', 'tip')]:
        db.publish_compression_child(parent_session_id=parent, child_session_id=child, source='test',
                                     messages=[{'role': 'user', 'content': 'old pivot',
                                                'display_kind': 'model_switch'}], require_compression_lease=False)
    before = db.get_messages('s')
    assert shutdown_flush.defer_row_for_parked_turn('tip', {'role': 'user', 'content': 'third'})
    if entrypoint == 'boot':
        assert shutdown_flush.recover_pending_spool(db) == (4, set())
    else:
        store = None
        if entrypoint in {'store', 'append'}:
            store = object.__new__(SessionStore)
            store._db = db
            store._transcript_retry_lock = threading.Lock()
            store._transcript_append_failures = {}
            store._dirty_transcripts = {}
            store._spooled_drop_sessions = {'s'}
            monkeypatch.setattr(store, '_db_for_session_id', lambda sid: db)
        authority = types.SimpleNamespace(db=db, runner=types.SimpleNamespace(session_store=store),
                                          physical_target=lambda ref: 'tip')
        # Both logical and physical refs now name only the tip: discover the parent spool from SQLite.
        if entrypoint == 'append':
            store.append_to_transcript('tip', {'role': 'user', 'content': 'fourth'})
        else:
            asyncio.run(drain_deferred_rows(authority, types.SimpleNamespace(session_id='tip')))
    assert not _spool_files()
    assert db.get_messages('s') == before
    contents = [m['content'] for m in db.get_messages('tip')]
    expected = ['first', 'new pivot', 'second', 'third'] + (['fourth'] if entrypoint == 'append' else [])
    assert contents[-len(expected):] == expected
    assert [m['content'] for m in db.get_messages('tip') if m.get('display_kind') == 'model_switch'] == ['new pivot']
    assert shutdown_flush.recover_pending_spool(db) == (0, set())


@pytest.mark.parametrize('defect', ['branch', 'reset', 'delegate', 'tool', 'noncompression', 'profile', 'ambiguous'])
@pytest.mark.parametrize('entrypoint', ['drain', 'boot'])
def test_deferred_rows_never_redirect_across_unproven_edges(parked, spool_home, defect, entrypoint):
    db, _, row = parked
    assert shutdown_flush.defer_row_for_parked_turn('s', {'role': 'user', 'content': 'keep custody'})
    _resume(db, row)
    config = {f'_{defect if defect != "branch" else "branched"}_from': 's'} if defect in {'branch', 'reset', 'delegate'} else {}
    db.create_session('child', source='tool' if defect == 'tool' else 'test', parent_session_id='s', model_config=config)
    db._execute_write(lambda c: c.execute("UPDATE sessions SET ended_at=1,end_reason=? WHERE id='s'",
                                          ('manual' if defect == 'noncompression' else 'compression',)))
    if defect == 'profile':
        db._execute_write(lambda c: c.execute("UPDATE sessions SET profile_name='foreign' WHERE id='child'"))
    if defect == 'ambiguous':
        db.create_session('sibling', source='test', parent_session_id='s')
    # Discovery from the child cannot steal the parent spool; nor may a parent/boot drain route to it.
    assert shutdown_flush.replay_deferred_rows(db, 'child') == (0, 0)
    # A noncompression-ended source may still accept its OWN passive rows; it must never
    # redirect them to its child. Compression-ended sources cannot accept an append at all.
    expected = int(defect == 'noncompression')
    if entrypoint == 'drain':
        assert shutdown_flush.replay_deferred_rows(db, 's')[0] == expected
    else:
        assert shutdown_flush.recover_pending_spool(db)[0] == expected
    assert len(_spool_files()) == 1 - expected
    assert not db.get_messages('child')
    assert _contents(db).count('keep custody') == expected


@pytest.mark.parametrize('entrypoint', ['drain', 'boot'])
def test_compression_replay_failure_holds_later_tip_rows_in_order(parked, spool_home, monkeypatch, entrypoint):
    import sqlite3
    db, _, row = parked
    assert shutdown_flush.defer_row_for_parked_turn('s', {'role': 'user', 'content': 'first'})
    _resume(db, row)
    db.publish_compression_child(parent_session_id='s', child_session_id='tip', source='test',
                                 messages=[{'role': 'user', 'content': 'summary'}], require_compression_lease=False)
    assert shutdown_flush.defer_row_for_parked_turn('tip', {'role': 'user', 'content': 'second'})
    append = db.append_message
    calls = []
    def unavailable(**kwargs):
        calls.append(kwargs['content'])
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(db, 'append_message', unavailable)
    if entrypoint == 'drain':
        assert shutdown_flush.replay_deferred_rows(db, 'tip') == (0, 2)
    else:
        assert shutdown_flush.recover_pending_spool(db) == (0, {'tip'})
    assert calls == ['first'] and len(_spool_files()) == 2
    monkeypatch.setattr(db, 'append_message', append)
    assert shutdown_flush.replay_deferred_rows(db, 'tip') == (2, 0)
    assert [m['content'] for m in db.get_messages('tip')][-2:] == ['first', 'second']


@pytest.mark.parametrize('entrypoint', ['settlement', 'store', 'boot', 'append'])
def test_compression_replay_uses_only_the_owning_profile_spool(parked, tmp_path, monkeypatch, entrypoint):
    import asyncio
    from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
    from gateway.session import SessionStore
    from gateway.session_settlement_recovery import drain_deferred_rows
    db, _, row = parked
    from hermes_state import SessionDB
    foreign_db = SessionDB(tmp_path / 'foreign.db')
    foreign_db.create_session('s', source='test')
    foreign_db.create_session('tip', source='test')
    owner, ambient = tmp_path / 'owner', tmp_path / 'ambient'
    owner.mkdir()
    ambient.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(ambient))
    token = set_hermes_home_override(str(owner))
    try:
        owner_file = shutdown_flush.defer_row_for_parked_turn('s', {'role': 'user', 'content': 'owner only'})
    finally:
        reset_hermes_home_override(token)
    foreign_file = shutdown_flush.defer_row_for_parked_turn('s', {'role': 'user', 'content': 'foreign only'})
    _resume(db, row)
    db.publish_compression_child(parent_session_id='s', child_session_id='tip', source='test',
                                 messages=[{'role': 'user', 'content': 'summary'}], require_compression_lease=False)
    if entrypoint == 'boot':
        token = set_hermes_home_override(str(owner))
        try:
            assert shutdown_flush.recover_pending_spool(db) == (1, set())
        finally:
            reset_hermes_home_override(token)
    else:
        store = None
        if entrypoint in {'store', 'append'}:
            store = object.__new__(SessionStore)
            store._transcript_retry_lock = threading.Lock()
            store._transcript_append_failures = {}
            store._dirty_transcripts = {}
            store._spooled_drop_sessions = {'s'}
            # A shared SessionStore may have lost the old id's owner hint after compression.
            # Settlement's authority still owns the canonical profile DB and must supply it.
            monkeypatch.setattr(store, '_db_for_session_id', lambda sid: foreign_db if entrypoint == 'store' else db)
            monkeypatch.setattr(store, '_owner_key_for_session_id', lambda sid: 'agent:owner:test')
            monkeypatch.setattr(store, '_profile_home_for_key', lambda key: owner)
        authority = types.SimpleNamespace(db=db, profile_id=str(owner),
                                          runner=types.SimpleNamespace(session_store=store),
                                          physical_target=lambda ref: 'tip')
        if entrypoint == 'append':
            assert store is not None
            store.append_to_transcript('tip', {'role': 'user', 'content': 'live'})
        else:
            asyncio.run(drain_deferred_rows(authority, types.SimpleNamespace(session_id='tip')))
    assert owner_file is not None and foreign_file is not None
    assert not owner_file.exists() and foreign_file.exists()
    expected = ['summary', 'owner only'] + (['live'] if entrypoint == 'append' else [])
    assert [m['content'] for m in db.get_messages('tip')] == expected
    assert get_hermes_home() == ambient
    assert not foreign_db.get_messages('s') and not foreign_db.get_messages('tip')
    foreign_db.close()


def test_settlement_drains_deferred_rows_for_both_session_ids():
    import asyncio
    from gateway.session_settlement_recovery import drain_deferred_rows
    drained = []
    store = types.SimpleNamespace(drain_deferred_transcript=drained.append)
    authority = types.SimpleNamespace(runner=types.SimpleNamespace(session_store=store),
                                      physical_target=lambda ref: 'tip')
    asyncio.run(drain_deferred_rows(authority, types.SimpleNamespace(session_id='s')))
    assert drained == ['s', 'tip']
    # A runner without a store (tests, headless owners) is a no-op, never an error.
    asyncio.run(drain_deferred_rows(types.SimpleNamespace(runner=None), types.SimpleNamespace(session_id='s')))
