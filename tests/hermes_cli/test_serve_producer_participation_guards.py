"""Each refusal in the panel participation/witness checks has its own killing test."""
import fcntl
import os
from dataclasses import fields, replace
from pathlib import Path

import pytest

from hermes_cli import serve_producer_admission as admission
from hermes_cli import serve_producer_participation as participation
from hermes_cli.serve_producer_census import LinuxProcessCensus
from hermes_cli.serve_producer_custody import FamilyCustody

pytestmark = pytest.mark.platforms('linux')
ROOT = Path(__file__).parents[2]


def enrolled(tmp_path, role='serve'):
    runtime = tmp_path / 'runtime'
    return runtime, admission.enroll_launch(runtime, role, installation=ROOT)


def observed():
    adapter = LinuxProcessCensus()
    return adapter.read_process(os.getpid(), adapter.boot_id())


def retired(ticket, runtime, **override):
    arguments = {'record': observed(), 'rows': (ticket,), 'runtime': runtime,
                 'generation': ticket.generation, 'installation': str(ROOT)}
    arguments.update(override)
    return participation.retired_participant(
        arguments['record'], arguments['rows'], arguments['runtime'],
        arguments['generation'], arguments['installation'])


def witness_path(runtime, ticket):
    return Path(runtime) / (participation._name(ticket) + '.lock')


@pytest.fixture
def held(tmp_path):
    runtime, ticket = enrolled(tmp_path)
    guard = participation.PanelParticipant(runtime, ticket)
    assert guard.retire(lambda: True)
    assert retired(ticket, runtime)
    try:
        yield runtime, ticket, guard
    finally:
        guard.close()


def hand_made_witness(runtime, ticket, *, locked, content=None, keep=None):
    """A private witness file shaped like the real one; `locked` decides if its description holds FLOCK."""
    path = witness_path(runtime, ticket)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    if locked:
        fcntl.flock(fd, fcntl.LOCK_EX)
    os.write(fd, (str(fd) if content is None else content(fd)).encode('ascii'))
    return fd


# ---- _check refusals (every disjunct needs its own failure) ----

def test_closed_participant_refuses_every_use(tmp_path):
    runtime, ticket = enrolled(tmp_path)
    guard = participation.PanelParticipant(runtime, ticket)
    guard.close()
    with pytest.raises(admission.AdmissionUnavailable):
        guard._check()


def test_ticket_must_be_an_exact_launch_receipt(tmp_path):
    runtime, ticket = enrolled(tmp_path)

    class Spoof(admission.LaunchReceipt):
        def __eq__(self, other):
            return True

        __hash__ = None  # type: ignore[assignment]

    spoof = Spoof(**{field.name: getattr(ticket, field.name) for field in fields(ticket)})
    with pytest.raises(admission.AdmissionUnavailable):
        participation.PanelParticipant(runtime, spoof)


def test_ticket_for_another_process_is_refused(tmp_path, monkeypatch):
    runtime, ticket = enrolled(tmp_path)
    monkeypatch.setattr(participation.os, 'getpid', lambda: ticket.identity.pid + 1)
    with pytest.raises(admission.AdmissionUnavailable):
        participation.PanelParticipant(runtime, ticket)


def test_producer_broker_role_can_never_participate(tmp_path):
    runtime, ticket = enrolled(tmp_path, 'producer_broker')
    with pytest.raises(admission.AdmissionUnavailable):
        participation.PanelParticipant(runtime, ticket)


@pytest.mark.parametrize('change', ['not-current', 'not-enrolled', 'other-generation'])
def test_exact_current_enrolled_generation_is_required(tmp_path, monkeypatch, change):
    runtime, ticket = enrolled(tmp_path)
    if change == 'not-current':
        monkeypatch.setattr(participation, '_current', lambda row: False)
    elif change == 'not-enrolled':
        monkeypatch.setattr(participation, 'read_participants_strict', lambda r: ())
    else:
        monkeypatch.setattr(participation, 'launcher_generation', lambda p: '0' * 64)
    with pytest.raises(admission.AdmissionUnavailable):
        participation.PanelParticipant(runtime, ticket)


def test_begin_legacy_is_idempotent_and_keeps_custody(tmp_path):
    runtime, ticket = enrolled(tmp_path)
    guard = participation.PanelParticipant(runtime, ticket)
    try:
        with admission.LaunchGate.acquire(runtime) as gate:
            guard.begin_legacy(gate)
            guard.begin_legacy(gate)  # a reload/retained callback restarts without a second acquisition
        with pytest.raises(admission.CustodyConflict):
            FamilyCustody.acquire(runtime, 'panel')
    finally:
        assert guard.retire(lambda: True)
        guard.close()


def test_retire_with_a_supplied_gate_never_waits_for_a_second_gate(tmp_path):
    runtime, ticket = enrolled(tmp_path)
    guard = participation.PanelParticipant(runtime, ticket)
    try:
        with admission.LaunchGate.acquire(runtime) as gate:
            assert guard.retire(lambda: True, gate=gate)  # a second acquisition would wait 10s and fail
    finally:
        guard.close()


def test_retire_without_legacy_never_touches_foreign_family_custody(tmp_path):
    runtime, ticket = enrolled(tmp_path)
    guard = participation.PanelParticipant(runtime, ticket)
    foreign = FamilyCustody.acquire(runtime, 'panel')  # e.g. the external broker's family
    try:
        assert guard.retire(lambda: True)
    finally:
        foreign.close()
        guard.close()


