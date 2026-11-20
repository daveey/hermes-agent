"""Root-only drain baseline capture from an image-authored service manifest.

No discovery-based allowlist: every userspace PID must be a declared unit's
MainPID (or the package-pinned PID 1), independently of its executable name.
Invoke directly as a systemd oneshot, without an SSH/SSM shell on the host.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from pathlib import Path

from hermes_cli import worker_drain_census as census
from hermes_cli import worker_drain_fence as fence

MANIFEST_PATH = Path('/var/lib/hermes-worker/drain-manifest.json')
UNIT_RE = re.compile(r'[a-zA-Z0-9_.@:-]+\.(?:service|scope)\Z')


class CaptureRefused(RuntimeError):
    """Unknown provenance or live work: do not install a baseline."""


def file_digest(path) -> str:
    with open(path, 'rb') as fh:
        return hashlib.file_digest(fh, 'sha256').hexdigest()


def load_manifest(path) -> dict:
    try:
        doc = json.loads(fence._secure_read(Path(path), {0}))
        if doc.get('version') != 1 or not isinstance(doc['units'], list) or not doc['units']:
            raise ValueError('manifest shape')
        names = set()
        for unit in doc['units']:
            if (not UNIT_RE.fullmatch(unit['name']) or unit['name'] in names
                    or unit['kind'] not in ('infrastructure', 'hermes', 'init')
                    or any(not re.fullmatch('[0-9a-f]{64}', unit[k]) for k in ('fragment_sha256', 'exe_sha256'))):
                raise ValueError('unit shape')
            names.add(unit['name'])
        return doc
    except (OSError, ValueError, KeyError, TypeError, AttributeError, fence.DrainRefused) as exc:
        raise CaptureRefused('untrusted service manifest') from exc


def unit_state(unit: dict) -> dict:
    from tools.environments.local import build_subprocess_env
    cmd = ['systemctl']
    if 'user' in unit:
        if not re.fullmatch('[a-z_][a-z0-9_-]*', unit['user']):
            raise CaptureRefused('invalid user manager')
        cmd += ['--user', '--machine=' + unit['user'] + '@.host']
    cmd += ['show', '--', unit['name']]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', timeout=5,
                                check=True, env=build_subprocess_env(scrub_secrets=True))
    except (OSError, subprocess.SubprocessError) as exc:
        raise CaptureRefused('unit manager unavailable') from exc
    fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    return {k: fields[k] for k in ('MainPID', 'ControlGroup', 'FragmentPath') if k in fields}


def _unit_entry(unit, state, proc_root, ident, manifest):
    try:
        pid = '1' if unit['kind'] == 'init' and unit['name'] == 'init.scope' else state['MainPID']
        if not pid.isdigit() or int(pid) <= 0:
            raise ValueError('no live MainPID')
        if file_digest(state['FragmentPath']) != unit['fragment_sha256']:
            raise ValueError('unit package changed')
        entry = census.process_identity(proc_root, pid)
        if entry[3] != state['ControlGroup'] or not entry[3]:
            raise ValueError('unit cgroup mismatch')
        if file_digest(f'{proc_root}/{pid}/exe') != unit['exe_sha256']:
            raise ValueError('executable package changed')
        if unit['kind'] == 'hermes':
            from hermes_cli.worker_drain_capability import validate_capability
            validate_capability(int(pid), entry, ident, manifest, proc_root=proc_root)
        return dict(zip(('pid', 'start_ticks', 'uid', 'cgroup', 'exe'), entry, strict=True))
    except (OSError, ValueError, KeyError, IndexError) as exc:
        raise CaptureRefused(f'unit provenance unavailable: {unit["name"]}') from exc


def capture(manifest_path, ident, *, proc_root='/proc') -> dict:
    manifest = load_manifest(manifest_path)
    if ident is None:
        act = fence.load_activation()
        if act is None:
            raise CaptureRefused('fence not activated')
        ident = fence.current_identity(act)
    units = [(unit, unit_state(unit)) for unit in manifest['units']]
    idle = [_unit_entry(unit, state, proc_root, ident, manifest) for unit, state in units]
    base = census.Baseline(ident.boot, ident.iid, ident.node_epoch,
                           frozenset(census._entry_identity(e) for e in idle))
    observed = census.observe_processes(proc_root, base)
    if observed.state != census.ZERO:
        raise CaptureRefused('undeclared or unobservable processes: ' + observed.state)
    # A service restart or PID reuse while scanning cannot be published as idle.
    if any(unit_state(unit) != state for unit, state in units):
        raise CaptureRefused('service changed during capture')
    for entry in idle:
        if census.process_identity(proc_root, str(entry['pid'])) != census._entry_identity(entry):
            raise CaptureRefused('process changed during capture')
    return {'version': 2, 'boot_id': ident.boot, 'iid': ident.iid, 'node_epoch': ident.node_epoch,
            'manifest_digest': fence.request_hash(manifest), 'idle': idle}


def validate_baseline(base, ident, *, proc_root='/proc') -> bool:
    """Re-read provenance AND daemon capability on every census, not only capture."""
    try:
        manifest = load_manifest(MANIFEST_PATH)
        if fence.request_hash(manifest) != base.manifest_digest:
            return False
        entries = [_unit_entry(unit, unit_state(unit), proc_root, ident, manifest) for unit in manifest['units']]
        return frozenset(census._entry_identity(e) for e in entries) == base.idle
    except (OSError, ValueError, CaptureRefused):
        return False


def _trusted_parent(path: Path) -> int:
    """Pin every root-owned, non-mutable directory without following symlinks."""
    if not path.is_absolute() or '..' in path.parts:
        raise CaptureRefused('baseline parent must be an absolute trusted path')
    flags = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open('/', flags)
    try:
        for component in path.parent.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            st = os.fstat(fd)
            if st.st_uid != 0 or st.st_mode & 0o022:
                raise CaptureRefused('baseline parent is not root-owned and immutable')
        return fd
    except (OSError, CaptureRefused):
        os.close(fd)
        raise


def _write_at(dfd: int, name: str, doc: dict) -> None:
    tmp = f'.{name}.{uuid.uuid4().hex}'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=dfd)
    try:
        os.fchown(fd, 0, 0)
        with os.fdopen(fd, 'wb', closefd=False) as fh:
            fh.write(fence._canon(doc))
            fh.flush()
            os.fchmod(fd, 0o644)
            os.fsync(fd)
        os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        os.fsync(dfd)
    finally:
        os.close(fd)
        try:
            os.unlink(tmp, dir_fd=dfd)
        except FileNotFoundError:
            pass


def write_baseline(path: Path, doc: dict) -> None:
    if os.geteuid() != 0:  # windows-footgun: ok -- root-only Linux installer
        raise CaptureRefused('baseline writer requires root')
    path = Path(path)
    dfd = _trusted_parent(path)
    try:
        _write_at(dfd, path.name, doc)
    finally:
        os.close(dfd)


def install(manifest_path=MANIFEST_PATH, ident=None, *, path=Path(census.BASELINE_PATH)) -> dict:
    if os.geteuid() != 0:  # windows-footgun: ok -- root-only Linux installer
        raise CaptureRefused('baseline installation requires root')
    # Failed re-capture must invalidate a previous baseline, never retain an old
    # daemon attestation after an unsuccessful upgrade/activation.
    path = Path(path)
    dfd = _trusted_parent(path)
    try:
        try:
            os.unlink(path.name, dir_fd=dfd)
        except FileNotFoundError:
            pass
        os.fsync(dfd)
        doc = capture(manifest_path, ident)
        _write_at(dfd, path.name, doc)
        return doc
    finally:
        os.close(dfd)
