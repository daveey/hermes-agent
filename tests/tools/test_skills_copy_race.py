"""The sanitized skills copy never follows a swap made between enumeration and open.

A sandbox that can write a skills source (a trusted project's ``.hermes/skills`` under a
cwd mounted read-write at ``/workspace``, or an rw ``docker_volumes`` entry) could swap a
vetted file, or a vetted file's parent directory, for a symlink to any host path after the
walk checked it and before the copy opened it. The path-based copy then followed the link
and wrote the host file (``HERMES_HOME/.env``, say) into the container's own mount. Every
docker refresh tick re-runs the copy, so the race could be retried without limit.

Each test swaps deterministically from inside the walk's deny predicate, i.e. after the
entry was enumerated and vetted and before it is opened, and asserts no secret byte ever
reaches the copy.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import pytest

import tools.credential_files as cf

SECRET = "TOP-SECRET-HOST-TOKEN"

def posix_only(test):
    """Runs on the Linux suite and the macOS ``-m platforms`` lane (real O_NOFOLLOW semantics)."""
    return pytest.mark.platforms("posix")(
        pytest.mark.skipif(not cf._ANCHORED_COPY_SUPPORTED, reason="needs O_NOFOLLOW + dir_fd opens")(test))


@pytest.mark.platforms("posix")
def test_posix_hosts_have_anchored_copy():
    """Linux and macOS must take the race-free path; losing it would silently empty every mount."""
    assert cf._ANCHORED_COPY_SUPPORTED


@pytest.mark.platforms("windows")
def test_windows_withholds_skills_copy_for_real(tmp_path, monkeypatch):
    home = tmp_path / "hh"
    (home / "skills" / "s").mkdir(parents=True)
    (home / "skills" / "s" / "SKILL.md").write_text("# s")
    monkeypatch.setenv("HERMES_HOME", str(home))

    copy = Path(cf._safe_skills_path(home / "skills"))

    assert not cf._ANCHORED_COPY_SUPPORTED
    assert not any(p.is_file() for p in copy.rglob("*"))


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "hh"
    (home / "skills" / "s").mkdir(parents=True)
    (home / "skills" / "s" / "SKILL.md").write_text("---\nname: s\n---\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def secret(tmp_path) -> Path:
    path = tmp_path / "host_secret.txt"
    path.write_text(SECRET + "\n")
    return path


def _swap_when_vetted(monkeypatch, name: str, swap) -> list[Path]:
    """Run *swap(path)* once, the first time the walk vets an entry called *name*."""
    fired: list[Path] = []
    real_denier = cf._skill_entry_denier

    def denier(root):
        denied = real_denier(root)
        if denied is None:
            return None

        def check(path):
            result = denied(path)
            if path.name == name and not fired:
                fired.append(path)
                swap(path)
            return result

        return check

    monkeypatch.setattr(cf, "_skill_entry_denier", denier)
    return fired


def _copied_text(copy: Path) -> str:
    return "".join(p.read_text(errors="replace") for p in copy.rglob("*") if p.is_file() and not p.is_symlink())


@posix_only
def test_file_swapped_for_symlink_after_vetting_is_not_followed(home, secret, monkeypatch):
    victim = home / "skills" / "s" / "zz_payload.md"
    victim.write_text("benign")

    def swap(path):
        path.unlink()
        path.symlink_to(secret)

    fired = _swap_when_vetted(monkeypatch, "zz_payload.md", swap)
    copy = Path(cf._safe_skills_path(home / "skills"))

    assert fired, "the swap hook never ran: the test did not exercise the race"
    assert SECRET not in _copied_text(copy)
    assert (copy / "s" / "SKILL.md").is_file(), "the rest of the tree must still be copied"


@posix_only
def test_parent_dir_swapped_for_symlink_after_file_vetting_is_not_followed(home, tmp_path, monkeypatch):
    """The file is fine; its *parent directory* is swapped for a link to a host dir holding a
    same-named secret between vetting the file and opening it."""
    sub = home / "skills" / "s" / "sub"
    sub.mkdir()
    (sub / "zz.md").write_text("benign")
    host_dir = tmp_path / "host_dir"
    host_dir.mkdir()
    (host_dir / "zz.md").write_text(SECRET)

    def swap(path):
        parent = path.parent
        parent.rename(tmp_path / "moved_away")
        parent.symlink_to(host_dir, target_is_directory=True)

    fired = _swap_when_vetted(monkeypatch, "zz.md", swap)
    copy = Path(cf._safe_skills_path(home / "skills"))

    assert fired
    assert SECRET not in _copied_text(copy)


@posix_only
def test_dir_swapped_for_symlink_before_descent_is_not_walked(home, tmp_path, monkeypatch):
    sub = home / "skills" / "s" / "sub"
    sub.mkdir()
    (sub / "a.md").write_text("benign")
    host_dir = tmp_path / "host_dir"
    host_dir.mkdir()
    (host_dir / "a.md").write_text(SECRET)

    def swap(path):
        path.rename(tmp_path / "moved_away")
        path.symlink_to(host_dir, target_is_directory=True)

    fired = _swap_when_vetted(monkeypatch, "sub", swap)
    copy = Path(cf._safe_skills_path(home / "skills"))

    assert fired
    assert SECRET not in _copied_text(copy)


@posix_only
def test_root_ancestor_swapped_after_vetting_withholds_the_tree(tmp_path, monkeypatch):
    """A project skills root's own ancestor (``<cwd>/.hermes``) is container-writable too."""
    home = tmp_path / "hh"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    project = tmp_path / "proj"
    (project / ".hermes" / "skills" / "s").mkdir(parents=True)
    (project / ".hermes" / "skills" / "s" / "SKILL.md").write_text("# s")
    decoy = tmp_path / "decoy"
    (decoy / "skills" / "s").mkdir(parents=True)
    (decoy / "skills" / "s" / "secret.md").write_text(SECRET)

    root = project / ".hermes" / "skills"
    real_denier = cf._skill_entry_denier
    fired: list[bool] = []

    def denier(r):
        result = real_denier(r)
        if not fired:
            fired.append(True)
            (project / ".hermes").rename(tmp_path / "moved_away")
            (project / ".hermes").symlink_to(decoy, target_is_directory=True)
        return result

    monkeypatch.setattr(cf, "_skill_entry_denier", denier)
    copy = Path(cf._safe_skills_path(root))

    assert fired
    assert SECRET not in _copied_text(copy)


