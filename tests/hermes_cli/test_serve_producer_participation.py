"""Positive retirement is live kernel custody, not an enrollment/readiness bit."""
import os
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cli import serve_producer_admission as admission
from hermes_cli import serve_producer_participation as participation
from hermes_cli.serve_producer_census import LinuxProcessCensus
from hermes_cli.serve_producer_custody import CustodyConflict, FamilyCustody

pytestmark = pytest.mark.platforms('linux')
ROOT = Path(__file__).parents[2]


def enrolled(tmp_path):
    runtime = tmp_path / 'runtime'
    ticket = admission.enroll_launch(runtime, 'serve', installation=ROOT)
    return runtime, ticket


def observed():
    adapter = LinuxProcessCensus()
    return adapter.read_process(os.getpid(), adapter.boot_id())


def test_no_witness_before_positive_drain_and_close_revokes(tmp_path):
    runtime, ticket = enrolled(tmp_path)
    guard = participation.PanelParticipant(runtime, ticket)
    record = observed()
    try:
        assert not participation.retired_participant(record, (ticket,), runtime, ticket.generation, str(ROOT))
        assert not guard.retire(lambda: False)
        assert not participation.retired_participant(record, (ticket,), runtime, ticket.generation, str(ROOT))
        assert guard.retire(lambda: True)
        assert participation.retired_participant(record, (ticket,), runtime, ticket.generation, str(ROOT))
        guard.close()
        assert not participation.retired_participant(record, (ticket,), runtime, ticket.generation, str(ROOT))
    finally:
        guard.close()


def test_truthy_is_not_positive_retirement(tmp_path):
    runtime, ticket = enrolled(tmp_path)
    guard = participation.PanelParticipant(runtime, ticket)
    try:
        assert not guard.retire(lambda: 1)
        assert not participation.retired_participant(observed(), (ticket,), runtime, ticket.generation, str(ROOT))
    finally:
        guard.close()


def test_legacy_owner_and_execution_fences_precede_witness_withdrawal(tmp_path):
    runtime, ticket = enrolled(tmp_path)
    guard = participation.PanelParticipant(runtime, ticket)
    try:
        assert guard.retire(lambda: True)
        with admission.LaunchGate.acquire(runtime) as gate:
            guard.begin_legacy(gate)
        assert not participation.retired_participant(observed(), (ticket,), runtime, ticket.generation, str(ROOT))
        with pytest.raises(CustodyConflict):
            FamilyCustody.acquire(runtime, 'panel')
        with pytest.raises(CustodyConflict):
            FamilyCustody.acquire_execution(runtime, 'panel')
        assert not guard.retire(lambda: False)
        with pytest.raises(CustodyConflict):
            FamilyCustody.acquire(runtime, 'panel')
        assert guard.retire(lambda: True)
        assert participation.retired_participant(observed(), (ticket,), runtime, ticket.generation, str(ROOT))
        owner = FamilyCustody.acquire(runtime, 'panel')
        owner.close()
    finally:
        guard.close()


@pytest.mark.parametrize('change', ['generation', 'installation', 'key', 'ticket', 'runtime'])
def test_witness_bound_to_exact_incarnation_generation_install_and_ticket(tmp_path, change):
    runtime, ticket = enrolled(tmp_path)
    guard = participation.PanelParticipant(runtime, ticket)
    try:
        assert guard.retire(lambda: True)
        base = observed()
        arguments = {'record': base, 'rows': (ticket,), 'domain': runtime,
                     'generation': ticket.generation, 'installation': str(ROOT)}
        arguments.update({
            'generation': {'generation': '0' * 64},
            'installation': {'installation': str(tmp_path)},
            'key': {'record': replace(base, identity=replace(base.identity, start_ticks=1))},
            'ticket': {'rows': ()},
            'runtime': {'domain': tmp_path / 'foreign'},
        }[change])
        assert not participation.retired_participant(
            arguments['record'], arguments['rows'], arguments['domain'],
            arguments['generation'], arguments['installation'])
    finally:
        guard.close()


def test_enrolled_but_uncooperative_process_is_never_retired(tmp_path):
    runtime, ticket = enrolled(tmp_path)
    assert not participation.retired_participant(observed(), (ticket,), runtime, ticket.generation, str(ROOT))


