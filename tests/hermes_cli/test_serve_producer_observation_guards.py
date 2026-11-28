"""Every conjunct of the shared-family observation has its own refusing control."""
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cli import serve_producer_broker as shared
from hermes_cli import serve_producer_observation as observation
from hermes_cli.serve_producer_admission import LaunchGate, launcher_generation, read_participants_strict
from hermes_cli.serve_producer_census import LinuxProcessCensus
from tests.hermes_cli.test_serve_producer_shared_broker import start, write_provider

pytestmark = pytest.mark.platforms('linux')
ROOT = Path(__file__).parents[2]


@pytest.fixture
def family(tmp_path):
    relay = start(write_provider(tmp_path), tmp_path)
    runtime = tmp_path / 'runtime'
    try:
        yield runtime, relay
    finally:
        assert relay.retire(timeout=10)


def observe(runtime, participants=None):
    gate = LaunchGate.acquire(runtime)
    try:
        return observation.observe_shared_family(
            runtime, ROOT, read_participants_strict(runtime) if participants is None else participants,
            launcher_generation(ROOT), gate=gate)
    finally:
        gate.close()


def test_control_the_untouched_family_is_observed(family):
    runtime, relay = family
    broker, child = observe(runtime)
    assert (broker.identity.pid, child.identity.pid) == (relay.broker_pid, relay.ping()['child_pid'])


@pytest.mark.parametrize('field, value', [
    ('generation', '0' * 64),
    ('installation', str(ROOT.parent)),
    ('observation', '0' * 64),
    ('identity', 'other-process'),
])
def test_each_ticket_attribute_must_match_individually(family, field, value):
    runtime, _ = family
    (row,) = [row for row in read_participants_strict(runtime) if row.role == 'producer_broker']
    if field == 'identity':
        value = replace(row.identity, start_ticks=row.identity.start_ticks + 1)
    assert observe(runtime, (replace(row, **{field: value}),)) == ()


def test_duplicate_matching_tickets_are_ambiguous(family):
    runtime, _ = family
    (row,) = [row for row in read_participants_strict(runtime) if row.role == 'producer_broker']
    assert observe(runtime, (row, row)) == ()


@pytest.mark.parametrize('failing_role', ['broker', 'producer'])
def test_both_processes_must_be_the_exact_launcher_exec(family, monkeypatch, failing_role):
    runtime, _ = family
    real = observation._exec
    monkeypatch.setattr(observation, '_exec', lambda record, installation, role: role != failing_role
                        and real(record, installation, role))
    assert observe(runtime) == ()


@pytest.mark.parametrize('failing', [('broker', 'panel.lock'), ('child', 'panel.lock'),
                                     ('child', 'panel-execution.lock')])
def test_each_lock_description_must_be_independently_owned(family, monkeypatch, failing):
    runtime, relay = family
    real = observation._descriptor
    child_pid = relay.ping()['child_pid']

    def descriptor(record, path, *, owner):
        who = 'child' if record.identity.pid == child_pid else 'broker'
        return (who, path.name) != failing and real(record, path, owner=owner)

    monkeypatch.setattr(observation, '_descriptor', descriptor)
    assert observe(runtime) == ()


@pytest.mark.parametrize('moving', ['broker', 'child'])
def test_a_process_that_changes_between_reads_is_refused(family, monkeypatch, moving):
    runtime, relay = family
    child_pid = relay.ping()['child_pid']
    target = child_pid if moving == 'child' else relay.broker_pid
    seen = []

    class Moving(LinuxProcessCensus):
        def read_process(self, pid, boot):
            record = super().read_process(pid, boot)
            if pid == target:
                seen.append(1)
                if len(seen) > 1:
                    return replace(record, argv=(*record.argv, 'changed'))
            return record

    monkeypatch.setattr(observation, 'LinuxProcessCensus', Moving)
    assert observe(runtime) == ()
    assert len(seen) == 2


def test_source_generation_must_still_be_current_after_the_observation(family, monkeypatch):
    runtime, _ = family
    monkeypatch.setattr(observation, 'launcher_generation', lambda installation: '0' * 64)
    assert observe(runtime) == ()


def test_owner_record_must_not_change_during_the_observation(family, monkeypatch):
    runtime, _ = family
    real = observation.read_owner
    seen = []

    def changing(path):
        value = real(path)
        seen.append(1)
        return value if len(seen) == 1 else {**(value or {}), 'revision': -1}

    monkeypatch.setattr(observation, 'read_owner', changing)
    assert observe(runtime) == ()
    assert len(seen) == 2


@pytest.mark.parametrize('line, expected', [
    ('lock:\t1: FLOCK  ADVISORY  WRITE {pid} 08:01:7 0 EOF', True),
    ('xlock:\t1: FLOCK  ADVISORY  WRITE {pid} 08:01:7 0 EOF', False),
    ('lock:\t1: POSIX  ADVISORY  WRITE {pid} 08:01:7 0 EOF', False),
    ('lock:\t1: FLOCK  ADVISORY  READ {pid} 08:01:7 0 EOF', False),
], ids=['exact', 'not-a-lock-line', 'not-flock', 'not-exclusive'])
def test_descriptor_lock_line_shape(family, monkeypatch, line, expected):
    runtime, relay = family
    adapter = LinuxProcessCensus()
    broker = adapter.read_process(relay.broker_pid, adapter.boot_id())
    monkeypatch.setattr(observation, '_read_bytes', lambda target, limit, **kw: line.format(
        pid=relay.broker_pid).encode('ascii'))
    assert observation._descriptor(broker, runtime / 'panel.lock', owner=relay.broker_pid) is expected


def test_child_pid_of_the_wrong_type_is_refused_before_any_process_read(family):
    runtime, relay = family
    path = runtime / shared.OWNER_FILE
    original = path.read_bytes()
    record = json.loads(original)
    record['child_pid'] = str(relay.ping()['child_pid'])
    path.write_text(json.dumps(record))
    os.chmod(path, 0o600)
    try:
        assert observe(runtime) == ()
    finally:
        path.write_bytes(original)
        os.chmod(path, 0o600)
