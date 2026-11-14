"""A reply watcher must retain the sender's profile across its thread boundary."""
import threading
import types

import hermes_constants
from gateway import shutdown_flush
from tests.gateway import test_parked_passive_row_delivery as helpers

parked = helpers.parked


def test_bot_reply_spools_and_replays_only_in_owning_profile(parked, tmp_path, monkeypatch):
    from tools import bot_mode_dm, process_registry_notifications as notes
    from tools.process_registry import process_registry

    db, _, row = parked
    launch, secondary = tmp_path / 'launch', tmp_path / 'secondary'
    launch.mkdir()
    secondary.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(launch))
    done = threading.Event()
    proc = types.SimpleNamespace(_completion_event=done)
    monkeypatch.setattr(process_registry, 'get', lambda pid: proc)
    monkeypatch.setattr(process_registry, 'is_completion_consumed', lambda pid: False)
    monkeypatch.setattr(process_registry, '_exit_snapshot', lambda p, s: {'status': s})
    monkeypatch.setattr(notes, 'format_process_notification', lambda evt: 'secondary reply')
    monkeypatch.setattr(notes, 'process_completion_display_text', lambda evts: 'reply')
    threads = []
    original_thread = threading.Thread

    def tracked_thread(*args, **kwargs):
        thread = original_thread(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(bot_mode_dm.threading, 'Thread', tracked_thread)
    token = hermes_constants.set_hermes_home_override(secondary)
    try:
        assert bot_mode_dm._persist_reply_when_done(
            'proc-profile', types.SimpleNamespace(_session_db=db, session_id='s'))
    finally:
        hermes_constants.reset_hermes_home_override(token)
        done.set()
        for thread in threads:
            thread.join(10)
            assert not thread.is_alive()

    assert not list((launch / 'pending_messages').glob('pending-*.json'))
    token = hermes_constants.set_hermes_home_override(secondary)
    try:
        assert len(helpers._spool_files()) == 1
        helpers._assert_proof_intact(db)
        assert shutdown_flush.replay_deferred_rows(db, 's') == (0, 1)
        helpers._resume(db, row)
        assert shutdown_flush.replay_deferred_rows(db, 's') == (1, 0)
        assert helpers._contents(db, display_kind='process_complete') == ['secondary reply']
        assert shutdown_flush.replay_deferred_rows(db, 's') == (0, 0)
    finally:
        hermes_constants.reset_hermes_home_override(token)
