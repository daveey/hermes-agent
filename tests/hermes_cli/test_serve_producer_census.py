"""Strict read-only census never interprets missing observations as retirement."""
import json
import os
from pathlib import Path

import pytest

from hermes_cli import serve_producer_census as census


def _proc(root, pid=123, birth=42, *, uid=None):
    directory = root / str(pid)
    directory.mkdir()
    fields = ['S'] + ['0'] * 18 + [str(birth)] + ['0'] * 4
    (directory / 'stat').write_text(f'{pid} (name with ) paren) ' + ' '.join(fields))
    (directory / 'status').write_text(f'Uid:\t{os.getuid() if uid is None else uid}\t0\t0\t0\n')
    (directory / 'cmdline').write_bytes(b'python\0-m\0hermes_cli.main\0serve\0')
    (directory / 'environ').write_bytes(b'HERMES_HOME=/example/home\0SECRET_MUST_NOT_ESCAPE=secret\0')
    (directory / 'exe').symlink_to('/example/python')
    (directory / 'cwd').symlink_to('/example/core')
    return directory


def _reader(tmp_path):
    (tmp_path / 'sys/kernel/random').mkdir(parents=True)
    (tmp_path / 'sys/kernel/random/boot_id').write_text('test-boot\n')
    return census.LinuxProcessCensus(tmp_path)


@pytest.mark.platforms('linux')
def test_reads_exact_incarnation_and_only_identity_environment(tmp_path):
    reader = _reader(tmp_path)
    _proc(tmp_path)
    result = reader.collect()
    assert result.complete
    assert result.errors == ()
    (record,) = result.processes
    assert record.identity == census.Incarnation('test-boot', 123, 42)
    assert record.argv == ('python', '-m', 'hermes_cli.main', 'serve')
    assert dict(record.environment) == {'HERMES_HOME': '/example/home'}
    assert record.exe == '/example/python'
    assert record.cwd == '/example/core'


@pytest.mark.platforms('linux')
def test_permission_denial_is_incomplete_not_disappearance(tmp_path, monkeypatch):
    reader = _reader(tmp_path)
    directory = _proc(tmp_path)
    original = census._read_bytes

    def denied(path, limit):
        if path == directory / 'environ':
            raise PermissionError('denied')
        return original(path, limit)

    monkeypatch.setattr(census, '_read_bytes', denied)
    result = reader.collect()
    assert not result.complete
    assert result.processes == ()
    assert result.exited == ()
    assert result.errors == ('pid:123:PermissionError',)


@pytest.mark.platforms('linux')
def test_pid_reuse_does_not_certify_old_or_new_process(tmp_path, monkeypatch):
    reader = _reader(tmp_path)
    directory = _proc(tmp_path)
    original = reader.identity
    calls = 0

    def reused(pid, boot):
        nonlocal calls
        calls += 1
        value = original(pid, boot)
        return census.Incarnation(boot, pid, 43) if calls > 1 else value

    monkeypatch.setattr(reader, 'identity', reused)
    result = reader.collect()
    assert not result.complete
    assert result.errors == ('pid:123:IncarnationChanged',)
    assert result.processes == ()
    assert directory.exists()


@pytest.mark.platforms('linux')
def test_missing_file_in_live_process_is_not_exit(tmp_path):
    reader = _reader(tmp_path)
    directory = _proc(tmp_path)
    (directory / 'cmdline').unlink()
    result = reader.collect()
    assert not result.complete
    assert result.errors == ('pid:123:FileNotFoundError',)
    assert result.exited == ()


@pytest.mark.platforms('linux')
def test_rechecked_exit_is_explicit_not_a_silent_skip(tmp_path, monkeypatch):
    reader = _reader(tmp_path)
    _proc(tmp_path)
    original = reader.identity
    calls = 0

    def exited(pid, boot):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise FileNotFoundError('gone')
        return original(pid, boot)

    monkeypatch.setattr(reader, 'identity', exited)
    result = reader.collect()
    assert result.complete
    assert result.exited == (census.Incarnation('test-boot', 123, 42),)
    assert result.processes == ()


