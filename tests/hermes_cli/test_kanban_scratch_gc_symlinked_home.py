"""Aged scratch is archived when the kanban home is reached through a symlink. [daveey]

daveey-mini (2026-11-13): ``~/.hermes`` is a symlink to ``.bb-hermes`` and every board
row spells its workspace through it. The archiver opened every path component
``O_NOFOLLOW`` from ``/``, so each aged tree was refused with ``... is not a plain
directory: NotADirectoryError(20, 'Not a directory')`` on every pass and done-card
scratch stayed on disk past retention. The operator's own link to the home is
resolved once; everything below the home is still opened without following links.
"""
import os
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_scratch_archive as sa
from hermes_cli import kanban_scratch_backoff as bo
from tests.hermes_cli import test_kanban_scratch_gc_archive as helpers
from tests.hermes_cli.test_kanban_scratch_gc_archive import _archive, _events, _gc, _plain_task, _task


@pytest.fixture
def linked_home(tmp_path, monkeypatch):
    """The mini's layout: the home is a relative symlink, and the process spells it through the link."""
    monkeypatch.setattr(psutil, "process_iter", lambda *args, **kwargs: iter(()))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    real = tmp_path / "real-hermes"
    real.mkdir()
    link = tmp_path / "hermes"
    link.symlink_to(real.name)
    monkeypatch.setenv("HERMES_HOME", str(link))
    for name in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_HOME"):
        monkeypatch.delenv(name, raising=False)
    assert kb.kanban_home() == link and kb.workspaces_root().is_relative_to(link)
    return link, real


@pytest.fixture
def remote(tmp_path):
    return helpers._seed_remote(tmp_path)


def _refusals():
    try:
        return bo.Refusals("default").entries
    except OSError:
        return {}


def test_non_git_symlink_tree_under_a_symlinked_home_is_archived_then_removed(linked_home):
    # The shape of three of the mini's trees: no repo, a venv's bin/python symlinks.
    tid, path = _plain_task(lambda p: ((p / "venv" / "bin").mkdir(parents=True),
                                       (p / "venv" / "bin" / "python").symlink_to("/usr/bin/python3")))
    link, real = linked_home
    assert path.is_relative_to(link) and not path.is_relative_to(real)  # spelled through the link, as on the mini

    assert _gc() == 0

    assert not path.exists()
    assert _refusals() == {}
    sa.verify_complete_archive(_archive(tid))
    kinds = [kind for kind, _payload in _events(tid)]
    assert "workspace_archived" in kinds


def test_git_clone_under_a_symlinked_home_is_archived_then_removed(linked_home, remote):
    tid, path = _task(remote=remote)

    assert _gc() == 0

    assert not path.exists()
    assert _refusals() == {}
    assert (_archive(tid) / "repo" / "meta.json").is_file()
    sa.verify_complete_archive(_archive(tid))


def test_workspaces_root_override_under_a_symlinked_parent_is_archived(linked_home, tmp_path, monkeypatch):
    real_store = tmp_path / "real-store"
    (real_store / "ws").mkdir(parents=True)
    (tmp_path / "store").symlink_to(real_store.name)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(tmp_path / "store" / "ws"))
    tid, path = _plain_task(lambda p: None)
    assert path.is_relative_to(tmp_path / "store")

    assert _gc() == 0

    assert not path.exists()
    sa.verify_complete_archive(_archive(tid))


def test_links_below_the_home_are_still_refused(linked_home, tmp_path):
    link, real = linked_home
    root = kb.workspaces_root()
    root.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    (outside / "d").mkdir(parents=True)
    (outside / "d" / "secret.txt").write_text("must not be read")

    # A workspace swapped for a link, and a directory reached through one.
    (root / "swapped").symlink_to(outside)
    for bad in (root / "swapped", root / "swapped" / "d"):
        with pytest.raises(sa.ArchiveRefused):
            sa._scan(bad)

    # A link back to the home from inside a workspace is no second way in: only the
    # outermost spelling of the home is resolved, the rest is opened without following.
    inside = root / "t_inside"
    inside.mkdir()
    (inside / "evil").symlink_to(link)
    with pytest.raises(sa.ArchiveRefused):
        sa._scan(inside / "evil" / "kanban" / "workspaces" / "t_inside")

    # The managed components themselves are not resolved either.
    relocated = tmp_path / "relocated-workspaces"
    (relocated / "t_moved").mkdir(parents=True)
    board_dir = real / "kanban" / "boards" / "other"
    board_dir.mkdir(parents=True)
    (board_dir / "workspaces").symlink_to(relocated)
    with pytest.raises(sa.ArchiveRefused):
        sa._scan(link / "kanban" / "boards" / "other" / "workspaces" / "t_moved")


