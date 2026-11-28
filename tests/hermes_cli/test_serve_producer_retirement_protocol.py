"""Exact wire/validation boundaries of the panel retirement rendezvous (one refusal per test row)."""
import array
import json
import os
import socket
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cli import serve_producer_admission as admission
from hermes_cli import serve_producer_retirement as retirement
from hermes_cli.serve_producer_mode import read_mode, write_mode

pytestmark = pytest.mark.platforms('linux')
ROOT = Path(__file__).parents[2]
NONCE = 'a' * 32


@pytest.fixture
def served(tmp_path):
    calls = []
    runtime = tmp_path / 'runtime'
    ticket = admission.enroll_launch(runtime, 'serve', installation=ROOT)
    server = retirement.bind_panel(runtime, tmp_path, lambda: calls.append('drain') or True)
    write_mode(tmp_path, external=True)
    try:
        yield runtime, ticket, server, calls, tmp_path
    finally:
        server.close()


def message(ticket, root, **override):
    value = {'nonce': NONCE, 'revision': read_mode(root)['revision'], 'generation': ticket.generation}
    value.update(override)
    return json.dumps(value).encode('ascii')


def exchange(runtime, ticket, payload, fd_count=1, ancillary=None):
    """Send `payload` with `fd_count` lent descriptors; return the reply bytes (b'' = refused)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    sock.settimeout(5)
    with sock, admission.LaunchGate.acquire(runtime) as gate:
        retirement._address(runtime, ticket, sock.connect)
        rights = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [gate.fd] * fd_count))] if fd_count else []
        try:
            sock.sendmsg([payload], rights + list(ancillary or ()))
            return sock.recv(8192)
        except (ConnectionResetError, BrokenPipeError):
            return b''  # the server refused and hung up before/while we were still talking


def assert_refused_then_still_serving(served, payload, **kwargs):
    runtime, ticket, _, calls, root = served
    assert exchange(runtime, ticket, payload, **kwargs) == b''
    assert calls == []
    # the control thread survived the malformed request
    assert json.loads(exchange(runtime, ticket, message(ticket, root))) == {'nonce': NONCE, 'retired': True}
    assert calls == ['drain']


def test_valid_request_is_the_only_one_that_drains(served):
    runtime, ticket, _, calls, root = served
    assert json.loads(exchange(runtime, ticket, message(ticket, root))) == {'nonce': NONCE, 'retired': True}
    assert calls == ['drain']


@pytest.mark.parametrize('name, payload', [
    ('not-json', b'not json'),
    ('list-of-the-key-names', json.dumps(['nonce', 'revision', 'generation']).encode()),
    ('extra-key', None),
    ('missing-key', json.dumps({'nonce': NONCE, 'generation': 'x'}).encode()),
    ('integer-nonce', 'int'),
    ('short-nonce', 'short'),
    ('float-revision', 'float'),
    ('wrong-generation', 'generation'),
    ('stale-revision', 'revision'),
])
def test_malformed_or_stale_request_never_drains(served, name, payload):
    _, ticket, _, _, root = served
    revision = read_mode(root)['revision']
    payload = {
        'extra-key': json.dumps({'nonce': NONCE, 'revision': revision, 'generation': ticket.generation,
                                 'extra': 1}).encode(),
        'int': message(ticket, root, nonce=12345678901234567890123456789012),
        'short': message(ticket, root, nonce='a' * 31),
        'float': json.dumps({'nonce': NONCE, 'revision': float(revision), 'generation': ticket.generation}).encode(),
        'generation': message(ticket, root, generation='0' * 64),
        'revision': message(ticket, root, revision=revision + 1),
    }.get(payload or name, payload)
    assert_refused_then_still_serving(served, payload)


def test_off_mode_never_drains(served):
    runtime, ticket, _, calls, root = served
    write_mode(root, external=False)
    assert exchange(runtime, ticket, message(ticket, root)) == b''
    assert calls == []


@pytest.mark.parametrize('fd_count', [0, 2, 5])
def test_exactly_one_lent_descriptor_is_required(served, fd_count):
    _, ticket, _, _, root = served
    assert_refused_then_still_serving(served, message(ticket, root), fd_count=fd_count)


@pytest.mark.parametrize('size', [4097, 6000])
def test_oversized_request_is_refused_even_when_json_is_valid(served, size):
    _, ticket, _, _, root = served
    padded = message(ticket, root).ljust(size, b' ')
    assert len(padded) == size
    assert_refused_then_still_serving(served, padded)


def test_non_rights_control_messages_are_not_descriptors():
    creds = array.array('i', [1, 2, 3]).tobytes()
    assert retirement._rights([(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, creds)]) == array.array('i')
    assert retirement._rights([(0, socket.SCM_RIGHTS, creds)]) == array.array('i')
    got = retirement._rights([(socket.SOL_SOCKET, socket.SCM_RIGHTS, creds + b'x')])
    assert list(got) == [1, 2, 3]  # partial trailing bytes are dropped, never a fourth descriptor


def test_foreign_uid_peer_is_ignored(served, monkeypatch):
    runtime, ticket, _, calls, root = served
    monkeypatch.setattr(retirement, '_peer', lambda sock: (os.getpid(), os.getuid() + 1))
    assert exchange(runtime, ticket, message(ticket, root)) == b''
    assert calls == []


def test_client_refuses_an_endpoint_owned_by_another_pid_before_sending(served, monkeypatch):
    runtime, ticket, _, calls, root = served
    monkeypatch.setattr(retirement, '_peer', lambda sock: (os.getpid() + 1, os.getuid()))
    with admission.LaunchGate.acquire(runtime) as gate:
        with pytest.raises(admission.AdmissionUnavailable, match='identity changed'):
            retirement.request_retirement(runtime, ticket, root, gate)
    assert calls == []


def serve_reply(runtime, ticket, reply_for):
    """A fake endpoint (this process owns it, so peer credentials match) with a scripted reply."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    retirement._address(runtime, ticket, listener.bind)
    listener.listen(1)

    def run():
        conn, _ = listener.accept()
        with conn:
            data, _, _, _ = conn.recvmsg(4097, socket.CMSG_SPACE(4 * 8))
            conn.send(reply_for(json.loads(data)['nonce']))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return listener, thread


