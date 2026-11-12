"""Operator-only all-origin coordinator; never grants authority or edits lineage."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex
import subprocess
from types import SimpleNamespace

from hermes_cli.kanban_supervisor_requests import _non_worker_refusal
from hermes_cli.kanban_sync_client import HubError, SyncUnavailable
from tools.environments.local import build_subprocess_env


def require_operator():
    refusal = _non_worker_refusal()
    if refusal:
        raise PermissionError(refusal)


def load_hosts(args):
    require_operator()  # Fence before reading even the transport configuration.
    if bool(args.host_map) == bool(args.host_map_file):
        raise ValueError('exactly one of --host-map or --host-map-file required')
    hosts = json.loads(args.host_map or Path(args.host_map_file).read_text(encoding='utf-8-sig'))
    if not isinstance(hosts, dict) or not hosts:
        raise ValueError('host map must be a nonempty origin mapping')
    for origin, entry in hosts.items():
        if not isinstance(origin, str) or not origin or not isinstance(entry, dict):
            raise ValueError('invalid origin mapping')
        for key in ('host', 'task', 'backup'):
            if not isinstance(entry.get(key), str) or not entry[key] or '\x00' in entry[key]:
                raise ValueError(f'{origin}: missing {key}')
        if entry['host'].startswith('-') or not Path(entry['backup']).is_absolute():
            raise ValueError('SSH host must not be an option; backup must be absolute')
        argv = entry.get('argv')
        if not isinstance(argv, list) or not argv or any(not isinstance(s, str) or not s or '\x00' in s for s in argv):
            raise ValueError(f'{origin}: command argv required')
    return hosts


def ssh(entry, words, data=None):
    """One quoted remote argv, exact UTF-8 manifest bytes on stdin, no shell locally."""
    require_operator()  # Do not spawn SSH from worker/delegated lineage.
    result = subprocess.run(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20',
         '--', entry['host'], shlex.join(entry['argv'] + words)],
        input=None if data is None else data.decode('utf-8'),
        capture_output=True, text=True, encoding='utf-8',
        env=build_subprocess_env(), timeout=300, check=False,
    )
    if result.returncode:
        raise ValueError(f'SSH operation refused ({result.returncode}): {result.stderr.strip()}')
    receipt = json.loads(result.stdout)
    if not isinstance(receipt, dict):
        raise ValueError('invalid remote receipt')
    return receipt


def run(conn, args):
    """Return (exit status, full receipts), including guarded rollback failures."""
    require_operator()
    from hermes_cli import kanban_priority_cli as cli
    from hermes_cli import kanban_priority_executor as executor

    hosts = load_hosts(args)
    if args.writer not in hosts:
        raise ValueError('local writer missing from host map')
    if args.manifest or args.backup or args.task or args.attachment_id or args.digest:
        raise ValueError('migrate takes attachment task/backup paths only from host map')
    local = hosts[args.writer]
    prepared_args = SimpleNamespace(**vars(args))
    prepared_args.task, prepared_args.backup = local['task'], local['backup']
    prepared_args.priority_operation = 'prepare'
    prepared = cli._prepare_stage(conn, prepared_args)
    data = Path(local['backup']).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    origins = prepared['origins']
    report = {'manifest_sha256': digest, 'staged': {args.writer: prepared},
              'attempted': [], 'applied': {}, 'restored': {}, 'published': {}, 'errors': []}

    def invoke(origin, action):
        require_operator()
        stage = report['staged'][origin]
        if origin == args.writer:
            return executor.apply_attached(conn, stage['attachment_id'], digest, writer=origin,
                                           undo=action == 'restore',
                                           allow_legacy_rollback=args.allow_legacy_rollback)
        words = ['--priority-operation', action, '--writer', origin,
                 '--attachment-id', str(stage['attachment_id']), '--digest', digest]
        if action == 'restore' and args.allow_legacy_rollback:
            words.append('--allow-legacy-rollback')
        return ssh(hosts[origin], words)

    def checked(receipt, origin):
        if receipt.get('writer') != origin or receipt.get('manifest_sha256') != digest:
            raise ValueError(f'{origin}: receipt writer/digest mismatch')
        return receipt

    try:
        if set(hosts) != set(origins):
            raise ValueError('host map must exactly cover manifest origins')
        checked(prepared, args.writer)
        for origin in sorted(origins):
            if origin == args.writer:
                continue
            entry = hosts[origin]
            receipt = ssh(entry, ['--priority-operation', 'stage', '--writer', origin,
                                 '--task', entry['task'], '--backup', entry['backup'], '--manifest', '-'], data)
            report['staged'][origin] = receipt
            checked(receipt, origin)
        # Validate ALL staging receipts before the first priority write.
        for origin, receipt in report['staged'].items():
            checked(receipt, origin)
            if receipt.get('origins') != origins or type(receipt.get('attachment_id')) is not int or receipt['attachment_id'] <= 0:
                raise ValueError(f'{origin}: invalid staged inventory/attachment receipt')
        if set(report['staged']) != set(origins):
            raise ValueError('missing staged origin')
    except (ValueError, OSError, subprocess.SubprocessError, HubError, SyncUnavailable) as exc:
        report['errors'].append(str(exc))
        return 1, report

    def settle(*, undo):
        for origin in sorted(origins):
            receipt = (cli._publish(conn, args.writer) if origin == args.writer else
                ssh(hosts[origin], ['--priority-operation', 'publish', '--writer', origin]))
            if receipt.get('writer') != origin or receipt.get('pending') != 0 or receipt.get('rejected') != 0:
                raise ValueError(f'{origin}: invalid publication receipt')
            report['published'][origin] = receipt
        verify_args = SimpleNamespace(**vars(args))
        verify_args.manifest, verify_args.digest = local['backup'], digest
        verify_args.exception = []
        verify_args.allow_legacy_rollback = False
        report['canonical_restore' if undo else 'canonical'] = cli._verify(conn, verify_args, undo=undo)

    try:
        for origin in sorted(origins):
            # A lost reply can hide a committed write. Restore every attempted
            # origin on failure; the same CAS makes a non-applied origin a no-op.
            report['attempted'].append(origin)
            report['applied'][origin] = checked(invoke(origin, 'apply'), origin)
    except (ValueError, OSError, subprocess.SubprocessError, HubError, SyncUnavailable) as exc:
        report['errors'].append(f'apply failed; attempting guarded restore of every attempted origin: {exc}')
        for origin in reversed(report['attempted']):
            try:
                report['restored'][origin] = checked(invoke(origin, 'restore'), origin)
            except (ValueError, OSError, subprocess.SubprocessError, HubError, SyncUnavailable) as restore_exc:
                report['errors'].append(f'{origin}: guarded restore refused; manual inspection required: {restore_exc}')
        if set(report['restored']) == set(report['attempted']):
            try:
                settle(undo=True)
            except (ValueError, OSError, subprocess.SubprocessError, HubError, SyncUnavailable) as restore_exc:
                report['errors'].append(f'restore publication/verification failed: {restore_exc}')
        return 1, report

    try:
        settle(undo=False)
    except (ValueError, OSError, subprocess.SubprocessError, HubError, SyncUnavailable) as exc:
        report['errors'].append(f'publication/canonical verification failed: {exc}')
        return 1, report
    return 0, report