# ---- witness file interpretation ----

@pytest.mark.parametrize('content', [lambda fd: f' {fd}', lambda fd: f'+{fd}', lambda fd: f'{fd:09d}'],
                         ids=['space', 'plus', 'overlong'])
def test_only_a_short_plain_decimal_locator_is_accepted(held, content):
    runtime, ticket, _guard = held
    path = witness_path(runtime, ticket)
    fd = int(path.read_text())
    path.write_text(content(fd))
    os.chmod(path, 0o600)
    assert not retired(ticket, runtime)


def test_locator_must_name_the_same_file(tmp_path):
    runtime, ticket = enrolled(tmp_path)
    other = os.open(tmp_path / 'other.lock', os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(other, fcntl.LOCK_EX)  # a genuine exclusive lock, but on a different inode
    fd = hand_made_witness(runtime, ticket, locked=False, content=lambda _: str(other))
    try:
        assert not retired(ticket, runtime)
    finally:
        os.close(fd)
        os.close(other)


def test_unlocked_reopen_of_the_same_inode_is_not_a_witness(tmp_path):
    runtime, ticket = enrolled(tmp_path)
    fd = hand_made_witness(runtime, ticket, locked=False)
    try:
        assert not retired(ticket, runtime)
    finally:
        os.close(fd)
    fd = hand_made_witness(runtime, ticket, locked=True)  # control: same shape, but actually locked
    try:
        assert retired(ticket, runtime)
    finally:
        os.close(fd)


def test_lock_must_be_held_by_the_recorded_producer_role(tmp_path):
    runtime, ticket = enrolled(tmp_path, 'producer_broker')
    fd = hand_made_witness(runtime, ticket, locked=True)
    try:
        assert not retired(ticket, runtime)
    finally:
        os.close(fd)


def test_installation_and_generation_are_both_exact_witness_inputs(held, monkeypatch):
    runtime, ticket, _ = held
    monkeypatch.setattr(participation, 'launcher_generation', lambda p: ticket.generation)
    assert not retired(ticket, runtime, installation=str(runtime.parent))  # row installation differs
    monkeypatch.setattr(participation, 'launcher_generation', lambda p: '1' * 64)
    assert not retired(ticket, runtime, generation='1' * 64)  # row generation differs
    assert not retired(ticket, runtime)  # source changed since the witness was published


def test_witness_is_reread_and_restamped_after_the_lock_probe(held, monkeypatch):
    runtime, ticket, _ = held
    path = witness_path(runtime, ticket)
    real = participation._read_bytes
    original = path.read_bytes()
    seen = []

    def swap(target, limit, **kwargs):
        if Path(target) == path:
            seen.append(1)
            if len(seen) == 2:
                path.write_bytes(original + b'0')  # same locator value, different bytes
                os.chmod(path, 0o600)
        return real(target, limit, **kwargs)

    monkeypatch.setattr(participation, '_read_bytes', swap)
    assert not retired(ticket, runtime)
    assert len(seen) >= 2


def test_witness_stamp_must_not_move_during_the_probe(held, monkeypatch):
    runtime, ticket, _ = held
    path = witness_path(runtime, ticket)
    real = participation._read_bytes
    seen = []

    def touch(target, limit, **kwargs):
        value = real(target, limit, **kwargs)
        if Path(target) == path:
            seen.append(1)
            if len(seen) == 2:
                os.utime(path, ns=(1, 1))  # bytes identical, mtime/ctime moved
        return value

    monkeypatch.setattr(participation, '_read_bytes', touch)
    assert not retired(ticket, runtime)
    assert len(seen) >= 2


def fdinfo_probe(monkeypatch, lines):
    real = participation._read_bytes

    def fake(target, limit, **kwargs):
        if str(target).startswith('/proc/') and '/fdinfo/' in str(target):
            return '\n'.join(lines).encode('ascii')
        return real(target, limit, **kwargs)

    monkeypatch.setattr(participation, '_read_bytes', fake)


@pytest.mark.parametrize('line, expected', [
    ('lock:\t1: FLOCK  ADVISORY  WRITE {pid} 08:01:7 0 EOF', True),
    ('xlock:\t1: FLOCK  ADVISORY  WRITE {pid} 08:01:7 0 EOF', False),
    ('lock:\t1: POSIX  ADVISORY  WRITE {pid} 08:01:7 0 EOF', False),
    ('lock:\t1: FLOCK  ADVISORY  READ {pid} 08:01:7 0 EOF', False),
    ('lock:\t1: FLOCK  ADVISORY  WRITE 1 08:01:7 0 EOF', False),
], ids=['exact', 'not-a-lock-line', 'not-flock', 'not-exclusive', 'other-owner'])
def test_fdinfo_lock_line_must_be_an_exclusive_flock_owned_by_the_pid(held, monkeypatch, line, expected):
    runtime, ticket, _ = held
    fdinfo_probe(monkeypatch, ['pos:\t0', line.format(pid=os.getpid())])
    assert retired(ticket, runtime) is expected


def test_nonhosting_classification_requires_the_process_to_be_current(tmp_path, monkeypatch):
    _, ticket = enrolled(tmp_path, 'gateway')
    args = (observed().key, (ticket,), ticket.generation, str(ROOT))
    assert participation.nonhosting_participant(*args)
    monkeypatch.setattr(participation, '_current', lambda row: False)
    assert not participation.nonhosting_participant(*args)
