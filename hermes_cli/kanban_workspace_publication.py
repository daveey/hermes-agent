"""Require publication or explicit host custody before a Git-workspace handoff.

Never stage, commit or push arbitrary worker files. Observation is outside the
transition transaction; the final local-state fence is inside it. A receipt is
not a portability grant: independent workspace/placement guards still apply.
"""
from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
from pathlib import Path
import re
import json
import subprocess
import time

_receipt: ContextVar[tuple | None] = ContextVar('workspace_publication', default=None)
_kernel_park: ContextVar[str | None] = ContextVar('workspace_kernel_park', default=None)
_deadline: ContextVar[float | None] = ContextVar('workspace_publication_deadline', default=None)
_REASON = re.compile(r'^UNPUBLISHED_WORK_REASON=([^\r\n]+)', re.MULTILINE)


def _git(path, deadline, *args):
    from tools.environments.local import build_subprocess_env

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired('git', 5)
    return subprocess.check_output(
        ['git', '-c', 'core.fsmonitor=false', '-c', 'protocol.ext.allow=never', '-C', str(path), *args],
        env=build_subprocess_env(extra={'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0',
                                      'GIT_NO_LAZY_FETCH': '1', 'SSH_ASKPASS_REQUIRE': 'never',
                                      'GIT_SSH_COMMAND': 'ssh -o BatchMode=yes -o ConnectTimeout=2'}),
        stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=remaining,
        text=True, encoding='utf-8').strip()


def _local(path, deadline):
    return {'branch': _git(path, deadline, 'symbolic-ref', '--quiet', '--short', 'HEAD'),
            'head': _git(path, deadline, 'rev-parse', 'HEAD'),
            'dirty': bool(_git(path, deadline, 'status', '--porcelain', '--untracked-files=all',
                               '--ignore-submodules=none'))}


def _workspace(conn, task_id):
    row = conn.execute('SELECT workspace_path FROM tasks WHERE id = ?', (task_id,)).fetchone()
    if not row or not row[0]:
        return None
    path = Path(row[0]).expanduser()
    if not any((p / '.git').exists() for p in (path, *path.parents)):
        return None
    return path


def _paths(conn, task_id, text=''):
    """Declared workspace plus explicit Git roots retained in current/prior checkpoints.

    Scratch is a container, not evidence that its separately named implementation
    worktree is disposable. Do not recursively scan its artifacts or read files.
    """
    found = {}
    declared = _workspace(conn, task_id)
    if declared:
        found[str(declared)] = declared
    retained = conn.execute('SELECT summary, metadata FROM task_runs WHERE task_id=? '
                            'ORDER BY id DESC LIMIT 3', (task_id,)).fetchall()
    texts = []
    latest = {}
    for summary, metadata in retained:
        texts.append(summary or '')
        try:
            data = json.loads(metadata or '{}')
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            texts.extend(str(data.get(key) or '') for key in ('checkpoint', 'worktree', 'worktrees'))
            # Carry server-observed custody forward even after checkpoint prose
            # rolls out of the bounded history window or the worktree disappears.
            receipts = data.get('workspace_publications')
            if isinstance(receipts, list):
                for receipt in receipts:
                    raw = receipt.get('path') if isinstance(receipt, dict) else None
                    if isinstance(raw, str) and Path(raw).is_absolute():
                        latest.setdefault(raw, receipt.get('state'))
    found.update((raw, Path(raw)) for raw, state in latest.items() if state != 'published')
    for value, historical in [(value, True) for value in texts] + [(text, False)]:
        for raw in re.findall(r"(?:/|~/)[^\s`'\"<>;,()\[\]]+", value):
            path = Path(raw.rstrip('.')).expanduser()
            if historical and latest.get(str(path)) == 'published':
                continue
            if (path / '.git').is_file():
                found[str(path)] = path
    return list(found.values())


def _observe(path):
    deadline = _deadline.get() or time.monotonic() + 5
    receipt = {'path': str(path), 'state': 'unverified', 'reason': 'git_probe_failed'}
    try:
        local = _local(path, deadline)
        receipt.update(local)
        if local['dirty']:
            receipt['reason'] = 'uncommitted_or_untracked_changes'
            return receipt
        ref = 'refs/heads/' + local['branch']
        remote = _git(path, deadline, 'ls-remote', '--exit-code', 'origin', ref).split()
        if remote != [local['head'], ref]:
            receipt['reason'] = 'origin_branch_differs'
            return receipt
        if _local(path, deadline) != local:
            receipt['reason'] = 'workspace_changed_during_probe'
            return receipt
        receipt.update(state='published', reason='exact_origin_branch')
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        pass  # Unknown publication must never become a successful publication receipt.
    return receipt


