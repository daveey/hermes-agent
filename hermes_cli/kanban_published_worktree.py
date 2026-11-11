"""Narrow worktree-path exemption for clean wt/* heads published to origin.

Only routing text is filtered: workspace requirements and all other host markers
remain authoritative. No fetch, checkout, push, or local ref mutation is performed.
"""
from __future__ import annotations

import re
from contextvars import ContextVar
from functools import wraps
import subprocess
import time
from pathlib import Path

from tools.environments.local import build_subprocess_env

# Only whole roots, never subpaths (which may name ignored local artifacts).
_PATH = re.compile(r"(?:~|/home/[^/\s]+)/code/_wt(?:-[\w-]+)?/[\w-]+(?=$|[\s`'\",:;)]|\.(?=\s|$))")
_ORIGIN = re.compile(r'(?:https://github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?$')


_observation: ContextVar[dict | None] = ContextVar('published_worktrees', default=None)


def publication_scope(fn):
    """Share one bounded observation per synchronous create, including nested entrypoints."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if _observation.get() is not None:
            return fn(*args, **kwargs)
        token = _observation.set({'deadline': None, 'texts': {}})
        try:
            return fn(*args, **kwargs)
        finally:
            _observation.reset(token)
    return wrapped


def without_published_worktrees(text: str) -> str:
    """Remove only verified roots, within one shared three-second probe budget.

    Missing paths (including another host's paths), dirty trees, unpublished or
    detached heads and unavailable origins retain the conservative host marker.
    The card must already name the GitHub source URL, branch and full commit SHA
    so its receiving agent can reconstruct the checkout. Subpaths remain local:
    they may reference ignored files not contained in the published commit.
    Credential-scrubbed origins that cannot be read also remain local.
    Results are reused only inside one create's decision/audit observation.
    """
    observation = _observation.get()
    if observation is not None:
        if text in observation['texts']:
            return observation['texts'][text]
        if observation['deadline'] is None:
            observation['deadline'] = time.monotonic() + 3
        deadline = observation['deadline']
    else:
        deadline = time.monotonic() + 3
    checked: dict[str, bool] = {}

    def replace(match: re.Match) -> str:
        raw = match.group(0)
        if raw not in checked:
            checked[raw] = _published(Path(raw).expanduser(), deadline, text)
        return '<published-worktree>' if checked[raw] else raw

    result = _PATH.sub(replace, text)
    if observation is not None:
        observation['texts'][text] = result
    return result


def _published(path: Path, deadline: float, text: str) -> bool:
    if not (path / '.git').is_file() or (path / '.gitmodules').exists():
        return False
    env = build_subprocess_env(extra={
        'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0',
        'GIT_SSH_COMMAND': 'ssh -o BatchMode=yes -o ConnectTimeout=2',
        'SSH_ASKPASS_REQUIRE': 'never',
    })

    def git(*args: str) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired('git', 3)
        return subprocess.check_output(
            ['git', '-c', 'core.fsmonitor=false', '-C', str(path), *args], env=env, timeout=remaining,
            stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
        ).strip()

    try:
        branch = git('symbolic-ref', '--quiet', '--short', 'HEAD')
        if not branch.startswith('wt/'):
            return False
        head = git('rev-parse', 'HEAD')
        raw_origin = git('config', '--get', 'remote.origin.url')
        origin = _ORIGIN.fullmatch(raw_origin)
        if git('ls-remote', '--get-url', 'origin') != raw_origin:
            return False
        # The receiving agent must have a source and exact checkout identity,
        # not merely an origin-host pathname. Never infer these from the slug.
        if not origin or any(value not in text for value in (
                f'https://github.com/{origin[1]}', branch, head)):
            return False
        if git('status', '--porcelain', '--untracked-files=all', '--ignore-submodules=none'):
            return False
        ref = f'refs/heads/{branch}'
        if git('ls-remote', '--exit-code', 'origin', ref).split() != [head, ref]:
            return False
        # Revalidate local state after the network observation.
        return (git('symbolic-ref', '--quiet', '--short', 'HEAD') == branch
                and git('rev-parse', 'HEAD') == head
                and not git('status', '--porcelain', '--untracked-files=all', '--ignore-submodules=none'))
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return False
