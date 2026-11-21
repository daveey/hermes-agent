"""Fail-closed exemption for gateways with no message ingress, not queue accounting.

A cold capability alone is insufficient: a second sealed descriptor is published
only after the running gateway has inspected its effective configs and adapter
maps. Any adapter construction permanently revokes both attestations beforehand.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from hermes_cli.worker_drain_baseline import CaptureRefused, file_digest
from hermes_cli.worker_drain_capability import revoke

NAME = 'hermes-worker-no-ingress-v1'
_inventory_fd: int | None = None
_inventory_revoked = False


def disable_ingress_exemption(reason: str) -> None:
    from hermes_cli import worker_drain_capability as cap
    with cap._daemon_lock:
        _disable_ingress_exemption(reason)


def _disable_ingress_exemption(reason: str) -> None:
    global _inventory_fd, _inventory_revoked
    _inventory_revoked = True
    # Revoke the cold attestation first, before releasing the inventory receipt.
    revoke(reason)
    if _inventory_fd is not None:
        os.close(_inventory_fd)
        _inventory_fd = None


def _config_files(homes: list[str]) -> dict:
    from ruamel.yaml import YAML
    from ruamel.yaml.error import YAMLError
    result = {}
    for home in homes:
        for name in ('config.yaml', 'gateway.json', '.env'):
            path = Path(home) / name
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                result[str(path)] = None
                continue
            if name == 'config.yaml':
                try:
                    doc = YAML(typ='safe').load(data)
                except YAMLError as exc:
                    raise ValueError('unreadable gateway config') from exc
                if not isinstance(doc, dict):
                    raise ValueError('unreadable gateway config')
            elif name == 'gateway.json' and not isinstance(json.loads(data), dict):
                raise ValueError('unreadable legacy gateway config')
            result[str(path)] = file_digest(path)
    return result


def _runner_empty(runner) -> bool:
    configs = [runner.config, *runner._profile_configs.values()]
    maps = [runner.adapters, *runner._profile_adapters.values()]
    return (all(isinstance(m, dict) and not m for m in maps)
            and all(isinstance(c.platforms, dict)
                    and all(p.enabled is False for p in c.platforms.values()) for c in configs))


def attest_runner(runner, homes: list[str]) -> bool:
    from hermes_cli import worker_drain_capability as cap
    with cap._daemon_lock:
        return _attest_runner(runner, homes)


def _attest_runner(runner, homes: list[str]) -> bool:
    global _inventory_fd
    from hermes_cli import worker_drain_capability as cap
    from hermes_cli.worker_drain_fence import _canon
    if _inventory_revoked or _inventory_fd is not None:
        return False
    try:
        if not _runner_empty(runner):
            raise ValueError('message ingress configured or live')
        doc = {'version': 1, 'pid': os.getpid(), 'homes': homes, 'adapters': [],
               'config_files': _config_files(homes)}
        fd = os.memfd_create(NAME, os.MFD_ALLOW_SEALING | os.MFD_CLOEXEC)
        try:
            os.write(fd, _canon(doc))
            cap.fcntl.fcntl(fd, cap._ADD_SEALS, cap._REQUIRED_SEALS)
        except (OSError, ValueError):
            os.close(fd)
            raise
        _inventory_fd = fd
        return True
    except (OSError, ValueError, AttributeError, TypeError):
        disable_ingress_exemption('gateway ingress inventory unavailable or nonempty')
        return False


def attest_activated_runner(runner) -> bool:
    from hermes_cli import worker_drain_capability as cap
    from hermes_cli.worker_drain_baseline import MANIFEST_PATH, load_manifest
    # No activation/cold receipt means no exemption to complete. Never mint late.
    if cap._daemon_fd is None:
        return False
    try:
        contract = cap.verified_contract(load_manifest(MANIFEST_PATH))
        return attest_runner(runner, contract['homes'])
    except (OSError, ValueError, CaptureRefused):
        disable_ingress_exemption('gateway ingress inventory contract unavailable')
        return False


def validate_ingress(pid: int, homes: list[str], *, proc_root='/proc') -> None:
    from hermes_cli import worker_drain_capability as cap
    try:
        expected = {'version': 1, 'pid': pid, 'homes': homes, 'adapters': [],
                    'config_files': _config_files(homes)}
        matches = []
        for path in (Path(proc_root) / str(pid) / 'fd').iterdir():
            try:
                if os.readlink(path) != f'/memfd:{NAME} (deleted)':
                    continue
                fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
                try:
                    if (cap.fcntl.fcntl(fd, cap._GET_SEALS) & cap._REQUIRED_SEALS == cap._REQUIRED_SEALS
                            and os.fstat(fd).st_uid == os.stat(Path(proc_root) / str(pid)).st_uid):
                        matches.append(json.loads(os.pread(fd, 1 << 20, 0)))
                finally:
                    os.close(fd)
            except FileNotFoundError:
                continue
        if matches != [expected]:
            raise ValueError('no live no-ingress inventory')
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        raise CaptureRefused('message ingress enabled or inventory unreadable') from exc