@pytest.mark.platforms('linux')
def test_process_appearing_during_enumeration_makes_cut_incomplete(tmp_path, monkeypatch):
    reader = _reader(tmp_path)
    _proc(tmp_path)
    original = reader.pids
    calls = 0

    def pids():
        nonlocal calls
        calls += 1
        return original() if calls == 1 else (123, 456)

    monkeypatch.setattr(reader, 'pids', pids)
    result = reader.collect()
    assert not result.complete
    assert 'census:membership_changed' in result.errors


@pytest.mark.platforms('linux')
def test_raw_identity_read_has_no_producer_imports_on_real_self():
    reader = census.LinuxProcessCensus(Path('/proc'))
    record = reader.read_process(os.getpid(), reader.boot_id())
    assert record.identity.pid == os.getpid()
    assert record.identity.start_ticks > 0
    assert record.exe
    assert record.argv


@pytest.mark.parametrize('contents', [b'', b'{}', b'null', b'[null]', b'[{"pid":1}]', b'not-json'])
def test_invalid_ledger_cannot_be_complete(tmp_path, contents):
    path = tmp_path / 'spawn-ledger.json'
    path.write_bytes(contents)
    with pytest.raises(census.CensusUnavailable):
        census.read_ledger_strict(path)
    assert path.read_bytes() == contents  # no quarantine, repair or writes


def test_missing_ledger_is_not_empty_success(tmp_path):
    with pytest.raises(census.CensusUnavailable, match='ledger'):
        census.read_ledger_strict(tmp_path / 'missing')


def test_pending_rows_are_strict_and_not_silently_filtered(tmp_path):
    path = tmp_path / 'spawn-ledger.json'
    row = {'pid': 123, 'create_time': 1.0, 'purpose': 'serve', 'install': 'install',
           'spawner_pid': None, 'spawner_create': None, 'registered_at': 2.0, 'argv': 'python'}
    path.write_text(json.dumps([row]))
    pending = path.with_name(path.name + '.pending')
    pending.mkdir()
    (pending / 'one.json').write_text(json.dumps(row | {'pid': 456}))
    assert [entry['pid'] for entry in census.read_ledger_strict(path)] == [123, 456]
    (pending / 'bad.json').write_text('null')
    with pytest.raises(census.CensusUnavailable):
        census.read_ledger_strict(path)


@pytest.mark.platforms('linux')
def test_census_budget_and_boot_change_fail_closed(tmp_path, monkeypatch):
    reader = _reader(tmp_path)
    _proc(tmp_path)
    monkeypatch.setattr(census, 'MAX_PROCESSES', 0)
    assert reader.collect().errors == ('census:process_budget',)
    monkeypatch.setattr(census, 'MAX_PROCESSES', 8192)
    boots = iter(['test-boot', 'changed-boot'])
    monkeypatch.setattr(reader, 'boot_id', lambda: next(boots))
    result = reader.collect()
    assert not result.complete
    assert 'census:boot_changed' in result.errors


@pytest.mark.platforms('linux')
def test_exec_details_mutation_with_unchanged_birth_is_incomplete(tmp_path, monkeypatch):
    reader = _reader(tmp_path)
    directory = _proc(tmp_path)
    original = reader._details
    changed = False

    def exec_after_details(pid):
        nonlocal changed
        result = original(pid)
        if not changed:
            changed = True
            (directory / 'exe').unlink()
            (directory / 'exe').symlink_to('/different/runtime')
        return result

    monkeypatch.setattr(reader, '_details', exec_after_details)
    result = reader.collect()
    assert not result.complete
    assert result.errors == ('pid:123:IncarnationChanged',)


