"""Startup-only, kernel-sealed proof of a daemon's activated fence runtime.

The image's root-authored contract pins reviewed code and supported homes. This
module does not infer fence coverage from a running process or package name.
The descriptor is close-on-exec and lives only as long as the attesting daemon.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import sys
import threading
from pathlib import Path

from hermes_cli import worker_drain_census as census
from hermes_cli import worker_drain_fence as fence
from hermes_cli.worker_drain_baseline import CaptureRefused, file_digest

fcntl = importlib.import_module('fcntl') if sys.platform.startswith('linux') else None
NAME = 'hermes-worker-drain-capability-v1'
CAPABILITIES = ('turn', 'claim', 'cron', 'plugin')
# Linux uapi/linux/fcntl.h. Some bundled Python builds omit these constants
# despite exposing fcntl and memfd_create; the kernel remains the authority.
_ADD_SEALS, _GET_SEALS, _REQUIRED_SEALS = 1033, 1034, 0x0F
REQUIRED_FILES = ('hermes_cli/worker_drain_fence.py', 'hermes_cli/worker_drain_capability.py',
                  'tui_gateway/turn_admission.py', 'cron/scheduler.py', 'tools/registry.py')


def verified_contract(manifest: dict) -> dict:
    try:
        contract = manifest['runtime']
        root = Path(contract['core_root'])
        files = contract['files']
        if (not root.is_absolute() or not set(REQUIRED_FILES).issubset(files)
                or set(contract['capabilities']) != set(CAPABILITIES)
                or any(type(contract[k]) is not int or contract[k] < 0
                       for k in ('installed_start_ticks', 'activation_start_ticks'))
                or not contract['homes'] or any(not Path(h).is_absolute() for h in contract['homes'])):
            raise ValueError('runtime contract shape')
        for name, digest in files.items():
            rel = Path(name)
            if (rel.is_absolute() or '..' in rel.parts or not re.fullmatch('[0-9a-f]{64}', digest)
                    or file_digest(root / rel) != digest):
                raise ValueError('installed runtime digest mismatch')
        digest = fence.request_hash({'files': files, 'core_root': str(root), 'capabilities': contract['capabilities']})
        return {**contract, 'digest': digest}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CaptureRefused('daemon capability runtime contract unavailable') from exc


def startup_is_cold() -> bool:
    # These two bootstrap observers do not execute application work. Unknown
    # threads or an already-created plugin manager make this a warm runtime.
    observers = {'gateway-startup-watchdog', 'hermes-exit-metrics'}
    if any(t is not threading.main_thread() and t.name not in observers for t in threading.enumerate()):
        return False
    plugins = sys.modules.get('hermes_cli.plugins')
    if plugins is not None and (getattr(plugins, '_plugin_manager', None) is not None
                               or getattr(plugins, '_plugin_managers_by_home', {})):
        return False
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return True
    return False


def _payload(ident, entry, contract) -> dict:
    if (contract['installed_boot_id'] != ident.boot
            or entry[1] <= max(contract['installed_start_ticks'], contract['activation_start_ticks'])):
        raise CaptureRefused('daemon capability predates install or activation')
    return {'version': 1, 'boot_id': ident.boot, 'iid': ident.iid, 'node_epoch': ident.node_epoch,
            'pid': entry[0], 'start_ticks': entry[1], 'uid': entry[2], 'cgroup': entry[3], 'exe': entry[4],
            'runtime_digest': contract['digest'], 'homes': contract['homes']}


def create_capability(ident, manifest, *, entry=None) -> int:
    if fcntl is None or not startup_is_cold():
        raise CaptureRefused('daemon capability requires cold single-threaded startup')
    entry = census.process_identity('/proc', str(os.getpid())) if entry is None else entry
    if entry[0] != os.getpid():
        raise CaptureRefused('cannot attest another process')
    contract = verified_contract(manifest)
    from hermes_cli._startup_fast import project_root_str
    if Path(contract['core_root']).resolve() != Path(project_root_str()).resolve():
        raise CaptureRefused('running core is not the reviewed installed runtime')
    payload = _payload(ident, entry, contract)
    fd = os.memfd_create(NAME, os.MFD_ALLOW_SEALING | os.MFD_CLOEXEC)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as fh:
            fh.write(fence._canon(payload))
            fh.flush()
        fcntl.fcntl(fd, _ADD_SEALS, _REQUIRED_SEALS)
        return fd
    except (OSError, ValueError):
        os.close(fd)
        raise


_daemon_fd: int | None = None
_revoked = False
_daemon_lock = threading.RLock()


def revoke(reason: str) -> None:
    """One-way loss of exemption, before any unsupported code can execute."""
    global _daemon_fd, _revoked
    with _daemon_lock:
        _revoked = True
        if _daemon_fd is not None:
            os.close(_daemon_fd)
            _daemon_fd = None


def start_daemon_capability() -> bool:
    """Only the cold entry calls this; failure disables exemption, not startup."""
    global _daemon_fd
    from hermes_cli.worker_drain_baseline import MANIFEST_PATH, load_manifest
    with _daemon_lock:
        if _revoked:
            return False
        if _daemon_fd is not None:
            return True
        try:
            act = fence.load_activation()
            if act is None:
                return False
            _daemon_fd = create_capability(fence.current_identity(act), load_manifest(MANIFEST_PATH))
            return True
        except (OSError, ValueError, CaptureRefused, fence.DrainRefused):
            revoke('capability startup refused')
            return False


def validate_capability(pid, entry, ident, manifest, *, proc_root='/proc') -> None:
    if fcntl is None:
        raise CaptureRefused('daemon capability requires Linux')
    try:
        expected = _payload(ident, entry, verified_contract(manifest))
        directory = Path(proc_root) / str(pid) / 'fd'
        matches = []
        for path in directory.iterdir():
            try:
                if os.readlink(path) != f'/memfd:{NAME} (deleted)':
                    continue
                # /proc/PID/fd is deliberately followed here: the descriptor is
                # checked through the kernel, not trusted as a regular JSON file.
                fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
                try:
                    if fcntl.fcntl(fd, _GET_SEALS) & _REQUIRED_SEALS != _REQUIRED_SEALS:
                        continue
                    if os.fstat(fd).st_uid != entry[2]:
                        continue
                    matches.append(json.loads(os.pread(fd, 1 << 20, 0)))
                finally:
                    os.close(fd)
            except FileNotFoundError:
                continue
        if matches != [expected]:
            raise CaptureRefused('daemon capability missing or mismatched')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CaptureRefused('daemon capability unreadable') from exc
