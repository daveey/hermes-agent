"""Bounded panel-only retirement rendezvous; replies never certify admission.

The collector lends its actual held LaunchGate description with SCM_RIGHTS. A process
may drain only while the same revision selects external mode. After the reply the collector
independently verifies a PID-owned kernel witness; cached ACKs/descriptor JSON cannot promote
an enrollment. No provider work, generic hooks, agent startup or takeover lives here.
"""
from __future__ import annotations

import array
import json
import os
import secrets
import socket
import struct
import threading
from pathlib import Path

from hermes_cli.serve_producer_admission import AdmissionUnavailable, LaunchGate
from hermes_cli.serve_producer_census import CensusUnavailable
from hermes_cli.serve_producer_custody import CustodyConflict, CustodyUnsafe
from hermes_cli.serve_producer_mode import effective_external, read_mode
from hermes_cli.serve_producer_participation import PanelParticipant, _name

_LIMIT = 4096
_SERVERS = {}
_SERVERS_LOCK = threading.RLock()


def _address(runtime, ticket, operation):
    name = _name(ticket) + '.sock'
    fd = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        return operation(f'/proc/self/fd/{fd}/{name}')
    finally:
        os.close(fd)


def _peer(sock):
    return struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[:2]


def _rights(ancillary):
    """Descriptors received as SCM_RIGHTS; every other control message is ignored."""
    fds = array.array('i')
    for level, kind, payload in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            fds.frombytes(payload[:len(payload) - len(payload) % fds.itemsize])
    return fds


class RetirementServer:
    def __init__(self, participant, root, drain):
        self.participant, self.root, self.drain = participant, Path(root), drain
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        old = os.umask(0o177)
        try:
            _address(participant.runtime, participant.ticket, self._socket.bind)
            self._socket.listen(8)
        except BaseException:
            self._socket.close()
            raise
        finally:
            os.umask(old)
        self._socket.settimeout(0.25)
        self._thread = threading.Thread(target=self._run, name='panel-retirement-control', daemon=True)
        self._thread.start()

    def _run(self):
        while True:  # close() closes the socket, so accept() fails and ends the loop
            try:
                conn, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(1)
                try:
                    self._request(conn)
                except (OSError, ValueError, AdmissionUnavailable, CensusUnavailable,
                        CustodyConflict, CustodyUnsafe):
                    continue  # no witness and no success reply on uncertainty

    def _request(self, conn):
        if _peer(conn)[1] != os.getuid():  # windows-footgun: ok — Linux adapter
            return
        data, ancillary, _, _ = conn.recvmsg(_LIMIT + 1, socket.CMSG_SPACE(4 * 8))
        fds = _rights(ancillary)
        try:
            # Truncated payloads/ancillary data always change these counts (limit+1 read; 4 fd slots).
            if len(data) > _LIMIT or len(fds) != 1:
                return
            value = json.loads(data)
            if not isinstance(value, dict) or set(value) != {'nonce', 'revision', 'generation'}:
                return
            nonce = value['nonce']
            if (type(nonce) is not str or len(nonce) != 32
                    or type(value['revision']) is not int
                    or value['generation'] != self.participant.ticket.generation):
                return
            if (not effective_external(self.root)
                    or read_mode(self.root)['revision'] != value['revision']):
                return
            # adopt owns this received descriptor only; the caller retains its original.
            fd = fds.pop()
            with LaunchGate.adopt(self.participant.runtime, fd) as gate:
                success = self.participant.retire(self.drain, gate=gate)
                conn.sendall(json.dumps({'nonce': nonce, 'retired': success}).encode('ascii'))
        finally:
            for fd in fds:
                os.close(fd)

    def close(self):
        self._socket.close()
        self._thread.join(5)
        if self._thread.is_alive():
            raise AdmissionUnavailable('retirement callback still draining')
        self.participant.close()
        _address(self.participant.runtime, self.participant.ticket, os.unlink)
        with _SERVERS_LOCK:
            identity = (str(self.participant.runtime), self.participant.ticket.key)
            if _SERVERS.get(identity) is self:
                del _SERVERS[identity]


def bind_panel(runtime, root, drain, *, start_lock=None):
    """Bind only an exact supported local launch; absent enrollment is not disabled."""
    from hermes_cli.serve_producer_admission import read_participants_strict
    from hermes_cli.serve_producer_census import LinuxProcessCensus

    runtime = Path(runtime).resolve(strict=True)
    adapter = LinuxProcessCensus()
    key = adapter.read_process(os.getpid(), adapter.boot_id()).key
    rows = [row for row in read_participants_strict(runtime) if row.key == key]
    if len(rows) != 1:
        raise AdmissionUnavailable('supported local launch required for panel binding')
    ticket = rows[0]
    with _SERVERS_LOCK:
        identity = (str(runtime), ticket.key)
        server = _SERVERS.get(identity)
        if server is None:
            server = RetirementServer(PanelParticipant(runtime, ticket, start_lock=start_lock), root, drain)
            _SERVERS[identity] = server
        else:
            server.participant._check()
            server.drain = drain  # reload keeps the same custody object, not a second incarnation
        return server


def request_retirement(runtime, ticket, root, gate, *, timeout=35):
    """Control lane only. A successful response is NOT a successful census classification."""
    gate.require_runtime(runtime)
    revision = read_mode(root)['revision']
    nonce = secrets.token_hex(16)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    sock.settimeout(timeout)
    with sock:
        _address(runtime, ticket, sock.connect)
        if _peer(sock) != (ticket.identity.pid, os.getuid()):  # windows-footgun: ok — Linux adapter
            raise AdmissionUnavailable('retirement endpoint identity changed')
        value = json.dumps({'nonce': nonce, 'revision': revision, 'generation': ticket.generation}).encode('ascii')
        sock.sendmsg([value], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [gate.fd]))])
        raw, _, _, _ = sock.recvmsg(_LIMIT + 1)  # a truncated reply is exactly limit+1 bytes
        if len(raw) > _LIMIT:
            raise AdmissionUnavailable('retirement reply budget')
        reply = json.loads(raw)
        if reply != {'nonce': nonce, 'retired': True}:
            raise AdmissionUnavailable('panel lanes not positively retired')