@pytest.mark.platforms('linux')
def test_reuse_after_observation_is_not_hidden_by_equal_pid_lists(tmp_path, monkeypatch):
    reader = _reader(tmp_path)
    _proc(tmp_path)
    original = reader.pids
    calls = 0

    def reused():
        nonlocal calls
        calls += 1
        if calls == 2:
            path = tmp_path / '123/stat'
            path.write_text(path.read_text().replace(' 42 ', ' 43 '))
        return original()

    monkeypatch.setattr(reader, 'pids', reused)
    result = reader.collect()
    assert not result.complete
    assert result.errors == ('pid:123:final_identity_changed',)


def test_pending_to_main_fold_cannot_disappear_from_read(tmp_path, monkeypatch):
    path = tmp_path / 'spawn-ledger.json'
    path.write_text('[]')
    pending = path.with_name(path.name + '.pending')
    pending.mkdir()
    row = {'pid': 123, 'create_time': 1.0, 'purpose': 'serve', 'install': 'install',
           'spawner_pid': None, 'spawner_create': None, 'registered_at': 2.0, 'argv': 'python'}
    queued = pending / 'one.json'
    queued.write_text(json.dumps(row))
    original = census._read_bytes
    changed = False

    def folded(candidate, limit):
        nonlocal changed
        result = original(candidate, limit)
        if candidate == path and not changed:
            changed = True
            path.write_text(json.dumps([row]))
            queued.unlink()
        return result

    monkeypatch.setattr(census, '_read_bytes', folded)
    with pytest.raises(census.CensusUnavailable, match='main ledger changed'):
        census.read_ledger_strict(path)


@pytest.mark.platforms('linux')
def test_argument_object_memory_and_separator_count_are_bounded(tmp_path, monkeypatch):
    reader = _reader(tmp_path)
    directory = _proc(tmp_path)
    (directory / 'cmdline').write_bytes(b'a\0' * 4000)
    monkeypatch.setattr(census, 'MAX_OBSERVATION_BYTES', 20000)
    assert 'census:retained_byte_budget' in reader.collect().errors
    (directory / 'cmdline').write_bytes(b'\0' * 4097)
    assert not reader.collect().complete


def test_directory_enumeration_stops_before_materializing_oversized_input(tmp_path, monkeypatch):
    from types import SimpleNamespace
    visited = []

    class Entries:
        def __enter__(self):
            return self
        def __exit__(self, *_exc):
            return False
        def __iter__(self):
            for value in range(100):
                visited.append(value)
                yield SimpleNamespace(name=str(value))

    monkeypatch.setattr(census.os, 'scandir', lambda path: Entries())
    with pytest.raises(census.CensusUnavailable, match='process_budget'):
        census._bounded_names(tmp_path, 2, numeric=True)
    assert visited == [0, 1, 2]


@pytest.mark.parametrize('changed', [
    {'pid': True}, {'create_time': None}, {'registered_at': float('nan')},
    {'boot_start': -1}, {'spawner_boot': True}, {'boot_id': 123},
    {'isolated': 'false'}, {'port': 65536}, {'profile': None},
    {'spawner_pid': 456, 'spawner_create': None}, {'unexpected': {'nested': 'value'}},
])
def test_malformed_optional_ledger_identity_is_not_ignored(tmp_path, changed):
    path = tmp_path / 'spawn-ledger.json'
    row = {'pid': 123, 'create_time': 1.0, 'purpose': 'serve', 'install': 'install',
           'spawner_pid': None, 'spawner_create': None, 'registered_at': 2.0, 'argv': 'python'}
    path.write_text(json.dumps([row | changed]))
    with pytest.raises(census.CensusUnavailable):
        census.read_ledger_strict(path)


def test_explicit_empty_ledger_is_readable_not_an_admission_receipt(tmp_path):
    path = tmp_path / 'spawn-ledger.json'
    path.write_text('[]')
    assert census.read_ledger_strict(path) == ()