def test_a_link_that_is_not_the_home_is_still_refused(linked_home, tmp_path):
    real_dir = tmp_path / "elsewhere"
    (real_dir / "d").mkdir(parents=True)
    (tmp_path / "alias").symlink_to(real_dir)
    for bad in (tmp_path / "alias", tmp_path / "alias" / "d"):
        with pytest.raises(sa.ArchiveRefused):
            sa._scan(bad)
    with pytest.raises(sa.ArchiveRefused):
        sa._scan(real_dir / "d" / ".." / "d")  # parent traversal stays refused, never collapsed


def test_canonical_resolves_only_the_home_prefix(linked_home, tmp_path):
    link, real = linked_home
    real = Path(os.path.realpath(real))
    tail = Path("kanban") / "workspaces" / "t_x" / "deep"
    assert sa._canonical(link / tail) == real / tail
    assert sa._canonical(real / tail) == real / tail
    assert sa._canonical(link) == real
    # Not below the home: returned as spelled, so the no-follow walk still refuses any link in it.
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "elsewhere")
    other = tmp_path / "alias" / "x"
    assert sa._canonical(other) == other
    assert sa._canonical(Path("relative") / "path") == Path("relative") / "path"


@pytest.mark.parametrize("operation", ["canonical", "scan"])
def test_home_retargeted_between_stat_and_resolve_is_not_accepted(
    linked_home, tmp_path, monkeypatch, operation,
):
    link, real = linked_home
    tail = Path("kanban") / "workspaces" / "tree"
    (real / tail).mkdir(parents=True)
    foreign = tmp_path / "foreign-home"
    (foreign / tail).mkdir(parents=True)
    (foreign / tail / "secret.txt").write_text("must not be read")
    spelled = link / tail
    realpath = os.path.realpath
    original = Path(realpath(real)) / tail
    foreign_root = Path(realpath(foreign)) / tail
    # Control: the stable operator-owned home link remains supported.
    assert sa._canonical(spelled) == original
    assert sa._scan(spelled).keys() == {"."}
    assert real.stat().st_ino != foreign.stat().st_ino
    retargeted = []

    def retarget_at_resolve(path, *args, **kwargs):
        # _canonical has already stat'ed this ancestor and matched its inode
        # to the trusted home. Change the real link, not either stat result.
        if Path(path) == link:
            assert not retargeted
            link.unlink()
            link.symlink_to(foreign.name)
            retargeted.append(path)
        return realpath(path, *args, **kwargs)

    # Keep the seam module-local: the test home-I/O guard also calls realpath
    # during stat, and must retain the real resolver and all its protections.
    archive_os = SimpleNamespace(**vars(os))
    archive_os.path = SimpleNamespace(**vars(os.path))
    archive_os.path.realpath = retarget_at_resolve
    with monkeypatch.context() as patch:
        patch.setattr(sa, "os", archive_os)
        if operation == "canonical":
            result = sa._canonical(spelled)
            assert result != foreign_root, "retargeted home must not accept a foreign root"
            assert result == spelled
        else:
            # The unchanged spelling still contains a symlink: the downstream
            # no-follow walk must refuse it rather than scan foreign content.
            with pytest.raises(sa.ArchiveRefused):
                sa._scan(spelled)
    assert retargeted == [link]
    assert link.resolve() == foreign_root.parents[2]
    assert (foreign / tail / "secret.txt").read_text() == "must not be read"


def test_scan_reads_the_same_tree_through_either_spelling(linked_home):
    link, real = linked_home
    tree = link / "kanban" / "workspaces" / "t_same"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "f.txt").write_text("x")
    through_link = sa._scan(tree)
    through_real = sa._scan(real / "kanban" / "workspaces" / "t_same")
    assert set(through_link) == {".", "sub", "sub/f.txt"} == set(through_real)
    assert through_link["sub/f.txt"].st_ino == through_real["sub/f.txt"].st_ino