@posix_only
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
def test_file_swapped_for_fifo_after_vetting_neither_hangs_nor_copies(home, monkeypatch):
    victim = home / "skills" / "s" / "zz_pipe.md"
    victim.write_text("benign")

    def swap(path):
        path.unlink()
        os.mkfifo(path)

    fired = _swap_when_vetted(monkeypatch, "zz_pipe.md", swap)
    result: list[Path] = []
    worker = threading.Thread(target=lambda: result.append(Path(cf._safe_skills_path(home / "skills"))),
                              daemon=True)
    worker.start()
    worker.join(10)

    assert not worker.is_alive(), "opening the swapped-in FIFO blocked the refresh"
    assert fired
    copy = result[0]
    assert not (copy / "s" / "zz_pipe.md").exists()
    assert (copy / "s" / "SKILL.md").is_file()


@posix_only
def test_symlinked_skills_root_is_still_copied(tmp_path, monkeypatch):
    """Portability: a user's ``skills`` that is itself a symlink to their real skills dir
    (dotfiles setups) keeps working; only links *inside* the tree are refused."""
    home = tmp_path / "hh"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    real = tmp_path / "dotfiles" / "skills"
    (real / "s").mkdir(parents=True)
    (real / "s" / "SKILL.md").write_text("# s")
    (real / "s" / "run.sh").write_text("#!/bin/sh\n")
    os.chmod(real / "s" / "run.sh", 0o755)
    (home / "skills").symlink_to(real, target_is_directory=True)

    copy = Path(cf._safe_skills_path(home / "skills"))

    assert (copy / "s" / "SKILL.md").read_text() == "# s"
    assert os.stat(copy / "s" / "run.sh").st_mode & 0o111, "executable bit must survive the copy"
    assert os.stat(copy / "s" / "SKILL.md").st_mtime_ns == os.stat(real / "s" / "SKILL.md").st_mtime_ns


def test_platform_without_anchored_opens_withholds_the_copy(home, monkeypatch, caplog):
    """Fail closed: without O_NOFOLLOW/dir_fd there is no race-free copy, so nothing is copied
    (and a previous copy is emptied) rather than falling back to a path-following copy."""
    (home / "skills" / "s" / "notes.md").write_text("# notes")
    copy = Path(cf._safe_skills_path(home / "skills"))
    assert (copy / "s" / "notes.md").exists() or not cf._ANCHORED_COPY_SUPPORTED

    monkeypatch.setattr(cf, "_ANCHORED_COPY_SUPPORTED", False)
    with caplog.at_level(logging.WARNING, logger=cf.logger.name):
        copy = Path(cf._safe_skills_path(home / "skills"))

    assert not any(p.is_file() for p in copy.rglob("*")), "a path-following copy must not run"
    assert "withholding" in caplog.text
