"""Exec-only fixture broker/producer. Not a supported production launcher yet."""
from __future__ import annotations

import array
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import select
import socket
import sqlite3
import sys
import threading
import uuid


def authorized(request, config):
    token = request.get('capability')
    return (isinstance(token, str) and token.isascii() and hmac.compare_digest(token, config['capability'])
            and request.get('generation') == config['generation'])


def load_provider(config):
    from hermes_cli.serve_producer_protocol import ProtocolError

    spec = config['provider']
    path = Path(spec['path'])
    source = path.read_bytes()
    if len(source) > 1024 * 1024 or hashlib.sha256(source).hexdigest() != spec['sha256']:
        raise ProtocolError('provider bytes changed')
    name = '_hermes_scoped_producer_v1'
    module_spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[name] = module
    # Execute the bytes just authenticated, not a second path read or stale pyc.
    exec(compile(source, str(path), 'exec'), module.__dict__)
    return module


def producer(channel, config):
    from hermes_cli.serve_producer_protocol import ProtocolError, ResourceExhausted, encode_request

    module = load_provider(config)
    target = module.open_provider(config) if hasattr(module, 'open_provider') else module.produce
    db_path = Path(config['runtime']) / 'projection.sqlite3'
    fd = os.open(db_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    db = sqlite3.connect(db_path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA max_page_count=4096')
    db.execute('PRAGMA journal_size_limit=1048576')
    db.execute('CREATE TABLE IF NOT EXISTS projection (id INTEGER PRIMARY KEY CHECK(id=1), generation TEXT, wire BLOB)')
    channel.send({'verb': 'READY', 'pid': os.getpid()}, 'ready')
    try:
        while True:
            request = channel.receive()
            if request.get('verb') == 'STOP':
                return
            if not authorized(request, config):
                raise ProtocolError('producer authorization failed')
            key = request['key']
            try:
                result = target(request['payload'])
                wire = result if isinstance(result, bytes) else json.dumps(result, ensure_ascii=True, separators=(',', ':')).encode()
                if len(wire) > 512 * 1024:
                    raise ResourceExhausted('projection size')
                wal = Path(str(db_path) + '-wal')
                if wal.exists() and wal.stat().st_size > 32 * 1024 * 1024:
                    raise ResourceExhausted('pinned projection WAL')
                response = {'verb': 'PUBLISHED', 'key': key, 'generation': config['generation'],
                            'wire': wire.decode('utf-8')}
                frames = encode_request(uuid.uuid4().hex, response)
                with db:
                    db.execute('INSERT OR REPLACE INTO projection VALUES (1,?,?)', (config['generation'], wire))
                db.execute('PRAGMA wal_checkpoint(PASSIVE)')
            except (ValueError, RuntimeError, sqlite3.Error) as exc:
                response = {'verb': 'UNAVAILABLE', 'key': key, 'generation': config['generation'],
                            'error': type(exc).__name__}
                frames = encode_request(uuid.uuid4().hex, response)
            for frame in frames:
                channel.sock.sendall(frame)
    except (EOFError, BrokenPipeError, ConnectionResetError):
        return
    finally:
        db.close()
        closer = getattr(target, 'close', None)
        if closer is not None:
            closer()


def receive_control(channel):
    from hermes_cli.serve_producer_protocol import ProtocolError
    from hermes_cli.serve_producer_runtime import Channel

    message, ancillary, flags, _ = channel.sock.recvmsg(1, socket.CMSG_SPACE(array.array('i').itemsize))
    if message != b'C' or flags & socket.MSG_CTRUNC or len(ancillary) != 1:
        raise ProtocolError('missing inherited control channel')
    level, kind, data = ancillary[0]
    if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
        raise ProtocolError('invalid inherited control channel')
    fds = array.array('i')
    fds.frombytes(data)
    if len(fds) != 1:
        for fd in fds:
            os.close(fd)
        raise ProtocolError('control descriptor count')
    return Channel(socket.socket(fileno=fds[0]))


def broker(channel, config):
    from hermes_cli.serve_producer_custody import FamilyCustody
    from hermes_cli.serve_producer_protocol import ProtocolError
    from hermes_cli.serve_producer_runtime import spawn

    owner = FamilyCustody.acquire(Path(config['runtime']), 'panel')
    child, data = spawn('producer', config, custody_fd=owner.fd)
    try:
        hello = data.receive()
        channel.send({'verb': 'READY'}, 'ready')
        control = receive_control(channel)
    except (OSError, EOFError, ValueError):
        data.close()
        child.wait()
        owner.close()
        return
    stop = threading.Event()
    data_error = []

    def forward():
        nonlocal child, data, hello
        try:
            while not stop.is_set():
                request = channel.receive()
                if not authorized(request, config) or request.get('verb') != 'PRODUCE':
                    raise ProtocolError('broker authorization failed')
                for attempt in range(3):
                    try:
                        data.send(request, uuid.uuid4().hex)
                        response = data.receive()
                        break
                    except (EOFError, BrokenPipeError, ConnectionResetError):
                        # EOF is not death: actual waitpid must confirm exit
                        # before another child can inherit producer custody.
                        child.wait()
                        data.close()
                        if attempt == 2 or stop.wait(1 << attempt):
                            raise EOFError('producer restart budget')
                        child, data = spawn('producer', config, custody_fd=owner.fd)
                        hello = data.receive()
                        if stop.is_set():
                            raise EOFError('broker draining')
                channel.send(response, uuid.uuid4().hex)
        except (EOFError, OSError, ValueError) as exc:
            data_error.append(type(exc).__name__)
            stop.set()
        finally:
            data.sock.shutdown(socket.SHUT_RDWR)
            data.close()
            child.wait()

    worker = threading.Thread(target=forward, name='producer-forward', daemon=True)
    worker.start()
    sequence = 0
    try:
        while not stop.is_set():
            if not select.select([control.sock], [], [], 0.1)[0]:
                continue
            request = control.receive()
            sequence += 1
            if not authorized(request, config):
                control.send({'verb': 'REFUSED'}, str(sequence))
            elif request.get('verb') == 'STOP':
                break
            elif request.get('verb') == 'PING':
                control.send({'verb': 'PONG', 'child_pid': hello['pid'],
                              'child_exited': child.poll() is not None}, str(sequence))
            else:
                control.send({'verb': 'REFUSED'}, str(sequence))
    except (EOFError, OSError):
        pass
    finally:
        stop.set()
        # EOF requests child shutdown when it finishes its current call. An
        # unresponsive child keeps the inherited family fence, even if we exit.
        data.sock.shutdown(socket.SHUT_RDWR)
        channel.sock.shutdown(socket.SHUT_RDWR)
        channel.close()
        control.close()
        worker.join()  # deliberately no timeout-triggered kill or custody release
        owner.close()


def main():
    from hermes_cli.serve_producer_protocol import ProtocolError
    from hermes_cli.serve_producer_runtime import Channel

    role, descriptor = sys.argv[1:3]
    channel = Channel(socket.socket(fileno=int(descriptor)))
    config = channel.receive()
    if role == 'broker':
        broker(channel, config)
    elif role == 'producer':
        producer(channel, config)
    else:
        raise ProtocolError('unknown fixture role')


if __name__ == '__main__':
    root = sys.argv[3] if len(sys.argv) > 3 else ''
    if not os.path.isdir(os.path.join(root, 'hermes_cli')):
        raise SystemExit('producer root missing')
    sys.path.insert(0, root)
    main()