def test_role_reenrollment_cannot_reuse_retired_witness(tmp_path):
    runtime, ticket = enrolled(tmp_path)
    guard = participation.PanelParticipant(runtime, ticket)
    try:
        assert guard.retire(lambda: True)
        successor = admission.enroll_launch(runtime, 'gateway_spawner', installation=ROOT)
        assert successor.role != ticket.role
        assert not participation.retired_participant(
            observed(), (successor,), runtime, successor.generation, str(ROOT))
    finally:
        guard.close()


def test_source_start_lock_serializes_drain_through_witness_publication(tmp_path):
    import threading

    runtime, ticket = enrolled(tmp_path)
    start_lock = threading.RLock()
    guard = participation.PanelParticipant(runtime, ticket, start_lock=start_lock)
    entered, release, attempted, started = (threading.Event() for _ in range(4))
    errors = []

    def drain():
        entered.set()
        assert release.wait(5)
        return True

    def retire():
        try:
            guard.retire(drain)
        except BaseException as exc:
            errors.append(exc)

    def start():
        attempted.set()
        with start_lock:
            with admission.LaunchGate.acquire(runtime) as gate:
                guard.begin_legacy(gate)
            started.set()

    retiring = threading.Thread(target=retire)
    starting = threading.Thread(target=start)
    retiring.start()
    try:
        assert entered.wait(5)
        starting.start()
        assert attempted.wait(5)
        assert not started.is_set()
        assert not start_lock.acquire(blocking=False)
    finally:
        release.set()
        retiring.join(10)
        starting.join(10)
    assert not retiring.is_alive() and not starting.is_alive()
    assert not errors and started.is_set()
    assert not participation.retired_participant(observed(), (ticket,), runtime, ticket.generation, str(ROOT))
    assert guard.retire(lambda: True)
    guard.close()


def test_foreign_gate_cannot_start_legacy_or_withdraw_witness(tmp_path):
    runtime, ticket = enrolled(tmp_path)
    guard = participation.PanelParticipant(runtime, ticket)
    try:
        assert guard.retire(lambda: True)
        with admission.LaunchGate.acquire(tmp_path / 'foreign') as gate:
            with pytest.raises(admission.AdmissionUnavailable):
                guard.begin_legacy(gate)
        assert participation.retired_participant(observed(), (ticket,), runtime, ticket.generation, str(ROOT))
    finally:
        guard.close()


def _enroll_role(tmp_path, role):
    runtime = tmp_path / 'runtime'
    return runtime, admission.enroll_launch(runtime, role, installation=ROOT)


@pytest.mark.parametrize('role', sorted(participation.NON_HOSTING_ROLES))
def test_nonhosting_role_is_positively_non_participating_but_cannot_host(tmp_path, role):
    runtime, ticket = _enroll_role(tmp_path, role)
    record = observed()
    assert participation.nonhosting_participant(record.key, (ticket,), ticket.generation, str(ROOT))
    with pytest.raises(admission.AdmissionUnavailable):
        participation.PanelParticipant(runtime, ticket)
    from hermes_cli.serve_producer_retirement import bind_panel
    with pytest.raises(admission.AdmissionUnavailable):
        bind_panel(runtime, ROOT, lambda: True)


@pytest.mark.parametrize('role', ['serve', 'dashboard', 'plugin_host', 'producer_broker'])
def test_hosting_roles_are_never_classified_non_participating(tmp_path, role):
    _, ticket = _enroll_role(tmp_path, role)
    assert not participation.nonhosting_participant(observed().key, (ticket,), ticket.generation, str(ROOT))


@pytest.mark.parametrize('change', ['generation', 'installation', 'key', 'absent', 'duplicate'])
def test_nonhosting_classification_needs_exact_current_enrollment(tmp_path, change):
    _, ticket = _enroll_role(tmp_path, 'gateway')
    key, rows, gen, install = observed().key, (ticket,), ticket.generation, str(ROOT)
    arguments = {
        'generation': {'gen': '0' * 64}, 'installation': {'install': str(tmp_path)},
        'key': {'key': replace(key, start_ticks=1)}, 'absent': {'rows': ()},
        'duplicate': {'rows': (ticket, ticket)},
    }[change]
    key, rows, gen, install = (arguments.get('key', key), arguments.get('rows', rows),
                               arguments.get('gen', gen), arguments.get('install', install))
    assert not participation.nonhosting_participant(key, rows, gen, install)
