"""The Windows sanitized skills copy: pinned-handle reads instead of an empty mount.

Windows has no ``O_NOFOLLOW``/``dir_fd`` opens. The copy used to be withheld there, which
emptied every skills mount. It now reads through ``tools.credential_files_windows``: each
ancestor and each file is held open without ``FILE_SHARE_DELETE`` through the read, every
component is opened with ``FILE_FLAG_OPEN_REPARSE_POINT``, and identity plus final path are
checked on the handle before anything is read.

Two lanes:

* Linux, via ``pinned_handles_emulation``. This exercises the walk's logic (parity with
  the POSIX copy, reparse/identity/final-path refusals, ancestors held while reading). It
  can't show that a held handle blocks a rename, because Linux has no share modes.
* Windows-only (``platforms("windows")``): the real Win32 binding, junction swaps, and
  renames refused while pinned. These tests run only on the Windows CI lane.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import tools.credential_files as cf
import tools.credential_files_windows as winwalk
from tests.tools import pinned_handles_emulation as emulation
from tests.tools.test_skills_copy_race import SECRET, _copied_text, _swap_when_vetted

emulated = pytest.mark.skipif(not emulation.AVAILABLE, reason="pinned-handle emulation needs Linux /proc + O_PATH")
windows_only = pytest.mark.platforms("windows")


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "hh"
    (home / "skills" / "s").mkdir(parents=True)
    (home / "skills" / "s" / "SKILL.md").write_text("---\nname: s\n---\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def pinned(monkeypatch) -> emulation.LinuxPinnedHandles:
    api = emulation.LinuxPinnedHandles()
    monkeypatch.setattr(cf, "_ANCHORED_COPY_SUPPORTED", False)
    monkeypatch.setattr(cf, "_WINDOWS_HANDLE_API", api)
    return api


def _tree(copy: Path) -> dict[str, bytes]:
    return {p.relative_to(copy).as_posix(): p.read_bytes() for p in sorted(copy.rglob("*")) if p.is_file()}


def _populate(skills: Path, tmp_path: Path) -> None:
    (skills / "s" / "references").mkdir()
    (skills / "s" / "references" / "ref.md").write_bytes(b"a\r\nb\n\x1a tail")
    (skills / "s" / ".env").write_text(SECRET)
    (skills / ".hub").mkdir()
    (skills / ".hub" / "cache.json").write_text("{}")
    (skills / "s" / "node_modules").mkdir()
    (skills / "s" / "node_modules" / "x.js").write_text("x")


# --- Linux emulation of the pinned walk ---

@emulated
def test_pinned_copy_matches_the_anchored_copy(home, tmp_path, monkeypatch):
    """Parity: the Windows walk ships exactly what the POSIX walk ships, byte for byte."""
    skills = home / "skills"
    _populate(skills, tmp_path)
    (skills / "s" / "link.md").symlink_to(skills / "s" / "SKILL.md")
    anchored = _tree(Path(cf._safe_skills_path(skills)))

    monkeypatch.setattr(cf, "_ANCHORED_COPY_SUPPORTED", False)
    monkeypatch.setattr(cf, "_WINDOWS_HANDLE_API", emulation.LinuxPinnedHandles())
    for p in Path(cf._safe_skills_path(skills)).rglob("*"):  # start from an empty copy
        if p.is_file():
            p.unlink()
    pinned = _tree(Path(cf._safe_skills_path(skills)))

    assert pinned == anchored
    assert set(pinned) == {"s/SKILL.md", "s/references/ref.md"}
    assert pinned["s/references/ref.md"] == b"a\r\nb\n\x1a tail"


@emulated
def test_pinned_walk_holds_every_ancestor_while_reading_and_releases_them(home, pinned):
    (home / "skills" / "s" / "deep").mkdir()
    (home / "skills" / "s" / "deep" / "d.md").write_text("d")

    copy = Path(cf._safe_skills_path(home / "skills"))

    assert (copy / "s" / "deep" / "d.md").read_text() == "d"
    assert pinned.file_opens, "no file was opened through the handle API"
    for path, held in pinned.file_opens:
        chain = [str(p) for p in Path(path).parents]
        assert set(chain) <= held, f"{path} was read with ancestors unpinned: {sorted(set(chain) - held)}"
    assert pinned.held() == [], "the walk leaked pinned handles"


@emulated
def test_final_path_mismatch_alone_refuses_the_file(home, pinned, monkeypatch, caplog):
    """Everything else about the handle checks out; only its final path is elsewhere."""
    (home / "skills" / "s" / "zz.md").write_text("benign")
    real_final = pinned.final_path

    def final_path(fd):
        path = real_final(fd)
        return path.replace("zz.md", "elsewhere.md") if path.endswith("zz.md") else path

    monkeypatch.setattr(pinned, "final_path", final_path)
    with caplog.at_level(logging.WARNING, logger=cf.logger.name):
        copy = Path(cf._safe_skills_path(home / "skills"))

    assert not (copy / "s" / "zz.md").exists()
    assert (copy / "s" / "SKILL.md").is_file()
    assert "opens" in caplog.text and "elsewhere.md" in caplog.text


@emulated
def test_file_replaced_by_another_regular_file_after_vetting_is_refused(home, tmp_path, pinned, monkeypatch):
    """Identity check: a different file under the vetted name is not read."""
    (home / "skills" / "s" / "zz.md").write_text("benign")

    def swap(path):
        path.rename(tmp_path / "kept_alive.md")  # keep the vetted inode alive so it can't be reused
        path.write_text("REPLACEMENT")

    fired = _swap_when_vetted(monkeypatch, "zz.md", swap)
    copy = Path(cf._safe_skills_path(home / "skills"))

    assert fired
    assert "REPLACEMENT" not in _copied_text(copy)
    assert (copy / "s" / "SKILL.md").is_file()


def test_reparse_attribute_is_refused_even_without_a_link_mode():
    """A junction lstat()s as a directory on Windows; the attribute alone must refuse it."""
    junction = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_file_attributes=winwalk.FILE_ATTRIBUTE_REPARSE_POINT)
    plain = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_file_attributes=0x10)
    assert winwalk._is_reparse(junction)
    assert not winwalk._is_reparse(plain)
    assert winwalk._is_reparse(SimpleNamespace(st_mode=stat.S_IFLNK | 0o777))


def test_final_path_prefixes_are_normalized():
    assert winwalk._plain("\\\\?\\UNC\\srv\\share\\x") == os.path.normcase("\\\\srv\\share\\x")
    assert winwalk._plain("\\\\?\\C:\\Users\\u") == os.path.normcase("C:\\Users\\u")


@pytest.mark.platforms("posix")
def test_no_win32_binding_off_windows():
    assert winwalk.load_api() is None
    assert cf._WINDOWS_HANDLE_API is None  # POSIX keeps the anchored walk


# --- Real Windows (CI lane only) ---

def _junction(target: Path, link: Path) -> None:
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


@windows_only
def test_windows_copies_a_clean_tree_for_real(home, tmp_path):
    skills = home / "skills"
    _populate(skills, tmp_path)

    copy = Path(cf._safe_skills_path(skills))

    assert not cf._ANCHORED_COPY_SUPPORTED and cf._WINDOWS_HANDLE_API is not None
    tree = _tree(copy)
    assert set(tree) == {"s/SKILL.md", "s/references/ref.md"}, "clean files ship; .env/.hub/node_modules don't"
    assert tree["s/references/ref.md"] == b"a\r\nb\n\x1a tail", "bytes must not be rewritten (text mode)"
    assert os.stat(copy / "s" / "SKILL.md").st_mtime_ns == os.stat(skills / "s" / "SKILL.md").st_mtime_ns


@windows_only
def test_windows_refresh_replaces_and_removes_read_only_files(home):
    src = home / "skills" / "s" / "ro.md"
    src.write_text("v1")
    os.chmod(src, stat.S_IREAD)
    copy = Path(cf._safe_skills_path(home / "skills"))
    assert (copy / "s" / "ro.md").read_text() == "v1"

    os.chmod(src, stat.S_IREAD | stat.S_IWRITE)
    src.write_text("version 2")
    os.chmod(src, stat.S_IREAD)
    cf._safe_skills_path(home / "skills")
    assert (copy / "s" / "ro.md").read_text() == "version 2"

    os.chmod(src, stat.S_IREAD | stat.S_IWRITE)
    src.unlink()
    cf._safe_skills_path(home / "skills")
    assert not (copy / "s" / "ro.md").exists()
    assert (copy / "s" / "SKILL.md").is_file()


@windows_only
def test_windows_dir_swapped_for_junction_before_descent_is_not_walked(home, tmp_path, monkeypatch):
    sub = home / "skills" / "s" / "sub"
    sub.mkdir()
    (sub / "a.md").write_text("benign")
    host_dir = tmp_path / "host_dir"
    host_dir.mkdir()
    (host_dir / "a.md").write_text(SECRET)

    def swap(path):
        path.rename(tmp_path / "moved_away")  # not yet opened: nothing pins it
        _junction(host_dir, path)

    fired = _swap_when_vetted(monkeypatch, "sub", swap)
    copy = Path(cf._safe_skills_path(home / "skills"))

    assert fired
    assert SECRET not in _copied_text(copy)
    assert (copy / "s" / "SKILL.md").is_file()


@windows_only
def test_windows_root_ancestor_swapped_for_junction_withholds_the_tree(tmp_path, monkeypatch):
    home = tmp_path / "hh"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    project = tmp_path / "proj"
    (project / ".hermes" / "skills" / "s").mkdir(parents=True)
    (project / ".hermes" / "skills" / "s" / "SKILL.md").write_text("# s")
    decoy = tmp_path / "decoy"
    (decoy / "skills" / "s").mkdir(parents=True)
    (decoy / "skills" / "s" / "secret.md").write_text(SECRET)
    real_denier = cf._skill_entry_denier
    fired: list[bool] = []

    def denier(r):
        result = real_denier(r)
        if not fired:
            fired.append(True)
            (project / ".hermes").rename(tmp_path / "moved_away")
            _junction(decoy, project / ".hermes")
        return result

    monkeypatch.setattr(cf, "_skill_entry_denier", denier)
    copy = Path(cf._safe_skills_path(project / ".hermes" / "skills"))

    assert fired
    assert SECRET not in _copied_text(copy)


@windows_only
def test_windows_pinned_ancestors_cannot_be_renamed_while_a_file_is_read(home, tmp_path, monkeypatch):
    """The guarantee the final-path check alone can't give: while a file in ``sub`` is vetted
    and read, ``sub`` and the skills root are held without delete sharing, so a swap fails."""
    sub = home / "skills" / "s" / "sub"
    sub.mkdir()
    (sub / "zz.md").write_text("benign")
    host_dir = tmp_path / "host_dir"
    host_dir.mkdir()
    (host_dir / "zz.md").write_text(SECRET)
    refused: list[str] = []

    def swap(path):
        for victim in (path.parent, home / "skills"):
            try:
                victim.rename(tmp_path / f"moved_{victim.name}")
            except OSError:
                refused.append(victim.name)
            else:
                _junction(host_dir, victim)

    fired = _swap_when_vetted(monkeypatch, "zz.md", swap)
    copy = Path(cf._safe_skills_path(home / "skills"))

    assert fired
    assert refused == ["sub", "skills"], "a pinned ancestor was renamed mid-walk"
    assert SECRET not in _copied_text(copy)
    assert (copy / "s" / "sub" / "zz.md").read_text() == "benign"


@windows_only
def test_windows_file_swapped_for_symlink_after_vetting_is_not_followed(home, tmp_path, monkeypatch):
    secret = tmp_path / "host_secret.txt"
    secret.write_text(SECRET)
    probe = tmp_path / "probe_link"
    try:
        probe.symlink_to(secret)
    except OSError:
        pytest.skip("creating symlinks needs Developer Mode or SeCreateSymbolicLinkPrivilege")
    victim = home / "skills" / "s" / "zz_payload.md"
    victim.write_text("benign")

    def swap(path):
        path.unlink()  # not yet opened (only listed), so delete is allowed
        path.symlink_to(secret)

    fired = _swap_when_vetted(monkeypatch, "zz_payload.md", swap)
    copy = Path(cf._safe_skills_path(home / "skills"))

    assert fired
    assert SECRET not in _copied_text(copy)
    assert (copy / "s" / "SKILL.md").is_file()