def involuntary_park(task_id, fn, *args, **kwargs):
    """Kernel-only call scope; never inferred from worker metadata or reason text."""
    token = _kernel_park.set(task_id)
    try:
        return fn(*args, **kwargs)
    finally:
        _kernel_park.reset(token)


def publication_guard(fn):
    """Observe before any transition transaction; exceptions leave run ownership intact."""
    @wraps(fn)
    def wrapped(conn, task_id, *args, **kwargs):
        if _kernel_park.get() == task_id:
            return fn(conn, task_id, *args, **kwargs)
        row = conn.execute('SELECT status, current_run_id FROM tasks WHERE id = ?', (task_id,)).fetchone()
        text = kwargs.get('checkpoint') or kwargs.get('reason') or kwargs.get('summary') or ''
        metadata = kwargs.get('metadata') or {}
        references = '\n'.join(str(metadata.get(key) or '') for key in ('worktree', 'worktrees'))
        paths = _paths(conn, task_id, text + '\n' + references) if row and row[0] == 'running' and row[1] else []
        if not paths:
            return fn(conn, task_id, *args, **kwargs)
        # Nested lifecycle calls reuse only this exact run's preflight.
        current = _receipt.get()
        if current and current[:3] == (id(conn), task_id, row[1]):
            return fn(conn, task_id, *args, **kwargs)
        budget = _deadline.set(time.monotonic() + 5)
        try:
            observations = [({'path': str(path), 'state': 'unverified', 'reason': 'already_in_transaction'}
                             if conn.in_transaction else _observe(path)) for path in paths]
        finally:
            _deadline.reset(budget)
        reason = _REASON.search(text)
        for observed in observations:
            if observed['state'] != 'published':
                if not reason or not reason[1].strip():
                    from hermes_cli.kanban_source_preflight import refusal_hint
                    raise ValueError('unpublished_workspace: commit and push the task branch before parking, or add '
                                     'UNPUBLISHED_WORK_REASON=<why work must remain on this host> to the checkpoint/reason'
                                     + refusal_hint(task_id, row[1]))
                observed['retention_reason'] = reason[1].strip()[:1000]
        token = _receipt.set((id(conn), task_id, row[1], observations))
        try:
            return fn(conn, task_id, *args, **kwargs)
        finally:
            _receipt.reset(token)
    from hermes_cli.kanban_source_preflight import source_receipt_preflight as preflight
    return preflight(wrapped)  # workspace_source: stacked outside the guard (v3 §2)


def source_receipt_preflight(fn):
    """The workspace_source ending preflight alone (``complete_task``/``request_changes``, which PR1133 does not
    guard): see :mod:`hermes_cli.kanban_source_preflight`."""
    from hermes_cli.kanban_source_preflight import source_receipt_preflight as preflight
    return preflight(fn)


def closed_metadata(conn, task_id, run_id, metadata):
    """Server-owned receipt; involuntary exits cannot be refused by network failure."""
    metadata = {key: value for key, value in (metadata or {}).items()
                if key not in ('workspace_publication', 'workspace_publications')}
    current = _receipt.get()
    paths = _paths(conn, task_id)
    observations = [{'path': str(path), 'state': 'unverified', 'reason': 'involuntary_or_unguarded_exit'}
                    for path in paths]
    if _kernel_park.get() != task_id and current and current[:3] == (id(conn), task_id, run_id):
        observations = [dict(item) for item in current[3]]
        deadline = time.monotonic() + 1
        for observed in observations:
            if observed['state'] == 'published':
                try:
                    local = _local(Path(observed['path']), deadline)
                except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
                    raise ValueError('unpublished_workspace: final local-state fence failed') from exc
                if any(local[key] != observed[key] for key in local):
                    raise ValueError('unpublished_workspace: workspace changed before handoff')
    if not observations:
        return metadata
    return {**(metadata or {}), 'workspace_publication': observations[0],
            'workspace_publications': observations}
