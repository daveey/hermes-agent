"""Owner-pinned trusted observer load for a capsule-required managed worker. [daveey]

Only ``agent.observation_bind`` imports this, and only once an owner-installed observation channel exists
(ordinary cards never import it). The owner (``gateway.session_kanban_rendezvous``) wrote exactly one line
into the channel before it brokered the peer end to the dispatcher host:
``{"v":1,"op":"observer","root":<abs dir>,"digest":<sha256 hex>,"host_pid":<acceptor pid>}``, taken from
the acceptor registration it verified. Here:

1. the line's every byte must come from the owner (SCM_CREDENTIALS pid == the channel's creator pid, which
   ``observation_bind`` already proved is an ancestor); malformed/missing -> ``capsule_observer_missing``;
2. the root must be canonical, owner-uid and not group/world-writable (root dir and the three files), its
   ancestors safe (``ancestors_problem``), and outside the card workspace and the profile's Kanban
   workspace roots; the digest over the exact bytes read must equal the line's -> else
   ``capsule_observer_untrusted``;
3. the core hook sentinels must be present (``capsule_observer_hook_drift``);
4. then those exact bytes are executed as ``observer_wire``, ``observer_runtime``, ``observer_codex``
   (registered in ``sys.modules`` first, so their by-name imports resolve to the verified modules, never
   sys.path / cwd). ``sys.path`` is never touched.

Env, config and model output choose nothing here: the root and digest come only from the owner's line.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import re
import socket
import stat
import struct
import sys

MISSING = 'capsule_observer_missing'
UNTRUSTED = 'capsule_observer_untrusted'
DRIFT = 'capsule_observer_hook_drift'
LINE_KEYS = frozenset({'v', 'op', 'root', 'digest', 'host_pid'})
OBSERVER_FILES = ('observer_codex.py', 'observer_runtime.py', 'observer_wire.py')
LOAD_ORDER = ('observer_wire', 'observer_runtime', 'observer_codex')  # runtime and codex import wire by name
MAX_LINE = 64 * 1024
MAX_MODULE = 1024 * 1024
_HEX64 = re.compile(r'[0-9a-f]{64}')
_CRED = struct.calcsize('3i')
# Same hook sentinels the trusted oneshot entry (integration observer_entry.cli) requires, plus the managed
# worker's own seal. A core missing any of them could run observed traffic that never reaches the observer.
SENTINELS = (
    ('agent.chat_completion_helpers', '@_observe_sdk_request'),
    ('agent.codex_runtime', 'return open_observed_codex_stream(agent, active_client,'),
    ('agent.chat_completion_helpers', '@_unsupported_observed_summary'),
    ('agent.client_lifecycle', 'and not observation_active()'),
    ('agent.tool_executor', '@_observe_terminal_commit'),
    ('agent.turn_api_call', 'if observation_active():'),
    ('agent.session_persistence', 'observe_persisted(self, result)'),
    ('agent.tool_executor', 'observe_terminal(agent, function_name'),
    ('agent.managed_worker', 'observation_finish('),
)


def observer_digest(files) -> str:
    """sha256 over the sorted ``(name, bytes)`` pairs; integration ``observer_acceptor.files_digest`` twin.

    Each entry is ``name utf-8 + NUL + 8-byte big-endian length + bytes`` (pinned by a shared golden vector).
    """
    h = hashlib.sha256()
    for name in sorted(files):
        data = files[name]
        h.update(name.encode('utf-8') + b'\x00' + len(data).to_bytes(8, 'big') + data)
    return h.hexdigest()


def ancestors_problem(entries):
    """``entries`` = ``[(path, lstat), ...]`` from ``/`` down to the root's parent; None when safe.

    Each must be a directory owned by root or this uid. A group/world-writable one is refused unless it is
    root-owned and sticky (``/tmp``) or sits beneath an own directory nobody else may traverse (no g/o x).
    """
    me, shielded = os.getuid(), False  # windows-footgun: ok -- POSIX-only (unix socket peer credentials, owner-only modes)
    for path, st in entries:
        mode = stat.S_IMODE(st.st_mode)
        if not stat.S_ISDIR(st.st_mode):
            return f'{path}: not a directory'
        if st.st_uid not in (0, me):
            return f'{path}: foreign owner'
        if mode & 0o022 and not shielded and not (st.st_uid == 0 and mode & stat.S_ISVTX):
            return f'{path}: writable by others'
        if st.st_uid == me and not mode & 0o011:
            shielded = True
    return None


def _senders(ancillary):
    return [struct.unpack('3i', data[:_CRED])[0] for level, kind, data in ancillary
            if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS]


def read_line(sock, owner_pid: int, deadline_s: float) -> bytes:
    """One ``\\n``-terminated line, byte-wise (nothing after it is consumed), every byte sent by the owner."""
    sock.settimeout(deadline_s)
    buf = bytearray()
    try:
        while not buf.endswith(b'\n'):
            data, ancillary, flags, _ = sock.recvmsg(1, socket.CMSG_SPACE(_CRED))
            if not data or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
                raise RuntimeError(MISSING)
            if _senders(ancillary) != [owner_pid]:
                raise RuntimeError(UNTRUSTED)
            buf += data
            if len(buf) > MAX_LINE:
                raise RuntimeError(MISSING)
    except OSError as exc:  # timeout / reset: no owner line
        raise RuntimeError(MISSING) from exc
    finally:
        sock.settimeout(None)
    return bytes(buf)


def parse_line(raw: bytes) -> dict:
    try:
        line = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(MISSING) from exc
    if not (isinstance(line, dict) and set(line) == LINE_KEYS and type(line['v']) is int and line['v'] == 1
            and line['op'] == 'observer' and isinstance(line['root'], str) and line['root']
            and isinstance(line['digest'], str) and _HEX64.fullmatch(line['digest'])
            and type(line['host_pid']) is int and line['host_pid'] > 1):
        raise RuntimeError(MISSING)
    return line


def _inside(path: str, base: str) -> bool:
    return os.path.commonpath([path, base]) == base


def _forbidden_roots(context: dict, home: str):
    real_home = os.path.realpath(home)
    roots = [os.path.join(real_home, 'kanban', 'workspaces'), os.path.join(real_home, 'kanban', 'boards')]
    workspace = context.get('workspace')
    if isinstance(workspace, str) and os.path.isabs(workspace):
        roots.append(os.path.realpath(workspace))
    return roots


def _check_location(root: str, context: dict, home: str) -> None:
    if not os.path.isabs(root) or os.path.realpath(root) != root:
        raise RuntimeError(UNTRUSTED)
    if any(_inside(root, base) for base in _forbidden_roots(context, home)):
        raise RuntimeError(UNTRUSTED)
    entries, parts = [], root.split(os.sep)[1:-1]
    for depth in range(len(parts) + 1):
        path = os.sep + os.sep.join(parts[:depth])
        try:
            entries.append((path, os.lstat(path)))
        except OSError as exc:
            raise RuntimeError(UNTRUSTED) from exc
    if ancestors_problem(entries) is not None:
        raise RuntimeError(UNTRUSTED)


def _owned_private(st) -> bool:
    return st.st_uid == os.getuid() and not stat.S_IMODE(st.st_mode) & 0o022  # windows-footgun: ok -- POSIX-only (unix socket peer credentials, owner-only modes)


def _read_file(dfd: int, name: str) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not _owned_private(st) or st.st_size > MAX_MODULE:
            raise RuntimeError(UNTRUSTED)
        data = b''
        while chunk := os.read(fd, MAX_MODULE + 1 - len(data)):
            data += chunk
            if len(data) > MAX_MODULE:
                raise RuntimeError(UNTRUSTED)
        return data
    finally:
        os.close(fd)


def verified_files(root: str, digest: str, context: dict, home: str) -> dict:
    """The three observer files' exact bytes, only if the root is trusted and their digest matches."""
    _check_location(root, context, home)
    try:
        dfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise RuntimeError(UNTRUSTED) from exc
    try:
        if not _owned_private(os.fstat(dfd)):
            raise RuntimeError(UNTRUSTED)
        files = {name: _read_file(dfd, name) for name in OBSERVER_FILES}
    except OSError as exc:
        raise RuntimeError(UNTRUSTED) from exc
    finally:
        os.close(dfd)
    if not hmac.compare_digest(observer_digest(files), digest):
        raise RuntimeError(UNTRUSTED)
    return files


