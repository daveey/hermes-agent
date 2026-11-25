"""Bounded fresh disk metadata, including real worktree and packed-ref layouts."""
import time

import pytest

from hermes_cli import code_provenance_disk as disk


def test_head_loose_packed_detached_and_linked(tmp_path):
    common = tmp_path / 'common'
    common.mkdir()
    work = tmp_path / 'work'
    work.mkdir()
    git = tmp_path / 'git'
    git.mkdir()
    (work / '.git').write_text('gitdir: ../git')
    (git / 'commondir').write_text('../common')
    (git / 'HEAD').write_text('ref: refs/heads/topic')
    ref = common / 'refs/heads/topic'
    ref.parent.mkdir(parents=True)
    ref.write_text('a' * 40)
    assert disk.disk_identity(work, time.monotonic() + 2) == {'sha': 'a' * 40, 'error': None}
    ref.unlink()
    (common / 'packed-refs').write_text('# comment\n' + 'b' * 40 + ' refs/heads/other\n' +
                                      'c' * 40 + ' refs/heads/topic\n')
    assert disk.disk_identity(work, time.monotonic() + 2) == {'sha': 'c' * 40, 'error': None}
    (git / 'HEAD').write_text('d' * 40)
    assert disk.disk_identity(work, time.monotonic() + 2)['sha'] == 'd' * 40


@pytest.mark.parametrize('head', ['ref: ../outside', 'ref: /outside', 'ref: refs/../outside',
    'ref: refs\\outside', 'g' * 40, 'a' * 39, 'ref: refs/heads/absent'])
def test_malformed_and_missing_metadata_explicit_unavailable(tmp_path, head):
    git = tmp_path / '.git'
    git.mkdir()
    (git / 'HEAD').write_text(head)
    result = disk.disk_identity(tmp_path, time.monotonic() + 2)
    assert result['sha'] is None and result['error'] is not None


def test_no_git_and_bad_gitdir(tmp_path):
    assert disk.disk_identity(tmp_path, time.monotonic() + 2)['error'] == 'FileNotFoundError'
    (tmp_path / '.git').write_text('not a gitdir')
    assert disk.disk_identity(tmp_path, time.monotonic() + 2)['error'] == 'ValueError'


def test_bounds_head_line_and_packed_total(tmp_path, monkeypatch):
    git = tmp_path / '.git'
    git.mkdir()
    head = git / 'HEAD'
    head.write_text('a' * (disk.MAX_METADATA + 1))
    assert disk.disk_identity(tmp_path, time.monotonic() + 2)['error'] == 'ValueError'
    head.write_text('ref: refs/heads/topic')
    packed = git / 'packed-refs'
    packed.write_text('x' * (disk.MAX_METADATA + 1))
    assert disk.disk_identity(tmp_path, time.monotonic() + 2)['error'] == 'ValueError'
    packed.write_text('# comment\n' * 100)
    monkeypatch.setattr(disk, 'MAX_PACKED_REFS', 100)
    assert disk.disk_identity(tmp_path, time.monotonic() + 2)['error'] == 'ValueError'
    monkeypatch.setattr(disk, 'MAX_PACKED_REFS', 10000)
    assert disk.disk_identity(tmp_path, time.monotonic() + 2)['error'] == 'ValueError'
    assert disk.disk_identity(tmp_path, time.monotonic() - 1) == {'sha': None, 'error': 'TimeoutError'}
