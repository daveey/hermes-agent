"""Shared installation-local control broker for the exec panel producer (dormant).

One broker process per installation runtime owns the panel family custody and ONE exec
producer child; compatible serves attach as clients over a private Unix socket instead of
each spawning their own relay. The broker owns no source work: it only forwards bounded
PRODUCE/EVENTS frames to the child and answers PING/STOP itself.

READY is one atomic publication: ``broker.json`` is renamed into place only after custody,
the child's pre-import execution fence, manifest verification and the completed initial
publication all succeeded. A client never sees partial READY. Unpublishing happens first on
shutdown. There is no kill, timeout takeover, LOCK_UN or inode replacement: a surviving
child/descendant keeps the kernel fences, and a successor refuses until they retire.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import stat
import struct
import threading
import uuid
from pathlib import Path

OWNER_FILE = 'broker.json'
SOCKET_FILE = 'broker.sock'
MAX_CLIENTS = 8
MAX_OWNER_BYTES = 4096
VERSION = 1


class OwnerUnavailable(RuntimeError):
    """No usable ready owner (absent, malformed, refused or not yet ready)."""


class IncompatibleOwner(OwnerUnavailable):
    """A live owner runs different provider bytes/home: an unavailable observer, never a takeover."""


def compatibility(spec, home) -> str:
    blob = json.dumps({'v': VERSION, 'provider': spec, 'home': str(home)}, sort_keys=True,
                      separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(blob.encode('ascii')).hexdigest()


def process_start(pid: int):
    """Kernel start time of a live ``pid`` (Linux), or None when it is gone or a zombie."""
    try:
        text = Path(f'/proc/{pid}/stat').read_text(encoding='ascii', errors='replace')
    except OSError:
        return None
    fields = text.rsplit(')', 1)[-1].split()
    if len(fields) <= 19 or fields[0] in ('Z', 'X'):
        return None  # an unreaped zombie has already released every descriptor and lock
    return fields[19]


def owner_alive(info) -> bool:
    return process_start(info['pid']) == info['start']


def _hex(value, length) -> bool:
    return (type(value) is str and len(value) == length
            and all(c in '0123456789abcdef' for c in value))


def read_owner(runtime):
    """The published READY record, or None when absent or its broker has exited.

    A malformed/foreign-owned/over-permissive record is a refusal (OwnerUnavailable), never
    treated as absent: absence is the only state that permits a spawn attempt.
    """
    path = Path(runtime) / OWNER_FILE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OwnerUnavailable('owner record unreadable') from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()  # windows-footgun: ok -- linux-only broker (AF_UNIX + /proc)
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > MAX_OWNER_BYTES):
            raise OwnerUnavailable('owner record not private')
        data = os.read(fd, MAX_OWNER_BYTES + 1)
    finally:
        os.close(fd)
    try:
        record = json.loads(data.decode('ascii'))
    except ValueError as exc:
        raise OwnerUnavailable('owner record malformed') from exc
    if (type(record) is not dict or record.get('version') != VERSION or record.get('ready') is not True
            or type(record.get('pid')) is not int or record['pid'] <= 0
            or type(record.get('start')) is not str or not _hex(record.get('capability'), 64)
            or not _hex(record.get('generation'), 32) or not _hex(record.get('digest'), 64)):
        raise OwnerUnavailable('owner record malformed')
    return record if owner_alive(record) else None


def _with_address(runtime, operation):
    address = str(Path(runtime) / SOCKET_FILE)
    if len(os.fsencode(address)) < 100:
        return operation(address)
    fd = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        return operation(f'/proc/self/fd/{fd}/{SOCKET_FILE}')
    finally:
        os.close(fd)


def connect_owner(runtime, timeout):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        _with_address(runtime, sock.connect)
    except OSError:
        sock.close()
        raise
    return sock


def _peer_uid(conn) -> int:
    return struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                              struct.calcsize('3i')))[1]


def _publish_owner(runtime: Path, record: dict) -> None:
    encoded = json.dumps(record, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    tmp = runtime / (OWNER_FILE + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, runtime / OWNER_FILE)  # the one atomic READY publication


def _unpublish_owner(runtime: Path) -> None:
    try:
        os.unlink(runtime / OWNER_FILE)
    except FileNotFoundError:
        pass


def _listener(runtime: Path):
    def bind(address):
        try:
            os.unlink(address)  # we hold panel custody: a leftover socket cannot have a live owner
        except FileNotFoundError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            sock.bind(address)
        finally:
            os.umask(old)
        sock.listen(4 * MAX_CLIENTS)
        return sock
    return _with_address(runtime, bind)


def run_shared(channel, config, *, gate=None) -> None:
    """Broker role entry (``serve_producer_process.main``). ``channel`` is the spawner's one-shot
    startup channel: it carries only READY/REFUSED and is closed before serving."""
    from hermes_cli._startup_fast import project_root_str
    from hermes_cli.serve_producer_custody import CustodyConflict, FamilyCustody
    from hermes_cli.serve_producer_enrollment import BrokerEnrollment
    from hermes_cli.serve_producer_process import authorized, shutdown_channel
    from hermes_cli.serve_producer_runtime import spawn

    runtime = Path(config['runtime'])
    config = dict(config, capability=secrets.token_hex(32), generation=secrets.token_hex(16))
    initial = config.pop('initial', None)

    def refuse(reason):
        try:
            channel.send({'verb': 'REFUSED', 'error': reason}, 'ready')
        finally:
            channel.close()

    try:
        from hermes_cli.serve_producer_manifest import verify_declared
        spec = config['provider']
        if spec.get('manifest') is None:
            raise ValueError('manifest required')
        verify_declared(spec['manifest'], Path(spec['path']).parent)  # before custody, child and import
    except (ValueError, KeyError, TypeError, AttributeError, OSError):
        return refuse('manifest')
    if initial is None:
        return refuse('initial')  # complete initial is mandatory; READY never precedes materialization

    if gate is None:
        return refuse('admission gate missing')
    gate._held()
    try:
        import sys
        enrollment = (BrokerEnrollment.acquire(runtime, Path(project_root_str()), gate=gate)
                      if sys.platform == 'linux' else None)
        owner = enrollment.owner if enrollment else FamilyCustody.acquire(runtime, 'panel')
    except CustodyConflict:
        return refuse('custody')
    child, data = spawn('producer', config, custody_fd=owner.fd)
    try:
        hello = data.receive()
        data.send({'verb': 'PRODUCE', 'key': 'initial', 'payload': initial,
                   'capability': config['capability'], 'generation': config['generation']},
                  uuid.uuid4().hex)
        first = data.receive()
        if first.get('verb') != 'PUBLISHED' or not first.get('wire'):
            raise EOFError('initial publication refused')
    except (OSError, EOFError, ValueError):
        shutdown_channel(data)
        data.close()
        child.wait()  # no timeout, no kill: a surviving child keeps the inherited fences
        owner.close()
        return refuse('child')

    stop = threading.Event()
    child_lock = threading.Lock()
    state = {'child': child, 'data': data, 'hello': hello}
    conns, conns_lock = set(), threading.Lock()

    def forward(request):
        with child_lock:
            try:
                return _forward_locked(request)
            except (EOFError, OSError, ValueError):
                stop.set()  # restart refused (descendants keep the fences) or budget spent: drain, no fallback
                raise

    def _forward_locked(request):
        for attempt in range(3):
            try:
                state['data'].send(request, uuid.uuid4().hex)
                return state['data'].receive()
            except (EOFError, BrokenPipeError, ConnectionResetError):
                # waitpid reaps only the leader; the replacement must re-take the execution
                # fence before any provider import, and refuses over surviving descendants.
                state['child'].wait()
                state['data'].close()
                if attempt == 2 or stop.is_set() or stop.wait(1 << attempt):
                    raise EOFError('producer restart budget') from None
                state['child'], state['data'] = spawn('producer', config, custody_fd=owner.fd)
                state['hello'] = state['data'].receive()

    def serve(conn):
        from hermes_cli.serve_producer_runtime import Channel
        peer = Channel(conn)
        try:
            if _peer_uid(conn) != os.getuid():  # windows-footgun: ok -- linux-only broker
                return
            while not stop.is_set():
                request = peer.receive()
                if not authorized(request, config):
                    peer.send({'verb': 'REFUSED'}, uuid.uuid4().hex)
                    continue
                verb = request.get('verb')
                if verb in ('PRODUCE', 'EVENTS'):
                    peer.send(forward(request), uuid.uuid4().hex)
                elif verb == 'PING':
                    with conns_lock:
                        clients = len(conns)
                    peer.send({'verb': 'PONG', 'child_pid': state['hello']['pid'],
                               'child_exited': state['child'].poll() is not None,
                               'enrollment_pid': enrollment.ticket.identity.pid if enrollment else None,
                               'custody_fd': owner.fd, 'clients': clients}, uuid.uuid4().hex)
                elif verb == 'STOP':
                    stop.set()
                    peer.send({'verb': 'STOPPING'}, uuid.uuid4().hex)
                    return
                else:
                    peer.send({'verb': 'REFUSED'}, uuid.uuid4().hex)
        except (EOFError, OSError, ValueError):
            pass
        finally:
            with conns_lock:
                conns.discard(conn)
            try:
                peer.close()
            except OSError:
                pass

    listener = _listener(runtime)
    listener.settimeout(0.1)
    record = {'version': VERSION, 'ready': True, 'pid': os.getpid(), 'start': process_start(os.getpid()),
              'capability': config['capability'], 'generation': config['generation'],
              'digest': compatibility(config['provider'], config['home']), 'child_pid': hello['pid']}
    _publish_owner(runtime, record)
    gate.close()  # only our copy: collecting serve closes its copy after READY
    try:
        channel.send({'verb': 'READY'}, 'ready')
    except OSError:
        pass  # the collecting serve may exit during initial materialization; published shared custody survives
    finally:
        channel.close()
    threads = []
    try:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            with conns_lock:
                if len(conns) >= MAX_CLIENTS:
                    conn.close()
                    continue
                conns.add(conn)
            worker = threading.Thread(target=serve, args=(conn,), daemon=True, name='producer-client')
            worker.start()
            threads.append(worker)
    finally:
        stop.set()
        _unpublish_owner(runtime)  # READY is withdrawn before anything drains
        listener.close()
        _with_address(runtime, lambda address: os.path.lexists(address) and os.unlink(address))
        with conns_lock:
            for conn in list(conns):
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        shutdown_channel(state['data'])
        for worker in threads:
            worker.join()  # deliberately no timeout-triggered kill or custody release
        state['data'].close()
        state['child'].wait()
        owner.close()
