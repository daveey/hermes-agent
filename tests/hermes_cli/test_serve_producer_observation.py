"""Kernel-backed shared-family observation: a published READY record is only a locator."""
import json
import os
from pathlib import Path

import pytest

from hermes_cli import serve_producer_broker as shared
from hermes_cli import serve_producer_observation as observation
from hermes_cli.serve_producer_admission import LaunchGate, launcher_generation, read_participants_strict
from hermes_cli.serve_producer_census import CensusUnavailable
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


def observe(runtime, **override):
    arguments = {'installation': ROOT, 'generation': launcher_generation(ROOT)}
    arguments.update(override)
    if 'participants' not in arguments:
        arguments['participants'] = read_participants_strict(runtime)
    gate = LaunchGate.acquire(runtime)
    try:
        return observation.observe_shared_family(
            runtime, arguments['installation'], arguments['participants'], arguments['generation'], gate=gate)
    finally:
        gate.close()


def test_no_published_owner_observes_nothing(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir(mode=0o700)
    assert observe(runtime, participants=()) == ()


def test_live_family_is_observed_by_exact_pids(family):
    runtime, relay = family
    broker, child = observe(runtime)
    assert broker.identity.pid == relay.broker_pid
    assert child.identity.pid == relay.ping()['child_pid']
    assert broker.identity.pid != child.identity.pid


@pytest.mark.parametrize('change', ['generation', 'installation', 'no-tickets', 'non-broker-ticket'])
def test_enrollment_must_match_exact_ticket_generation_and_installation(family, change):
    from dataclasses import replace
    runtime, _ = family
    rows = read_participants_strict(runtime)
    override = {
        'generation': {'generation': '0' * 64},
        'installation': {'installation': ROOT.parent},
        'no-tickets': {'participants': ()},
        'non-broker-ticket': {'participants': tuple(replace(row, role='serve') for row in rows)},
    }[change]
    assert observe(runtime, **override) == ()


@pytest.mark.parametrize('child_pid', ['same', 'zero', 'text', 'missing'])
def test_owner_child_pid_must_be_a_distinct_positive_integer(family, child_pid):
    runtime, relay = family
    path = runtime / shared.OWNER_FILE
    record = json.loads(path.read_text())
    original = path.read_bytes()
    record['child_pid'] = {'same': record['pid'], 'zero': 0, 'text': str(relay.ping()['child_pid']),
                           'missing': None}[child_pid]
    if child_pid == 'missing':
        del record['child_pid']
    path.write_text(json.dumps(record))
    os.chmod(path, 0o600)
    try:
        assert observe(runtime) == ()
    finally:
        path.write_bytes(original)
        os.chmod(path, 0o600)


def test_child_pid_naming_the_broker_peer_without_locks_is_not_a_family(family):
    runtime, _ = family
    path = runtime / shared.OWNER_FILE
    original = path.read_bytes()
    record = json.loads(original)
    record['child_pid'] = os.getpid()  # a live process that holds neither lock nor exec argv
    path.write_text(json.dumps(record))
    os.chmod(path, 0o600)
    try:
        assert observe(runtime) == ()
    finally:
        path.write_bytes(original)
        os.chmod(path, 0o600)


@pytest.mark.parametrize('role, argv, good', [
    ('broker', ('p', '-I', 'R/hermes_cli/serve_producer_process.py', 'broker', '8', 'R', '-1', '10'), True),
    ('producer', ('p', '-I', 'R/hermes_cli/serve_producer_process.py', 'producer', '6', 'R', '4'), True),
    ('broker', ('p', '-I', 'R/hermes_cli/serve_producer_process.py', 'broker', '8', 'R', '-1'), False),
    ('broker', ('p', '-I', 'R/hermes_cli/serve_producer_process.py', 'broker', 'x', 'R', '-1', '10'), False),
    ('broker', ('p', '-I', 'R/hermes_cli/serve_producer_process.py', 'broker', '8', 'other', '-1', '10'), False),
    ('broker', ('p', '-I', 'R/hermes_cli/serve_producer_process.py', 'broker', '8', 'R', '3', '10'), False),
    ('broker', ('p', '-I', 'R/hermes_cli/serve_producer_process.py', 'broker', '8', 'R', '-1', 'x'), False),
    ('broker', ('p', '-c', 'R/hermes_cli/serve_producer_process.py', 'broker', '8', 'R', '-1', '10'), False),
    ('broker', ('p', '-I', 'R/hermes_cli/other.py', 'broker', '8', 'R', '-1', '10'), False),
    ('producer', ('p', '-I', 'R/hermes_cli/serve_producer_process.py', 'broker', '6', 'R', '4'), False),
    ('producer', ('p', '-I', 'R/hermes_cli/serve_producer_process.py', 'producer', '6', 'R', 'x'), False),
    ('producer', ('p', '-I', 'R/hermes_cli/serve_producer_process.py', 'producer', '6', 'R', '4', '1'), False),
    ('producer', ('p', '-I', 'R/hermes_cli/serve_producer_process.py', 'producer', '6', 'R'), False),
    ('producer', ('p', '-I', 'R/hermes_cli/serve_producer_process.py', 'producer', 'x', 'R', '4'), False),
])
def test_exec_argv_must_be_exactly_the_supported_launcher(role, argv, good):
    from types import SimpleNamespace
    root = Path('R')
    record = SimpleNamespace(argv=tuple(a.replace('R/', f'{root}/') if a.startswith('R/') else root.name if a == 'R' else a
                                        for a in argv))
    assert observation._exec(record, root, role) is good


def test_descriptor_budget_refuses_rather_than_truncating(family, monkeypatch):
    runtime, relay = family
    from hermes_cli.serve_producer_census import LinuxProcessCensus
    record = LinuxProcessCensus().read_process(relay.broker_pid, LinuxProcessCensus().boot_id())
    monkeypatch.setattr(observation, '_MAX_FDS', 1)
    with pytest.raises(CensusUnavailable):
        observation._descriptor(record, runtime / 'panel.lock', owner=relay.broker_pid)


def test_descriptor_requires_the_named_owner_of_the_exclusive_lock(family):
    runtime, relay = family
    from hermes_cli.serve_producer_census import LinuxProcessCensus
    adapter = LinuxProcessCensus()
    broker = adapter.read_process(relay.broker_pid, adapter.boot_id())
    child = adapter.read_process(relay.ping()['child_pid'], adapter.boot_id())
    assert observation._descriptor(broker, runtime / 'panel.lock', owner=broker.identity.pid)
    assert not observation._descriptor(broker, runtime / 'panel.lock', owner=child.identity.pid)
    assert not observation._descriptor(child, runtime / 'panel.lock', owner=child.identity.pid)
    assert observation._descriptor(child, runtime / 'panel-execution.lock', owner=child.identity.pid)
    assert not observation._descriptor(broker, runtime / 'panel-execution.lock', owner=broker.identity.pid)
