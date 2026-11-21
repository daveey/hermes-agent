"""Owner side of the run-bound capsule observation rendezvous (Linux, unix socket, SCM_RIGHTS). [daveey]

The trusted dispatcher host (never this module) listens for one capsule-required attempt at
``<realpath(board db) dir>/capsule-rendezvous/<run_id>.sock``. The owner only brokers a socketpair end to
it for the exact managed-worker child it spawned for ``(db, task_id, run_id, claim_lock)``:

1. Path checks: the rendezvous dir is opened ``O_DIRECTORY|O_NOFOLLOW`` and must be a real 0700 dir owned
   by the owner uid; the run socket (looked up relative to that open dir) must be a socket owned by it.
   The connect goes through ``/proc/self/fd/<dirfd>/<name>``: no path swap, no sun_path length limit.
2. ``SO_PEERCRED`` uid of the listener must equal the owner uid.
2a. Listener identity: ``D/acceptor.json`` (regular 0600 owner-uid file, ≤4 KiB, read relative to the
   checked dir fd with ``O_NOFOLLOW``) must name this board's realpath db, a ``dispatch_owner`` equal to
   the board's persisted ``get_dispatch_owner``, and the listener's own pid + ``_process_fingerprint``
   (``capsule_acceptor_unregistered|owner_mismatch|pid_mismatch|stale``). The hello reply must echo that
   ``dispatch_owner``. Residual: same-uid host code can forge both; this stops accidental/stale/recycled
   squatting, not a deliberate same-uid attacker.
2b. (run22) The registration must also pin the trusted observer: ``observer_root`` (canonical absolute dir)
   and ``observer_digest`` (sha256 hex over exactly ``observer_{codex,runtime,wire}.py``); missing or
   malformed -> ``capsule_observer_untrusted`` before any hello. ``Rendezvous.prepare`` writes one
   ``{"v":1,"op":"observer","root","digest","host_pid":<registration pid>}`` line into the child's end
   before spawn; the child (``agent.observation_load``) re-verifies and loads exactly those bytes.
3. Hello (one JSON line each way): the listener must echo the EXACT identity or the owner refuses.
4. ``attach`` (launch) only: an ``O_EXCL`` ``<run_id>.consumed`` marker beside the socket makes the run
   single-use before anything is spawned. ``probe`` (admission) never consumes.
5. After spawn the owner sends the parent end over SCM_RIGHTS with the child's pid and /proc start
   fingerprint, waits for ``received``, then closes every owner copy. Any failure raises; the caller
   (``placing_spawn``) kills and reaps the child.

Nothing here reads env vars, config or caller params; the frozen ``kanban_json`` context only names
which socket to try, and the listener must authenticate that identity back.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import struct
import sys
from pathlib import Path

DIR_NAME = 'capsule-rendezvous'
FD_FLAG = '--observation-fd'
HELLO_TIMEOUT_S = 5.0
MAX_LINE = 64 * 1024
IDENTITY = ('db', 'task_id', 'run_id', 'claim_lock')
REGISTRATION = 'acceptor.json'
OBSERVER_KEYS = frozenset({'observer_root', 'observer_digest'})
REGISTRATION_KEYS = frozenset({'v', 'db', 'dispatch_owner', 'pid', 'start', *OBSERVER_KEYS})
MAX_REGISTRATION = 4096
REPLY_KEYS = frozenset({'v', 'ok', *IDENTITY, 'dispatch_owner'})
UNVERIFIED = 'unverified'  # hermes_cli.kanban_db_dispatch.UNVERIFIED_WORKER_FINGERPRINT
OBSERVER_UNTRUSTED = 'capsule_observer_untrusted'
_HEX64 = frozenset('0123456789abcdef')


class RendezvousError(RuntimeError):
    """The rendezvous is absent, unsafe, unauthenticated, mismatched, consumed or broke mid-handoff."""


def _owner_uid() -> int:
    return os.getuid()  # windows-footgun: ok -- POSIX-only (unix socket peer credentials, owner-only modes)


def _peer_cred(sock) -> tuple[int, int, int]:
    pid, uid, gid = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
    return pid, uid, gid


def _child_fingerprint(pid):
    from hermes_cli.kanban_db_dispatch import _process_fingerprint
    return _process_fingerprint(pid)


def _listener_fingerprint(pid):
    from hermes_cli.kanban_db_dispatch import _process_fingerprint
    return _process_fingerprint(pid)


def rendezvous_dir(db) -> Path:
    """``<board dir>/capsule-rendezvous`` for the board database ``db`` (canonical)."""
    return Path(os.path.realpath(db)).parent / DIR_NAME


def identity(context: dict) -> dict:
    """The exact attempt identity from the frozen context; RendezvousError when incomplete."""
    db, task_id, run_id, claim = (context.get(k) for k in IDENTITY)
    if not (isinstance(db, str) and db and isinstance(task_id, str) and task_id and type(run_id) is int
            and run_id > 0 and isinstance(claim, str) and claim):
        raise RendezvousError('capsule_rendezvous_identity_incomplete')
    try:
        real = str(Path(db).resolve(strict=True))
    except OSError as exc:
        raise RendezvousError('capsule_rendezvous_db_missing') from exc
    return {'db': real, 'task_id': task_id, 'run_id': run_id, 'claim_lock': claim}


def _open_dir(path: Path) -> int:
    try:
        dfd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise RendezvousError(f'capsule_rendezvous_dir_unsafe: {exc.strerror}') from exc
    st = os.fstat(dfd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != _owner_uid() or stat.S_IMODE(st.st_mode) != 0o700:
        os.close(dfd)
        raise RendezvousError('capsule_rendezvous_dir_unsafe')
    return dfd


def _read_line(sock) -> dict:
    buf = b''
    while b'\n' not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RendezvousError('capsule_rendezvous_closed')
        buf += chunk
        if len(buf) > MAX_LINE:
            raise RendezvousError('capsule_rendezvous_oversize')
    try:
        reply = json.loads(buf.split(b'\n', 1)[0])
    except ValueError as exc:
        raise RendezvousError('capsule_rendezvous_malformed') from exc
    if not isinstance(reply, dict) or reply.get('v') != 1:
        raise RendezvousError('capsule_rendezvous_malformed')
    return reply


def _send_line(sock, message: dict, fds=()) -> None:
    data = json.dumps(message, sort_keys=True).encode() + b'\n'
    if fds:
        sent = socket.send_fds(sock, [data], list(fds))
    else:
        sock.sendall(data)
        return
    if sent != len(data):
        raise RendezvousError('capsule_rendezvous_short_write')


class Rendezvous:
    """One authenticated, consumed attach for one attempt; brokers exactly one child FD."""

    def __init__(self, ident: dict, sock, registration=None):
        self.identity, self._sock, self._parent_end = ident, sock, None
        self._registration = registration

    def prepare(self, argv, kwargs):
        """Create the socketpair; the child's end rides ``pass_fds`` and the owner-only argv flag.

        Before anything is spawned (so before the peer end can reach the dispatcher host) the owner writes
        exactly one ``observer`` line into the child's end: the trusted observer root + digest and the
        listener pid from the registration it verified (``_read_registration``/``_check_listener``).
        """
        if self._sock is None or self._parent_end is not None:
            raise RendezvousError('capsule_rendezvous_reused')
        reg = self._registration
        if not isinstance(reg, dict) or not OBSERVER_KEYS <= set(reg):
            raise RendezvousError(OBSERVER_UNTRUSTED)
        self._parent_end, child_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            child_end.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            _send_line(self._parent_end, {'v': 1, 'op': 'observer', 'root': reg['observer_root'],
                                          'digest': reg['observer_digest'], 'host_pid': reg['pid']})
        except OSError as exc:
            child_end.close()
            raise RendezvousError(f'capsule_rendezvous_broken: {exc}') from exc
        fd = child_end.fileno()
        kwargs = dict(kwargs)
        kwargs['pass_fds'] = tuple(kwargs.get('pass_fds', ())) + (fd,)
        return child_end, [*argv, FD_FLAG, str(fd)], kwargs

    def deliver(self, process) -> None:
        """Send the parent end + exact child identity; the listener must acknowledge receipt."""
        if self._sock is None or self._parent_end is None:
            raise RendezvousError('capsule_rendezvous_not_prepared')
        fingerprint = _child_fingerprint(process.pid)
        if not fingerprint or process.poll() is not None:
            raise RendezvousError('capsule_rendezvous_child_unverified')
        message = {'v': 1, 'op': 'fd', 'task_id': self.identity['task_id'], 'run_id': self.identity['run_id'],
                   'claim_lock': self.identity['claim_lock'], 'child_pid': process.pid,
                   'child_start': fingerprint, 'owner_pid': os.getpid()}
        try:
            _send_line(self._sock, message, [self._parent_end.fileno()])
            self._parent_end.close()
            reply = _read_line(self._sock)
        except OSError as exc:
            raise RendezvousError(f'capsule_rendezvous_broken: {exc}') from exc
        if reply.get('ok') is not True or reply.get('received') is not True:
            raise RendezvousError('capsule_rendezvous_refused')
        self.close()

    def close(self) -> None:
        for end in (self._parent_end, self._sock):
            if end is not None:
                end.close()
        self._parent_end = self._sock = None


def _read_registration(dfd: int, ident: dict) -> dict:
    """``D/acceptor.json``: a regular 0600 owner-uid file (no symlink), ≤4 KiB, exact schema, this board's db.

    It binds the listener to the board's persisted dispatch owner and to one live process (pid + start
    fingerprint). Not a same-uid security boundary: deliberate same-uid host code can forge it; it closes
    accidental, stale and recycled-pid squatting of the rendezvous path.
    """
    try:
        st = os.lstat(REGISTRATION, dir_fd=dfd)
        if not stat.S_ISREG(st.st_mode):
            raise RendezvousError('capsule_acceptor_unregistered')
        fd = os.open(REGISTRATION, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
    except OSError as exc:
        raise RendezvousError('capsule_acceptor_unregistered') from exc
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != _owner_uid() or stat.S_IMODE(st.st_mode) != 0o600
                or st.st_size > MAX_REGISTRATION):
            raise RendezvousError('capsule_acceptor_unregistered')
        data = os.read(fd, MAX_REGISTRATION + 1)
    except OSError as exc:
        raise RendezvousError('capsule_acceptor_unregistered') from exc
    finally:
        os.close(fd)
    try:
        reg = json.loads(data) if len(data) <= MAX_REGISTRATION else None
    except ValueError:
        reg = None
    if (not isinstance(reg, dict) or not REGISTRATION_KEYS - OBSERVER_KEYS <= set(reg) <= REGISTRATION_KEYS
            or type(reg['v']) is not int or reg['v'] != 1
            or type(reg['pid']) is not int or reg['pid'] <= 0 or not isinstance(reg['start'], str)
            or not reg['start'] or not isinstance(reg['db'], str) or reg['db'] != ident['db']):
        raise RendezvousError('capsule_acceptor_unregistered')
    owner = reg['dispatch_owner']
    try:
        from hermes_cli.kanban_dispatch_owner import get_dispatch_owner
        board_owner = get_dispatch_owner(Path(ident['db']))
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise RendezvousError('capsule_acceptor_owner_mismatch') from exc
    if not isinstance(owner, str) or not owner or owner != board_owner:
        raise RendezvousError('capsule_acceptor_owner_mismatch')
    _check_observer_fields(reg)
    return reg


def _check_observer_fields(reg: dict) -> None:
    """Both run22 fields present: a canonical absolute existing dir + a lowercase sha256 hex digest.

    The owner only pins them (format + canonical path); the managed child re-verifies ownership, mode,
    location and recomputes the digest before it loads anything.
    """
    root, digest = reg.get('observer_root'), reg.get('observer_digest')
    if not (isinstance(root, str) and root and os.path.isabs(root) and os.path.realpath(root) == root
            and os.path.isdir(root) and isinstance(digest, str) and len(digest) == 64
            and set(digest) <= _HEX64):
        raise RendezvousError(OBSERVER_UNTRUSTED)


def _check_listener(pid: int, reg: dict) -> None:
    """The connected listener is the registered process: same pid, same live start fingerprint."""
    if pid != reg['pid']:
        raise RendezvousError('capsule_acceptor_pid_mismatch')
    start = _listener_fingerprint(pid)
    if not start or start == UNVERIFIED or start != reg['start']:
        raise RendezvousError('capsule_acceptor_stale')


def _connect(ident: dict, op: str):
    """Checked dir + socket + registration + peer uid/pid + exact hello echo; returns the connected socket."""
    return _connect_registered(ident, op)[0]


def _connect_registered(ident: dict, op: str):
    """``_connect`` plus the verified registration (the trusted observer pin comes from it)."""
    if not sys.platform.startswith('linux'):
        raise RendezvousError('capsule_rendezvous_unsupported_platform')
    name = f"{ident['run_id']}.sock"
    dfd = _open_dir(rendezvous_dir(ident['db']))
    try:
        _check_entries(dfd, ident, name)
        reg = _read_registration(dfd, ident)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(HELLO_TIMEOUT_S)
        try:
            sock.connect(f'/proc/self/fd/{dfd}/{name}')
            _authenticate(sock, ident, op, reg)
        except BaseException:
            sock.close()
            raise
    finally:
        os.close(dfd)
    return sock, reg


def _check_entries(dfd: int, ident: dict, name: str) -> None:
    if _consumed(dfd, ident):
        raise RendezvousError('capsule_rendezvous_consumed')
    try:
        st = os.lstat(name, dir_fd=dfd)
    except OSError as exc:
        raise RendezvousError('capsule_rendezvous_absent') from exc
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid != _owner_uid():
        raise RendezvousError('capsule_rendezvous_socket_unsafe')


def _consumed(dfd: int, ident: dict) -> bool:
    try:
        os.lstat(f"{ident['run_id']}.consumed", dir_fd=dfd)
    except FileNotFoundError:
        return False
    return True


def _authenticate(sock, ident: dict, op: str, reg: dict) -> None:
    try:
        pid, uid, _gid = _peer_cred(sock)
        if uid != _owner_uid():
            raise RendezvousError('capsule_rendezvous_peer_uid')
        _check_listener(pid, reg)
        _send_line(sock, {'v': 1, 'op': op, **ident, 'owner_pid': os.getpid()})
        reply = _read_line(sock)
    except OSError as exc:
        raise RendezvousError(f'capsule_rendezvous_broken: {exc}') from exc
    expected = {'v': 1, 'ok': True, **ident, 'dispatch_owner': reg['dispatch_owner']}
    if set(reply) != REPLY_KEYS or reply.get('ok') is not True or type(reply.get('run_id')) is not int \
            or {k: reply[k] for k in IDENTITY} != ident or reply != expected:
        raise RendezvousError('capsule_rendezvous_identity')


def _consume(ident: dict) -> None:
    """Durable single-use: ``O_EXCL`` marker in the checked dir, before anything is spawned."""
    dfd = _open_dir(rendezvous_dir(ident['db']))
    try:
        fd = os.open(f"{ident['run_id']}.consumed", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                     | os.O_CLOEXEC, 0o600, dir_fd=dfd)
    except FileExistsError as exc:
        raise RendezvousError('capsule_rendezvous_consumed') from exc
    except OSError as exc:
        raise RendezvousError(f'capsule_rendezvous_consume_failed: {exc.strerror}') from exc
    finally:
        os.close(dfd)
    try:
        os.write(fd, json.dumps({**ident, 'owner_pid': os.getpid()}, sort_keys=True).encode())
    finally:
        os.close(fd)


def check(context: dict) -> None:
    """Authenticated listener for exactly this attempt, not yet consumed; never consumes. Raises RendezvousError."""
    _connect(identity(context), 'probe').close()


def probe(context: dict) -> bool:
    """Admission-time readiness: authenticated listener for exactly this attempt, not yet consumed."""
    try:
        check(context)
    except RendezvousError:
        return False
    return True


def attach(context: dict) -> Rendezvous:
    """Launch-time: authenticate, then consume the run (single-use). Raises RendezvousError."""
    ident = identity(context)
    sock, reg = _connect_registered(ident, 'attach')
    try:
        _consume(ident)
    except BaseException:
        sock.close()
        raise
    sock.settimeout(HELLO_TIMEOUT_S)
    return Rendezvous(ident, sock, reg)


def observing_spawn(spawn, rendezvous, *, owner_sha=None):
    """``spawn`` that hands the child its end of a fresh socketpair (``pass_fds`` + argv flag).

    ``rendezvous`` None (ordinary card) returns ``spawn`` itself: the launch is byte-identical. The owner's
    copy of the child end is always closed after Popen; ``Rendezvous.deliver`` brokers the parent end.
    """
    if rendezvous is None:
        return spawn

    def run(argv, *args, **kwargs):
        child_end, argv, kwargs = rendezvous.prepare(argv, kwargs)
        if owner_sha is not None:
            from gateway.capsule_contract import WORKER_FLAG
            argv = [*argv, WORKER_FLAG, owner_sha]
        try:
            return spawn(argv, *args, **kwargs)
        except BaseException:
            rendezvous.close()
            raise
        finally:
            child_end.close()
    return run
