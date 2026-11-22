"""Negative-only memo failures must retain the live quota verdict."""
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from hermes_cli import auth, auth_codex, auth_codex_quota_negative as memo
from tests.hermes_cli.test_auth_codex_quota_negative_memo import token


@pytest.mark.parametrize('field', ['failure_at', 'reset_at'])
@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf')])
def test_nonfinite_evidence_keeps_live_probe(tmp_path, monkeypatch, field, value):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    calls = []

    @contextmanager
    def client(**kwargs):
        calls.append(1)
        yield SimpleNamespace(get=lambda *a, **k: SimpleNamespace(status_code=429))

    monkeypatch.setattr(auth_codex, '_codex_http_client', client)
    auth._codex_quota_probe_cache.clear()
    assert auth_codex._probe_codex_quota_restored(token(), **{field: value}) is False
    assert calls == [1]


@pytest.mark.parametrize('error', [OSError, ValueError, TypeError])
@pytest.mark.parametrize('status, expected', [(429, False), (401, None)])
def test_memo_write_failure_preserves_verdict(tmp_path, monkeypatch, error, status, expected):
    import utils
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))

    def fail(*a, **kw):
        raise error('injected persistence fault')

    @contextmanager
    def client(**kwargs):
        yield SimpleNamespace(get=lambda *a, **k: SimpleNamespace(status_code=status))

    monkeypatch.setattr(utils, 'atomic_write_bytes', fail)
    monkeypatch.setattr(auth_codex, '_codex_http_client', client)
    auth._codex_quota_probe_cache.clear()
    assert auth_codex._probe_codex_quota_restored(token()) is expected


@pytest.mark.parametrize('stamp', ['1000', True, None, [], {}, False])
def test_invalid_stamp_is_a_miss(tmp_path, monkeypatch, stamp):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(memo.time, 'time', lambda: 1.)
    memo._path().parent.mkdir(parents=True)
    memo._path().write_text(json.dumps({'key': stamp}))
    assert memo.hit('key', 300) is False


def test_oversized_memo_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(memo.time, 'time', lambda: 1000.)
    memo._path().parent.mkdir(parents=True)
    memo._path().write_text(json.dumps({'key': 1000.}) + ' ' * memo.MAX_BYTES)
    assert memo.hit('key', 300) is False


def test_caller_interval_can_shorten_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    clock = [1000.]
    monkeypatch.setattr(memo.time, 'time', lambda: clock[0])
    memo.remember('key')
    clock[0] += 9
    assert memo.hit('key', 10) is True
    clock[0] += 1
    assert memo.hit('key', 10) is False
    assert memo.hit('key', 300) is True


@pytest.mark.parametrize('stamp', ['1000', True, None, [], {}, False])
def test_remember_discards_invalid_stamp_after_negative_probe(tmp_path, monkeypatch, stamp):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(memo.time, 'time', lambda: 1.)
    memo._path().parent.mkdir(parents=True)
    memo._path().write_text(json.dumps({'junk': stamp}))
    calls = []

    @contextmanager
    def client(**kwargs):
        calls.append(1)
        yield SimpleNamespace(get=lambda *a, **k: SimpleNamespace(status_code=429))

    monkeypatch.setattr(auth_codex, '_codex_http_client', client)
    auth._codex_quota_probe_cache.clear()
    assert auth_codex._probe_codex_quota_restored(token()) is False
    assert calls == [1]
    records = json.loads(memo._path().read_text())
    assert 'junk' not in records
    assert len(records) == 1
    assert list(records.values()) == [1.]


def test_entry_read_cap_rejects_recent_records(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(memo.time, 'time', lambda: 1000.)
    memo._path().parent.mkdir(parents=True)
    records = {str(i): 1000. for i in range(memo.MAX_ENTRIES + 1)}
    memo._path().write_text(json.dumps(records))
    assert memo.hit('0', 300) is False
    memo.remember('new')
    assert json.loads(memo._path().read_text()) == {'new': 1000.}


def test_remember_filters_expired_future_and_nonfinite_stamps(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(memo.time, 'time', lambda: 1000.)
    memo._path().parent.mkdir(parents=True)
    memo._path().write_text(json.dumps({
        'expired': 1000. - memo.TTL_SECONDS, 'older': 0.,
        'future': 1001., 'nan': float('nan'), 'inf': float('inf'),
        'negative_inf': -float('inf'), 'recent': 999.,
    }))
    memo.remember('new')
    assert json.loads(memo._path().read_text()) == {'recent': 999., 'new': 1000.}


def test_remember_trims_oldest_at_entry_cap(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(memo.time, 'time', lambda: 1000.)
    memo._path().parent.mkdir(parents=True)
    records = {str(i): 999. + i / memo.MAX_ENTRIES for i in range(memo.MAX_ENTRIES)}
    memo._path().write_text(json.dumps(records))
    assert memo.hit('0', 300) is True  # Exactly-at-cap input remains usable.
    memo.remember('new')
    expected = {k: v for k, v in records.items() if k != '0'}
    expected['new'] = 1000.
    assert json.loads(memo._path().read_text()) == expected
