"""Controlled hub-shaped census plus actual kernel family-custody observations."""
import os
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cli import serve_producer_admission as admission
from hermes_cli import serve_producer_enrollment as enrollment
from hermes_cli.serve_producer_census import LinuxProcessCensus, ProcessCensus
from hermes_cli.serve_producer_custody import CustodyConflict, FamilyCustody

pytestmark = pytest.mark.platforms('linux')
ROOT = Path(__file__).parents[2]


def broker_record(monkeypatch):
    adapter = LinuxProcessCensus()
    record = adapter.read_process(os.getpid(), adapter.boot_id())
    record = replace(record, argv=('python', '-I', str(ROOT / 'hermes_cli/serve_producer_process.py'), 'broker', '7'))
    monkeypatch.setattr(LinuxProcessCensus, 'read_process', lambda *_: record)
    return record


def test_live_custody_promotes_only_exact_broker_and_close_revokes(tmp_path, monkeypatch):
    record = broker_record(monkeypatch)
    runtime = tmp_path / 'runtime'
    broker = enrollment.BrokerEnrollment.acquire(runtime, ROOT)
    ticket = broker.ticket
    participants = admission.read_participants_strict(runtime)
    def classify(value=record, **overrides):
        options = dict(installation=str(ROOT), runtime=runtime, broker=broker)
        options.update(overrides)
        return enrollment.classify_with_broker(value, participants, ticket.generation, **options)
    try:
        assert not ticket.guarded  # persisted launch ticket never becomes authority
        assert classify().kind == 'participant'
        with pytest.raises(CustodyConflict):
            FamilyCustody.acquire(runtime, 'panel')
        assert classify(replace(record, identity=replace(record.identity, start_ticks=1))).kind != 'participant'
        assert classify(replace(record, argv=('python',))).kind != 'participant'
        assert classify(runtime=tmp_path / 'foreign').kind != 'participant'
        assert classify(broker=replace(ticket, guarded=True)).kind != 'participant'
        assert classify(installation='/foreign').kind != 'participant'
        broker.close()
        assert classify().kind == 'legacy'
    finally:
        broker.close()


def test_complete_controlled_hub_shaped_cut_requires_guarded_broker_and_legacy_retirement(tmp_path, monkeypatch):
    record = broker_record(monkeypatch)
    runtime = tmp_path / 'runtime'
    broker = enrollment.BrokerEnrollment.acquire(runtime, ROOT)
    ticket = broker.ticket
    participants = admission.read_participants_strict(runtime)
    unrelated = replace(record, identity=replace(record.identity, pid=record.identity.pid + 100000),
                        cwd='/other', exe='/usr/bin/other', argv=('other',), environment=())
    observed = ProcessCensus(ticket.identity.boot_id, 0.0, 1.0, (record, unrelated), (), ())
    collectors = {name: lambda: admission.CollectorEvidence(('positive scoped fixture',), ())
                  for name in admission.REQUIRED_COLLECTORS}
    def cut(gate, *, guarded, legacy=(), exited=lambda _: False):
        return gate.inspect(boot_id=ticket.identity.boot_id, generation=ticket.generation,
            installation=str(ROOT), census=lambda: observed, collectors=collectors,
            classify=lambda row: enrollment.classify_with_broker(row, participants, ticket.generation,
                installation=str(ROOT), runtime=runtime, broker=broker if guarded else None),
            legacy_candidates=legacy, exited=exited)
    try:
        with admission.LaunchGate.acquire(runtime) as gate:
            assert not cut(gate, guarded=False).complete
            assert cut(gate, guarded=True).complete
            assert not cut(gate, guarded=True, legacy=(unrelated.identity,)).complete
            assert cut(gate, guarded=True, legacy=(unrelated.identity,), exited=lambda _: True).complete
            broker.close()
            assert not cut(gate, guarded=True).complete
    finally:
        broker.close()


def test_ordinary_serve_cannot_promote_ticket(tmp_path):
    with pytest.raises(admission.AdmissionUnavailable, match='exec broker'):
        enrollment.BrokerEnrollment.acquire(tmp_path / 'runtime', ROOT)


def test_closed_descriptor_reopened_without_flock_does_not_certify(tmp_path, monkeypatch):
    record = broker_record(monkeypatch)
    runtime = tmp_path / 'runtime'
    broker = enrollment.BrokerEnrollment.acquire(runtime, ROOT)
    fd = broker.owner.fd
    os.close(fd)
    replacement = os.open(runtime / 'panel.lock', os.O_RDWR)
    try:
        if replacement != fd:
            os.dup2(replacement, fd)
        assert not broker.matches(record, runtime=runtime, installation=str(ROOT), generation=broker.ticket.generation)
    finally:
        broker.close()
        if replacement != fd:
            os.close(replacement)
