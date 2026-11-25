"""Exercise explicit recency preparation through its real module entry point."""
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from hermes_state import SessionDB
import hermes_state_recency_build as build
from tests.hermes_state import test_recency_hot_rows as helpers
from tests.hermes_state.test_recency_cold_index import prepare
from tests.hermes_state.test_recency_preparation import state, assert_mirror


def test_real_preparation_module_and_reopen(tmp_path):
    path = tmp_path / 'state.db'
    db = SessionDB(db_path=path)
    helpers._seed(db, 20)
    db.close()
    result = subprocess.run([sys.executable, '-m', 'hermes_state_recency_build', str(path)],
                            text=True, capture_output=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert 'recency candidates ready' in result.stdout
    reader = SessionDB(db_path=path, read_only=True)
    try:
        assert state(reader)[0][1] == 1
        assert_mirror(reader)
    finally:
        reader.close()


@pytest.mark.parametrize('seconds', ['0', '-1', 'nan', 'inf'])
def test_main_rejects_invalid_deadline_before_owner_open(tmp_path, monkeypatch, seconds):
    missing = tmp_path / 'missing.db'
    monkeypatch.setattr(sys, 'argv', ['build', str(missing), '--seconds', seconds])
    with pytest.raises(SystemExit) as exc:
        build.main()
    assert exc.value.code == 2
    assert not missing.exists()


def test_main_refuses_missing_database(tmp_path, monkeypatch):
    missing = tmp_path / 'missing.db'
    monkeypatch.setattr(sys, 'argv', ['build', str(missing)])
    with pytest.raises(SystemExit) as exc:
        build.main()
    assert exc.value.code == 2
    assert not missing.exists()


def test_main_deadline_retains_incomplete_cursor_and_yields(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'state.db'
    db = SessionDB(db_path=path)
    helpers._seed(db, 200)
    db.close()
    monkeypatch.setattr(sys, 'argv', ['build', str(path), '--seconds', '2'])
    original = build.prepare_batch
    calls, yields = [], []
    ticks = iter([0, 0, 0, 0.1, 3])
    fake_time = SimpleNamespace(monotonic=lambda: next(ticks), sleep=yields.append)
    monkeypatch.setattr(build, 'time', fake_time)
    def one_batch(owner):
        calls.append(owner)
        monkeypatch.setattr(build, 'time', time)
        try:
            return original(owner, batch_size=1)
        finally:
            monkeypatch.setattr(build, 'time', fake_time)
    monkeypatch.setattr(build, 'prepare_batch', one_batch)
    assert build.main() == 1
    assert len(calls) == 1
    assert yields == [0.1]
    assert 'incomplete' in capsys.readouterr().out
    monkeypatch.undo()
    db = SessionDB(db_path=path)
    try:
        assert state(db) == [(1, 0)]
        prepare(db)
        assert_mirror(db)
    finally:
        db.close()
