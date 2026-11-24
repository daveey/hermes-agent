"""Admission identity matrix: namespace/exec changes never become absence evidence."""
import json
import os
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from hermes_cli import serve_producer_admission as admission
from hermes_cli.serve_producer_census import CensusUnavailable, LinuxProcessCensus

pytestmark = pytest.mark.platforms('linux')
ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize('field,value', [
    ('boot_id', 'another-boot'), ('namespace', 'pid:[1]'), ('pid', 987654),
    ('start_ticks', 1), ('launcher_incarnation', 'a' * 64),
])
def test_each_component_changes_identity_and_cannot_reuse_enrollment(tmp_path, field, value):
    receipt = admission.enroll_launch(tmp_path / 'runtime', 'serve', installation=ROOT)
    adapter = LinuxProcessCensus()
    record = adapter.read_process(os.getpid(), adapter.boot_id())
    assert receipt.key == record.key
    changed = replace(record.key, **{field: value})
    assert changed != record.key
    assert len({changed, record.key}) == 2
    if field == 'namespace':
        foreign = replace(receipt, namespace=value)
    elif field == 'launcher_incarnation':
        foreign = replace(receipt, observation=value)
    else:
        foreign = replace(receipt, identity=replace(receipt.identity, **{field: value}))
    result = admission.classify_process(record, (foreign,), receipt.generation, installation=str(ROOT))
    assert not result.proof.startswith('enrollment:')
    assert result.identity == record.key


@pytest.mark.parametrize('change,duplicate', [
    ({}, True), ({'namespace': 'pid:[1]'}, False),
    ({'identity': {'start_ticks': 1}}, False),
    ({'identity': {'boot_id': 'previous-boot'}}, False),
    ({'observation': 'a' * 64}, False),
    ({'role': 'gateway'}, True), ({'generation': 'a' * 64}, True),
])
def test_duplicate_detection_uses_exact_key_not_role_or_source_version(tmp_path, change, duplicate):
    runtime = tmp_path / 'runtime'
    local = admission.enroll_launch(runtime, 'serve', installation=ROOT)
    change = dict(change)
    if 'identity' in change:
        change['identity'] = replace(local.identity, **change['identity'])
    other = replace(local, **change)
    (runtime / 'participants.json').write_text(json.dumps([asdict(local), asdict(other)]))
    if duplicate:
        with pytest.raises(CensusUnavailable, match='duplicate'):
            admission.read_participants_strict(runtime, allow_foreign_namespace=True)
    else:
        assert admission.read_participants_strict(runtime, allow_foreign_namespace=True) == (local, other)


@pytest.mark.parametrize('collision', ['same', 'reused', 'other-pid', 'previous-boot'])
def test_foreign_receipt_survives_repeated_shared_home_launch_without_liveness_probe(tmp_path, monkeypatch, collision):
    runtime = tmp_path / 'runtime'
    local = admission.enroll_launch(runtime, 'serve', installation=ROOT)
    identity = local.identity
    if collision == 'reused':
        identity = replace(identity, start_ticks=1)
    elif collision == 'other-pid':
        identity = replace(identity, pid=987654)
    elif collision == 'previous-boot':
        identity = replace(identity, boot_id='previous-boot')
    foreign = replace(local, namespace='pid:[1]', identity=identity)
    (runtime / 'participants.json').write_text(json.dumps([asdict(foreign)]))
    def forbidden(*_):
        pytest.fail('foreign namespace must never be probed/retired')
    monkeypatch.setattr(admission, 'incarnation_exited', forbidden)
    for _ in range(3):
        fresh = admission.enroll_launch(runtime, 'serve', installation=ROOT)
        assert admission.read_participants_strict(runtime, allow_foreign_namespace=True) == (foreign, fresh)
    if collision != 'previous-boot':
        with pytest.raises(CensusUnavailable, match='namespace'):
            admission.read_participants_strict(runtime)


@pytest.mark.parametrize('field,value,outcome', [
    (None, None, False), ('start_ticks', 1, True),
    ('launcher_incarnation', 'a' * 64, False),
    ('boot_id', 'previous-boot', 'boot'), ('namespace', 'pid:[1]', 'namespace'),
])
def test_retirement_uses_same_key_and_only_positive_kernel_reuse(field, value, outcome):
    adapter = LinuxProcessCensus()
    key = adapter.read_process(os.getpid(), adapter.boot_id()).key
    expected = key if field is None else replace(key, **{field: value})
    if isinstance(outcome, str):
        with pytest.raises(CensusUnavailable, match=outcome):
            admission.incarnation_exited(adapter, expected)
    else:
        assert admission.incarnation_exited(adapter, expected) is outcome