def request_against_fake(tmp_path, reply_for):
    runtime = tmp_path / 'runtime'
    ticket = admission.enroll_launch(runtime, 'serve', installation=ROOT)
    write_mode(tmp_path, external=True)
    listener, thread = serve_reply(runtime, ticket, reply_for)
    try:
        with admission.LaunchGate.acquire(runtime) as gate:
            retirement.request_retirement(runtime, ticket, tmp_path, gate, timeout=5)
    finally:
        thread.join(5)
        listener.close()


def positive(nonce):
    return json.dumps({'nonce': nonce, 'retired': True}).encode('ascii')


def test_exact_reply_is_accepted(tmp_path):
    request_against_fake(tmp_path, positive)


@pytest.mark.parametrize('size', [4097, 5000])
def test_oversized_reply_is_a_budget_refusal_not_a_json_error(tmp_path, size):
    with pytest.raises(admission.AdmissionUnavailable, match='reply budget'):
        request_against_fake(tmp_path, lambda nonce: positive(nonce).ljust(size, b' '))


@pytest.mark.parametrize('reply', [
    lambda nonce: json.dumps({'nonce': 'b' * 32, 'retired': True}).encode(),
    lambda nonce: json.dumps({'nonce': nonce, 'retired': False}).encode(),
])
def test_only_the_exact_positive_reply_is_a_retirement(tmp_path, reply):
    with pytest.raises(admission.AdmissionUnavailable, match='not positively retired'):
        request_against_fake(tmp_path, reply)


def test_bind_requires_exactly_the_one_current_enrollment(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    ticket = admission.enroll_launch(runtime, 'serve', installation=ROOT)
    other = replace(ticket, identity=replace(ticket.identity, pid=ticket.identity.pid + 1))
    assert other.key != ticket.key
    for rows in ((), (ticket, ticket)):
        monkeypatch.setattr(admission, 'read_participants_strict', lambda r, rows=rows: rows)
        with pytest.raises(admission.AdmissionUnavailable, match='supported local launch'):
            retirement.bind_panel(runtime, tmp_path, lambda: True)
    monkeypatch.setattr(admission, 'read_participants_strict', lambda r: (other, ticket))
    server = retirement.bind_panel(runtime, tmp_path, lambda: True)  # an unrelated row is not a duplicate
    server.close()


def test_close_unregisters_only_itself_and_never_fails_on_a_missing_entry(tmp_path):
    runtime = tmp_path / 'runtime'
    ticket = admission.enroll_launch(runtime, 'serve', installation=ROOT)
    server = retirement.bind_panel(runtime, tmp_path, lambda: True)
    identity = (str(server.participant.runtime), ticket.key)
    assert retirement._SERVERS[identity] is server
    server.close()
    assert identity not in retirement._SERVERS

    server = retirement.bind_panel(runtime, tmp_path, lambda: True)
    sentinel = object()
    retirement._SERVERS[identity] = sentinel  # a different server now owns this identity
    server.close()
    assert retirement._SERVERS[identity] is sentinel
    del retirement._SERVERS[identity]

    server = retirement.bind_panel(runtime, tmp_path, lambda: True)
    del retirement._SERVERS[identity]
    server.close()  # already unregistered: no KeyError


def test_close_refuses_to_report_success_while_the_callback_still_drains(tmp_path):
    runtime = tmp_path / 'runtime'
    admission.enroll_launch(runtime, 'serve', installation=ROOT)
    server = retirement.bind_panel(runtime, tmp_path, lambda: True)
    real = server._thread

    class Stuck:
        def join(self, timeout=None):
            return None

        def is_alive(self):
            return True

    server.__dict__['_thread'] = Stuck()
    try:
        with pytest.raises(admission.AdmissionUnavailable, match='still draining'):
            server.close()
    finally:
        server.__dict__['_thread'] = real
        server.close()
