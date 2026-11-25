"""Fresh, bounded Git HEAD metadata only; not a working-tree integrity check."""
from pathlib import Path, PurePosixPath
import re
import time

MAX_METADATA = 4096
MAX_PACKED_REFS = 1024 * 1024


def _check(deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError('disk_identity_deadline')


def _read(path, deadline):
    _check(deadline)
    with path.open('rb') as stream:
        data = stream.read(MAX_METADATA + 1)
    _check(deadline)
    if len(data) > MAX_METADATA:
        raise ValueError('git_metadata_too_large')
    return data.decode('utf-8-sig').strip()


def _head(root, deadline):
    git_dir = root / '.git'
    if git_dir.is_file():
        marker = _read(git_dir, deadline)
        if not marker.startswith('gitdir: '):
            raise ValueError('git_dir_unavailable')
        git_dir = root / marker[len('gitdir: '):]
    common = git_dir
    if (git_dir / 'commondir').exists():
        common = git_dir / _read(git_dir / 'commondir', deadline)
    head = _read(git_dir / 'HEAD', deadline)
    if not head.startswith('ref: '):
        return head
    ref = head[len('ref: '):]
    path = PurePosixPath(ref)
    if not ref.startswith('refs/') or path.is_absolute() or '..' in path.parts or '\\' in ref:
        raise ValueError('git_ref_invalid')
    for directory in (git_dir, common):
        if (directory / ref).is_file():
            return _read(directory / ref, deadline)
    total = 0
    with (common / 'packed-refs').open('rb') as stream:
        while True:
            _check(deadline)
            line = stream.readline(MAX_METADATA + 1)
            if not line:
                break
            total += len(line)
            if len(line) > MAX_METADATA or total > MAX_PACKED_REFS:
                raise ValueError('git_metadata_too_large')
            parts = line.decode('ascii').strip().split(' ', 1)
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    raise ValueError('git_ref_unavailable')


def disk_identity(root: Path, deadline: float) -> dict:
    """One blocking syscall is not cancellable; caller must retain single-flight custody."""
    try:
        sha = _head(root, deadline)
        _check(deadline)
        if re.fullmatch('[0-9a-f]{40}', sha) is None:
            raise ValueError('git_sha_invalid')
        return {'sha': sha, 'error': None}
    except (OSError, ValueError) as exc:
        # Disk paths and arbitrary exception messages never reach the response.
        return {'sha': None, 'error': type(exc).__name__}
