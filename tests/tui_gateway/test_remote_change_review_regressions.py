"""Real discovery/source identity regressions from the first independent review."""
import contextlib
from pathlib import Path

import pytest

from tests.tui_gateway import test_remote_absent_expired_claims as claims
from tests.tui_gateway import test_remote_change_absence as absence
from tui_gateway import notice_fanout as nf, kanban_unloaded as ku

claim = claims.claim
remote = absence.remote
absent = absence.absent


@pytest.mark.parametrize('spelling', ['symlink', 'relative'])
def test_expired_absent_claim_uses_canonical_source(claim, tmp_path, monkeypatch, spelling):
    ledger, source = claim
    source.touch()
    if spelling == 'symlink':
        alias = tmp_path / 'linked'
        alias.symlink_to(source.parent, target_is_directory=True)
        alias_source = alias / source.name
    else:
        monkeypatch.chdir(source.parent)
        alias_source = Path(source.name)
    assert str(alias_source) != str(source.resolve())
    assert claims.notices._settle_stale_claims({}, alias_source, ledger, now=claims.NOW) == 1
    assert claims.row(claim)['settled'] == 'expired'
    with claims.sqlite3.connect(ledger) as conn:
        assert conn.execute('SELECT replica,reason FROM swept').fetchall() == [(str(source.resolve()), 'expired')]
    assert claims.notices._settle_stale_claims({}, source, ledger, now=claims.NOW) == 0


@pytest.mark.parametrize('failure', ['listing', 'stat'])
def test_profile_discovery_failure_keeps_retry_then_recovers(absent, tmp_path, monkeypatch, failure):
    from tui_gateway import server
    import hermes_constants

    root = tmp_path / 'profiles'
    profile = root / 'work'
    profile.mkdir(parents=True)
    (profile / 'state.db').touch()
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: tmp_path)
    monkeypatch.setattr('tui_gateway.kanban_chat_hosting.get_hermes_home', lambda: tmp_path)
    original_iterdir, original_stat = Path.iterdir, Path.stat
    broken = [True]

    def iterdir(path):
        if broken[0] and failure == 'listing' and path == root:
            raise OSError(4, 'Interrupted system call')
        return original_iterdir(path)

    def stat(path, *args, **kwargs):
        if broken[0] and failure == 'stat' and path == profile / 'state.db':
            raise OSError(4, 'Interrupted system call')
        return original_stat(path, *args, **kwargs)

    class EmptyStore:
        def get_session(self, chat):
            return None

    class PresentStore:
        def get_session(self, chat):
            return {'id': chat}

        def get_compression_tip(self, chat):
            return chat

    @contextlib.contextmanager
    def profile_db(params=None, **kwargs):
        yield PresentStore() if params == {'profile': 'work'} else EmptyStore()

    monkeypatch.setattr(Path, 'iterdir', iterdir)
    monkeypatch.setattr(Path, 'stat', stat)
    monkeypatch.setattr(server, '_profile_db', profile_db)
    with ku.find_session_db('missing') as lookup:
        assert tuple(lookup) == (None, None)
        assert lookup.unavailable
    result = nf._remote_change_pass(ku.find_session_db)
    assert result.incomplete
    assert absent['acks'] == [{}]
    assert 'missing' not in nf._remote_absent
    broken[0] = False
    with ku.find_session_db('missing') as lookup:
        assert lookup[1] == 'missing'
        assert not lookup.unavailable
    # Discovery repair is retried immediately, not held behind absence maintenance.
    assert not nf._remote_change_pass(ku.find_session_db).incomplete
    assert absent['acks'][-1] == {'missing': frozenset({('owner/board/card', 'blocked')})}


def test_warning_history_tracks_only_current_snapshot(absent, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger=nf.__name__):
        absent['run']()
        for status in ['running', 'blocked', 'running']:
            absent['remote']['cards'][0]['status'] = status
            nf._remote_memo = None
            absent['run']()
            assert len(nf._remote_absent_warned) == 1
        absent['remote']['cards'] = []
        nf._remote_memo = None
        absent['run']()
    assert nf._remote_absent_warned == set()
    assert nf._remote_absent == {}
