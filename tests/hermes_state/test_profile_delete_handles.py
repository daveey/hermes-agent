"""Profile retirement must include bare and read-only handles, not just shared writers."""
import shutil

import pytest

from hermes_state import SessionDB
from hermes_state_dbfile import quarantine_cross_process_lock
from hermes_state_registry import acquire, close_all_under


def test_release_includes_unshared_and_readonly_handles(tmp_path):
    home = tmp_path / 'retiring'
    home.mkdir()
    shared = acquire(home / 'state.db')
    bare = SessionDB(home / 'state.db')
    reader = SessionDB(home / 'state.db', read_only=True)
    bystander = SessionDB(tmp_path / 'bystander.db')
    try:
        close_all_under(home)
        assert shared._conn is None
        assert bare._conn is None
        assert reader._conn is None
        assert bystander._conn is not None
        # A replacement must not encounter our own deleted-WAL descriptors.
        shutil.rmtree(home)
        home.mkdir()
        replacement = acquire(home / 'state.db')
        try:
            replacement.create_session('replacement', source='cli', model='test')
            assert replacement.get_session('replacement') is not None
        finally:
            replacement.close()
    finally:
        close_all_under(home)
        bare.close()
        reader.close()
        bystander.close()


@pytest.mark.parametrize('deleted', [False, True])
def test_quarantine_poll_does_not_recreate_missing_profile(tmp_path, monkeypatch, deleted):
    from hermes_constants import mark_named_profile_deleted

    root = tmp_path / 'hermes'
    root.mkdir()
    (root / 'config.yaml').write_text('{}')
    monkeypatch.setenv('HERMES_HOME', str(root))
    home = root / 'profiles' / 'absent'
    if deleted:
        mark_named_profile_deleted(home)
    for _ in range(3):
        with pytest.raises(FileNotFoundError):
            with quarantine_cross_process_lock(home / 'state.db'):
                pytest.fail('a retired home acquired a quarantine lock')
        assert not home.exists()
