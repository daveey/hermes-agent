"""Root-owned build output in finished scratch is recorded, adopted, archived and removed. [daveey]

worker-a2 (2026-11-23): two done cards' root-filesystem builds (88 GiB) were kept forever:
every hourly archive retry hit PermissionError (root-owned files) or a device node, and the
worker stopped claiming cards at its disk floor (``worker_disk_below_floor``).
"""
import json
import os
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_scratch_archive as sa
from hermes_cli import kanban_scratch_archive_ownership as own
from tests.hermes_cli import test_kanban_scratch_gc_archive as base

board = base.board


class FakeSudo:
    """Answers the module's sudo calls by shape; records every argv."""

    def __init__(self, *, sudo_ok=True, foreign=b"", listing=b"", earlier=None, linked=b"", record_kind=b"",
                 fail=None):
        self.sudo_ok, self.foreign, self.listing, self.earlier = sudo_ok, foreign, listing, earlier
        self.linked, self.record_kind, self.fail = linked, record_kind, fail
        self.calls, self.written = [], None

    def __call__(self, argv, input=None, capture_output=True, timeout=None):
        self.calls.append(argv)
        cmd = argv[2] if argv[:2] == ["sudo", "-n"] else argv[0]
        if cmd == self.fail:
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"Operation not permitted")
        handler = {"true": self._true, "find": self._find, "cat": self._cat, "mv": self._mv}.get(cmd)
        rc, out = handler(argv) if handler else (0, b"")
        return SimpleNamespace(returncode=rc, stdout=out, stderr=b"")

    def _true(self, argv):
        return (0 if self.sudo_ok else 1), b""

    def _find(self, argv):
        if "-maxdepth" in argv:
            return 0, self.record_kind
        if "-links" in argv:
            return 0, self.linked
        if "-quit" in argv:
            return 0, self.foreign
        return 0, (self.listing if "-printf" in argv else b"")

    def _cat(self, argv):
        return (0, self.earlier) if self.earlier is not None else (1, b"")

    def _mv(self, argv):
        with open(argv[-2], "rb") as fh:
            self.written = json.loads(fh.read())
        return 0, b""

    def ran(self, cmd, flag=None):
        return [c for c in self.calls if c[:3] == ["sudo", "-n", cmd] and (flag is None or flag in c)]


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(own.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(own.os, "getegid", lambda: 1000)
    monkeypatch.setattr(own.sys, "platform", "linux")
    monkeypatch.setattr(own, "mounts_below", lambda root: [])
    monkeypatch.setattr(own, "_grant_owner_access", lambda root, rows: None)

    def make(**kw):
        f = FakeSudo(**kw)
        monkeypatch.setattr(own, "_run", f)
        return f
    return make


def _ws(tmp_path):
    ws = tmp_path / "workspaces" / "t_x"
    ws.mkdir(parents=True)
    return ws


def test_nothing_foreign_changes_nothing(fake, tmp_path):
    f = fake(foreign=b"")
    assert own.adopt(_ws(tmp_path)) == []
    assert not f.ran("find", "-exec") and not f.ran("mv")


def test_no_passwordless_sudo_changes_nothing(fake, tmp_path):
    f = fake(sudo_ok=False, foreign=b"x\n")
    assert own.adopt(_ws(tmp_path)) == []
    assert not f.ran("find")


def test_foreign_entries_are_recorded_before_any_owner_change(fake, tmp_path):
    ws = _ws(tmp_path)
    listing = b"0 0 755 d \x000 0 644 f rootfs/etc/hosts\x000 0 4755 f rootfs/bin/su\x001000 0 640 f grp\x00"
    f = fake(foreign=b"x\n", listing=listing)
    rows = own.adopt(ws)
    assert [r["path"] for r in rows] == [".", "rootfs/etc/hosts", "rootfs/bin/su", "grp"]
    assert f.written["entries"][2] == {"path": "rootfs/bin/su", "uid": 0, "gid": 0, "mode": 0o4755, "type": "f"}
    assert f.written["adopted_to"] == [1000, 1000]
    mv, chown = f.ran("mv"), f.ran("find", "-exec")
    root = os.path.realpath(ws)
    assert mv[0][-1] == os.path.join(root, own.RECORD_NAME) and mv[0][3:5] == ["-T", "-f"]
    assert chown == [["sudo", "-n", "find", root, "-xdev", "(", "!", "-uid", "1000", "-o", "!", "-gid", "1000", ")",
                      "-exec", "chown", "-h", "1000:1000", "--", "{}", "+"]]
    assert f.calls.index(mv[0]) < f.calls.index(chown[0])  # the record exists before any owner changes
    assert all("-xdev" in c for c in f.ran("find") if "-maxdepth" not in c)
    assert not list((tmp_path / "workspaces").glob(".t_x.ownership-*"))  # temp record renamed in, none left


def test_earlier_record_is_merged_not_replaced(fake, tmp_path):
    earlier = json.dumps({"version": 1, "entries": [{"path": "a", "uid": 0, "gid": 0, "mode": 0o600, "type": "f"}]})
    f = fake(foreign=b"x\n", listing=b"0 0 644 f b\x001000 0 644 f " + own.RECORD_NAME.encode() + b"\x00",
             earlier=earlier.encode())
    own.adopt(_ws(tmp_path))
    assert [e["path"] for e in f.written["entries"]] == ["a", "b"]


@pytest.mark.parametrize("kw,match", [
    (dict(earlier=b"{not json"), "unreadable"),
    (dict(linked=b"/ws/rootfs/usr/lib/libx.so\n"), "hard links"),
    (dict(record_kind=b"/ws/" + own.RECORD_NAME.encode() + b"\n"), "not a regular file"),
    (dict(fail="find"), "find failed"),
])
def test_unsafe_shapes_refuse_before_any_change(fake, tmp_path, kw, match):
    f = fake(foreign=b"x\n", listing=b"0 0 644 f b\x00", **kw)
    with pytest.raises(sa.ArchiveRefused, match=match):
        own.adopt(_ws(tmp_path))
    assert not f.ran("find", "-exec") and not f.ran("mv")


def test_owner_changed_refuses_before_any_change(fake, tmp_path):
    f = fake(foreign=b"x\n", listing=b"0 0 644 f b\x00")
    with pytest.raises(sa.ArchiveRefused, match="owner changed"):
        own.adopt(_ws(tmp_path), owner_check=lambda: False)
    assert not f.ran("find", "-exec") and not f.ran("mv")


def test_mount_inside_workspace_refuses_before_any_change(fake, monkeypatch, tmp_path):
    f = fake(foreign=b"x\n", listing=b"0 0 644 f b\x00")
    monkeypatch.setattr(own, "mounts_below", lambda root: [str(tmp_path / "rootfs/dev")])
    with pytest.raises(sa.ArchiveRefused, match="mount point"):
        own.adopt(_ws(tmp_path))
    assert not f.ran("find", "-exec") and not f.ran("mv")


def test_failed_chown_refuses_after_the_record_is_in_place(fake, tmp_path):
    f = fake(foreign=b"x\n", listing=b"0 0 644 f b\x00")
    real = f._find
    f._find = lambda argv: (1, b"") if "-exec" in argv else real(argv)
    with pytest.raises(sa.ArchiveRefused, match="find failed"):
        own.adopt(_ws(tmp_path))
    assert f.written is not None and f.written["entries"][0]["path"] == "b"


def test_grant_owner_access_opens_only_what_the_worker_needs(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").write_text("x")
    os.chmod(tmp_path / "d" / "f", 0o000)
    os.chmod(tmp_path / "d", 0o555)
    (tmp_path / "keep").write_text("y")
    os.chmod(tmp_path / "keep", 0o444)
    rows = [{"path": "d", "type": "d"}, {"path": "d/f", "type": "f"}]
    own._grant_owner_access(str(tmp_path), rows)
    assert stat.S_IMODE(os.lstat(tmp_path / "d").st_mode) == 0o755
    assert stat.S_IMODE(os.lstat(tmp_path / "d" / "f").st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(tmp_path / "keep").st_mode) == 0o444  # not adopted: untouched


def test_mounts_below_reads_mountinfo(tmp_path):
    root = os.path.realpath(tmp_path)
    info = (f"36 35 0:30 / {root}/rootfs/dev rw - devtmpfs udev rw\n"
            f"37 35 0:31 / {root}x/other rw - tmpfs t rw\n"
            f"38 35 0:32 / {root}/with\\040space rw - tmpfs t rw\n"
            "39 35 0:33 / / rw - ext4 /dev/root rw\n")
    assert own.mounts_below(tmp_path, info) == [f"{root}/rootfs/dev", f"{root}/with space"]


def _sudo_ok():
    if not sys.platform.startswith("linux") or os.geteuid() == 0 or not shutil.which("sudo"):
        return False
    return subprocess.run(["sudo", "-n", "true"], capture_output=True, check=False).returncode == 0


real_sudo = pytest.mark.skipif(not _sudo_ok(), reason="needs a non-root user with passwordless sudo (pool workers)")


def _sh(*argv):
    subprocess.run(["sudo", "-n", *argv], check=True)


def _cleanup(path):
    if path.exists():
        _sh("rm", "-rf", "--one-file-system", str(path))


@real_sudo
def test_root_owned_build_is_archived_and_removed_with_real_sudo(board):
    def build(p):
        (p / "rootfs" / "etc").mkdir(parents=True)
        (p / "rootfs" / "etc" / "shadow").write_text("root:*:1::::::\n")
        (p / "rootfs" / "ro").mkdir()
        (p / "rootfs" / "ro" / "a").write_text("a")
        (p / "grp.txt").write_text("g")
        _sh("mknod", str(p / "rootfs" / "null"), "c", "1", "3")
        _sh("chown", "-R", "0:0", str(p / "rootfs"))
        _sh("chgrp", "0", str(p / "grp.txt"))  # worker uid, foreign gid
        _sh("chmod", "0000", str(p / "rootfs" / "etc" / "shadow"))
        _sh("chmod", "0700", str(p / "rootfs" / "etc"))
        _sh("chmod", "0555", str(p / "rootfs" / "ro"))

    tid, path = base._plain_task(build)
    try:
        assert base._gc() == 0
        assert not path.exists()
        assert not list(path.parent.glob(f".{path.name}.*"))  # no detached or temp leftovers
        members = base._members(tid)
        assert members["rootfs/etc/shadow"][1] == b"root:*:1::::::\n"
        record = {e["path"]: e for e in json.loads(members[own.RECORD_NAME][1])["entries"]}
        assert record["rootfs/etc"]["uid"] == 0 and record["rootfs/etc"]["mode"] == 0o700
        assert record["rootfs/etc/shadow"]["mode"] == 0
        assert record["rootfs/ro"]["mode"] == 0o555
        assert record["grp.txt"]["uid"] == os.getuid() and record["grp.txt"]["gid"] == 0
        assert record["rootfs/null"]["type"] == "c"
        dev = json.loads((base._archive(tid) / base.PAYLOAD / "manifest.json").read_text())["entries"]["rootfs/null"]
        assert dev["type"] == "chardev" and dev["rdev"] == [1, 3]
        sa.verify_complete_archive(base._archive(tid))
    finally:
        _cleanup(path)


@real_sudo
def test_hard_linked_foreign_file_is_left_untouched(board, tmp_path):
    outside = tmp_path / "outside-root-file"
    outside.write_text("host")
    _sh("chown", "0:0", str(outside))

    def build(p):
        _sh("ln", str(outside), str(p / "hl"))

    _tid, path = base._plain_task(build)
    try:
        base._gc()
        assert path.exists() and os.stat(outside).st_uid == 0
        assert not (path / own.RECORD_NAME).exists()
    finally:
        _cleanup(path)
        _sh("rm", "-f", str(outside))


@real_sudo
def test_symlinked_record_is_never_written_through(board, tmp_path):
    target = tmp_path / "target"
    target.write_text("keep me")

    def build(p):
        (p / own.RECORD_NAME).symlink_to(target)
        (p / "r").write_text("r")
        _sh("chown", "0:0", str(p / "r"))

    _tid, path = base._plain_task(build)
    try:
        base._gc()
        assert target.read_text() == "keep me" and path.exists()
        assert os.lstat(path / "r").st_uid == 0
    finally:
        _cleanup(path)
