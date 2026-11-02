"""Managed-worker side of the owner-brokered capsule observation channel. [daveey]

The owner (``gateway.session_kanban_rendezvous.observing_spawn``) launches a capsule-required card's
managed worker with ``--observation-fd <n>``: ``n`` is an inherited AF_UNIX socketpair end whose other end
the owner handed to the trusted dispatcher host over SCM_RIGHTS. ``main`` installs it from argv before the
hello frame (``install_from_argv``); nothing else (env, config, frames, model output) can install one.

``observation_bind(frame)`` runs in ``execute`` right after ``bind_worker_context`` (HERMES_KANBAN_* set) and
before checkpoint recovery, worker hooks, MCP discovery and AIAgent:

- no channel: a no-op for an ordinary card, ``capsule_observation_missing`` for a capsule-required one;
- channel: its peer must be this uid and an ancestor process (the owner created the pair); then exactly one
  ``start`` JSON line must arrive within ``START_DEADLINE_S`` naming this attempt's task, run and claim
  lock plus a workspace inside the card's workspace; then ``tools.capsule_workspace.bind_worker_workspace``
  pins it, with liveness = the channel still open. Anything else raises: fail closed, never skipped.

A ``bound`` line is written back on success. ``observation_revoke()`` tombstones the binding (execute's
``finally``). ``channel()`` exposes the socket to an in-process observer after the bind.

run22, trusted observer: right after the peer check and BEFORE ``start``, the owner's ``observer`` line
names a root + digest (from the acceptor registration the owner verified) and the acceptor ``host_pid``;
``agent.observation_load`` verifies and loads exactly those ``observer_*`` bytes (``capsule_observer_*``
refusals). ``start`` must then come from ``host_pid`` (SCM_CREDENTIALS, ``capsule_observation_start_sender``).
After the bind the observer is installed on a dup of the channel with the capsule the host Receiver
expects, and the binding's liveness becomes its authenticated ``workspace_check`` round trip.
``observation_finish(ok)`` seals (observer ``close``) or poisons once; ordinary cards never load any of it.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import threading
from typing import Any

FD_FLAG = '--observation-fd'
START_DEADLINE_S = 30.0
MAX_LINE = 64 * 1024
MISSING = 'capsule_observation_missing'
_LOCK = threading.Lock()
_STATE: dict[str, Any] = {'fd': None, 'sock': None, 'binding': None, 'runtime': None, 'observer': None,
                          'finished': False}


def _reset_for_tests():
    with _LOCK:
        if _STATE['sock'] is not None:
            _STATE['sock'].close()
        _STATE.update(fd=None, sock=None, binding=None, runtime=None, observer=None, finished=False)


def install_from_argv(argv) -> None:
    """``[]`` (no channel) or exactly ``['--observation-fd', '<n>']``; anything else is refused."""
    argv = list(argv)
    if not argv:
        return
    if len(argv) != 2 or argv[0] != FD_FLAG or not argv[1].isdigit() or int(argv[1]) < 3:
        raise ValueError('invalid_managed_worker_argv')
    with _LOCK:
        if _STATE['fd'] is not None:
            raise ValueError('invalid_managed_worker_argv')
        _STATE['fd'] = int(argv[1])


def installed_fd():
    return _STATE['fd']


def channel():
    """The bound observation socket (after ``observation_bind``), else None."""
    return _STATE['sock']


def _context(frame):
    raw = (frame.get('policy') or {}).get('kanban_json')
    if raw is None:
        return None
    context = json.loads(raw)
    if not isinstance(context, dict):
        raise RuntimeError(MISSING)
    return context


def _required(context) -> bool:
    # The owner re-derives from the board; here a frozen value can only add a requirement (an older policy
    # without the field was already re-derived and refused by the owner when it needed a capsule).
    frozen = context.get('capsule')
    return frozen is not None and frozen != {'required': False}


def _peer_pid(sock) -> int:
    return struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))[0]


def _peer_is_owner(sock) -> bool:
    pid, uid, _gid = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
    if uid != os.getuid() or pid <= 1 or pid == os.getpid():  # windows-footgun: ok -- POSIX-only (unix socket peer credentials, owner-only modes)
        return False
    import psutil
    try:
        return pid in {p.pid for p in psutil.Process().parents()}
    except psutil.Error:
        return False


def _read_start(sock, host_pid) -> dict:
    sock.settimeout(START_DEADLINE_S)
    buf = b''
    cred = struct.calcsize('3i')
    try:
        while b'\n' not in buf:
            chunk, ancillary, _flags, _ = sock.recvmsg(4096, socket.CMSG_SPACE(cred))
            if not chunk:
                raise RuntimeError('capsule_observation_start_missing')
            senders = [struct.unpack('3i', v[:cred])[0] for level, kind, v in ancillary
                       if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS]
            if senders != [host_pid]:  # a unix stream read never merges writers: one sender per chunk
                raise RuntimeError('capsule_observation_start_sender')
            buf += chunk
            if len(buf) > MAX_LINE:
                raise RuntimeError('capsule_observation_start_malformed')
    except TimeoutError as exc:
        raise RuntimeError('capsule_observation_start_missing') from exc
    except OSError as exc:
        raise RuntimeError('capsule_observation_start_missing') from exc
    finally:
        sock.settimeout(None)
    try:
        start = json.loads(buf.split(b'\n', 1)[0])
    except ValueError as exc:
        raise RuntimeError('capsule_observation_start_malformed') from exc
    if not isinstance(start, dict) or start.get('v') != 1 or start.get('op') != 'start':
        raise RuntimeError('capsule_observation_start_malformed')
    return start


def _check_start(start, context) -> str:
    for key in ('task_id', 'run_id', 'claim_lock'):
        if key not in start:
            raise RuntimeError('capsule_observation_start_malformed')
    if (start['task_id'], start['run_id'], start['claim_lock']) != (
            context.get('task_id'), context.get('run_id'), context.get('claim_lock')) or type(start['run_id']) is not int:
        raise RuntimeError('capsule_observation_start_mismatch')
    workspace, root = start.get('workspace'), context.get('workspace')
    if not (isinstance(workspace, str) and isinstance(root, str) and os.path.isabs(workspace)):
        raise RuntimeError('capsule_observation_workspace')
    real, base = os.path.realpath(workspace), os.path.realpath(root)
    if real != workspace or os.path.commonpath([real, base]) != base:
        raise RuntimeError('capsule_observation_workspace')
    return workspace


def _alive(sock):
    def is_live():
        observer = _STATE['observer']
        if observer is not None:  # after install: the authenticated host round trip, never a local flag
            return observer.workspace_check() is True
        try:
            data = sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
        except BlockingIOError:
            return True
        except OSError:
            return False
        return bool(data)
    return is_live


def observation_bind(frame):
    """Bind the capsule from the owner-brokered channel, or refuse; None when there is nothing to bind."""
    context = _context(frame)
    fd = _STATE['fd']
    if fd is None:
        if context is not None and _required(context):
            raise RuntimeError(MISSING)
        return None
    if context is None or (frame.get('policy') or {}).get('source') != 'kanban':
        raise RuntimeError(MISSING)
    sock = socket.socket(fileno=fd)
    with _LOCK:
        _STATE['fd'], _STATE['sock'] = -1, sock  # consumed: one bind per process
    if sock.family != socket.AF_UNIX or not _peer_is_owner(sock):
        raise RuntimeError('capsule_observation_peer')
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    from agent import observation_load
    runtime, host_pid = observation_load.load_trusted(sock, _peer_pid(sock), context, frame['home'],
                                                      START_DEADLINE_S)
    _STATE['runtime'] = runtime
    workspace = _check_start(_read_start(sock, host_pid), context)
    from tools import capsule_workspace
    home = os.path.realpath(frame['home'])
    binding = capsule_workspace.bind_worker_workspace(context['task_id'], str(context['run_id']), home,
                                                      workspace, _alive(sock))
    _STATE['binding'] = binding
    try:
        _install_observer(runtime, sock, context, host_pid, home, workspace, binding)
        sock.sendall(json.dumps({'v': 1, 'op': 'bound', 'task_id': context['task_id'],
                                 'run_id': context['run_id'], 'pid': os.getpid()}).encode() + b'\n')
    except BaseException:
        binding.close()
        raise
    return binding


def _install_observer(runtime, sock, context, host_pid, home, workspace, binding) -> None:
    """``observer_runtime.install`` on a dup of the channel; capsule = the host Receiver's WORKSPACE_CHECK."""
    fd = os.dup(sock.fileno())
    try:
        observer = runtime.install(fd, context['run_id'], host_pid)
    except BaseException:
        os.close(fd)
        raise
    observer.capsule = {'task_id': context['task_id'], 'run_id': str(context['run_id']), 'profile_home': home,
                        'workspace': workspace}
    observer.binding = binding
    _STATE['observer'] = observer


def observation_finish(ok):
    """Seal (``ok``: observer ``close`` with counts) or poison (unsealed) once; None without an observer.

    ``observer_runtime.finish_oneshot`` semantics: 0 when sealed, nonzero when poisoned or the seal
    failed. Idempotent; ordinary cards (no observer loaded) always get None.
    """
    with _LOCK:
        runtime = _STATE['runtime'] if _STATE['observer'] is not None and not _STATE['finished'] else None
        if runtime is not None:
            _STATE['finished'] = True
    if runtime is None:
        return None
    return runtime.finish_oneshot(0 if ok is True else 1)


def observation_revoke() -> None:
    """Tombstone this process's binding (idempotent); the channel stays for the observer until exit."""
    binding = _STATE['binding']
    if binding is not None:
        binding.close()