def _source(name: str) -> str:
    module = sys.modules.get(name)
    if module is not None:
        import inspect
        return inspect.getsource(module)
    spec = importlib.util.find_spec(name)
    if spec is None or not spec.origin:
        raise OSError(name)
    with open(spec.origin, encoding='utf-8') as stream:  # windows-footgun: ok -- POSIX-only capsule observer; source compared byte-exact
        return stream.read()


def check_hooks() -> None:
    """Refuse a core whose observation hooks drifted (read from the loaded core, never executed here)."""
    for name, sentinel in SENTINELS:
        try:
            present = sentinel in _source(name)
        except (OSError, TypeError, ImportError) as exc:
            raise RuntimeError(DRIFT) from exc
        if not present:
            raise RuntimeError(DRIFT)


def load_modules(root: str, files: dict):
    """Execute the verified bytes under their module names; returns the ``observer_runtime`` module."""
    if any(name in sys.modules for name in LOAD_ORDER):
        raise RuntimeError(UNTRUSTED)  # something else already claimed a trusted name
    loaded = []
    try:
        for name in LOAD_ORDER:
            path = os.path.join(root, name + '.py')
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None:
                raise ImportError(name)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            loaded.append(name)
            exec(compile(files[name + '.py'], path, 'exec', dont_inherit=True), module.__dict__)
    except Exception as exc:
        for name in loaded:
            sys.modules.pop(name, None)
        raise RuntimeError(UNTRUSTED) from exc
    runtime = sys.modules['observer_runtime']
    if not all(callable(getattr(runtime, attr, None)) for attr in ('install', 'active', 'finish_oneshot')):
        raise RuntimeError(UNTRUSTED)
    return runtime


def load_trusted(sock, owner_pid: int, context: dict, home: str, deadline_s: float):
    """Read the owner's observer line, verify, load. Returns ``(observer_runtime module, host_pid)``."""
    line = parse_line(read_line(sock, owner_pid, deadline_s))
    files = verified_files(line['root'], line['digest'], context, home)
    check_hooks()
    return load_modules(line['root'], files), line['host_pid']