@pytest.mark.parametrize('failure', [FileNotFoundError, PermissionError])
def test_missing_live_exec_details_do_not_prove_retirement(monkeypatch, failure):
    adapter = LinuxProcessCensus()
    key = adapter.read_process(os.getpid(), adapter.boot_id()).key
    def denied(*_):
        raise failure('not process death')
    monkeypatch.setattr(adapter, 'read_process', denied)
    with pytest.raises((CensusUnavailable, PermissionError)):
        admission.incarnation_exited(adapter, key)


def test_rechecked_absence_can_retire_exact_launcher(monkeypatch):
    adapter = LinuxProcessCensus()
    key = adapter.read_process(os.getpid(), adapter.boot_id()).key
    def gone(*_):
        raise FileNotFoundError('positive rechecked exit')
    monkeypatch.setattr(adapter, 'read_process', gone)
    monkeypatch.setattr(adapter, 'identity', gone)
    assert admission.incarnation_exited(adapter, key)


@pytest.mark.parametrize('field,value', [('namespace', 'pid:[1]'), ('boot_id', 'wrong-boot'), ('pid', 987654)])
def test_incoherent_census_result_is_not_retirement(monkeypatch, field, value):
    adapter = LinuxProcessCensus()
    record = adapter.read_process(os.getpid(), adapter.boot_id())
    if field == 'namespace':
        changed = replace(record, namespace=value)
    else:
        changed = replace(record, identity=replace(record.identity, **{field: value}))
    monkeypatch.setattr(adapter, 'read_process', lambda *_: changed)
    with pytest.raises(CensusUnavailable, match='incoherent'):
        admission.incarnation_exited(adapter, record.key)


@pytest.mark.parametrize('failure', [PermissionError, FileNotFoundError, IndexError, CensusUnavailable])
def test_uncertain_own_namespace_receipt_does_not_brick_ordinary_launch(tmp_path, monkeypatch, failure):
    runtime = tmp_path / 'runtime'
    local = admission.enroll_launch(runtime, 'serve', installation=ROOT)
    old = replace(local, identity=replace(local.identity, pid=987654))
    (runtime / 'participants.json').write_text(json.dumps([asdict(old)]))
    def uncertain(*_):
        raise failure('unreadable/zombie/incomplete process observation')
    monkeypatch.setattr(admission, 'incarnation_exited', uncertain)
    fresh = admission.enroll_launch(runtime, 'serve', installation=ROOT)
    assert admission.read_participants_strict(runtime) == (old, fresh)


def test_reused_pid_can_retire_even_when_new_owners_exec_is_unreadable(monkeypatch):
    adapter = LinuxProcessCensus()
    record = adapter.read_process(os.getpid(), adapter.boot_id())
    def denied(*_):
        raise PermissionError('different UID on reused pid')
    monkeypatch.setattr(adapter, 'read_process', denied)
    assert admission.incarnation_exited(adapter, replace(record.key, start_ticks=1))


def test_raw_birth_probe_cannot_authorize_legacy_retirement(tmp_path):
    adapter = LinuxProcessCensus()
    raw = adapter.identity(os.getpid(), adapter.boot_id())
    with pytest.raises(CensusUnavailable, match='full launcher'):
        admission.incarnation_exited(adapter, raw)


def test_foreign_proc_mount_numbering_cannot_mint_namespace_identity(monkeypatch):
    original = os.readlink
    def foreign(path):
        if Path(path) == Path('/proc/self'):
            return str(os.getpid() + 1)
        return original(path)
    monkeypatch.setattr(os, 'readlink', foreign)
    with pytest.raises(CensusUnavailable, match='numbering'):
        LinuxProcessCensus().namespace_id()


def test_duplicate_write_refuses_before_replacing_existing_evidence(tmp_path):
    runtime = tmp_path / 'runtime'
    local = admission.enroll_launch(runtime, 'serve', installation=ROOT)
    ledger = runtime / 'participants.json'
    before = ledger.read_bytes()
    with pytest.raises(CensusUnavailable, match='duplicate'):
        admission._write_participants(runtime, (local, replace(local, role='gateway')))
    assert ledger.read_bytes() == before


def test_empty_census_cannot_hide_namespace_change(monkeypatch):
    adapter = LinuxProcessCensus()
    calls = 0
    def namespace():
        nonlocal calls
        calls += 1
        return 'pid:[10]' if calls == 1 else 'pid:[11]'
    monkeypatch.setattr(adapter, 'pids', lambda: ())
    monkeypatch.setattr(adapter, 'namespace_id', namespace)
    observed = adapter.collect()
    assert not observed.complete
    assert 'census:namespace_changed' in observed.errors
