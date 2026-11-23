"""Root-owned build output in finished scratch is recorded, chowned, archived and removed. [daveey]

worker-a2 (2026-11-23): two done cards' root-filesystem builds (88 GiB) were kept forever:
every hourly archive retry hit PermissionError (root-owned files) or a device node, and the
worker stopped claiming cards at its disk floor (``worker_disk_below_floor``).
"""
import json
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_scratch_archive as sa
from hermes_cli import kanban_scratch_archive_ownership as own
from tests.hermes_cli import test_kanban_scratch_gc_archive as base

board = base.board
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="sudo adoption is Linux-only")


class FakeSudo:
    def __init__(self, *, sudo_ok=True, foreign=b"", listing=b"", earlier=None):
        self.sudo_ok, self.foreign, self.listing, self.earlier = sudo_ok, foreign, listing, earlier
        self.calls, self.written = [], None

    def __call__(self, argv, input=None, capture_output=True, timeout=None):
        self.calls.append(argv)
        cmd = argv[2] if argv[:2] == ["sudo", "-n"] else argv[0]
        handler = {"true": self._true, "find": self._find, "cat": self._cat, "tee": self._tee}.get(cmd)
        rc, out = handler(argv, input) if handler else (0, b"")
        return SimpleNamespace(returncode=rc, stdout=out, stderr=b"")

    def _true(self, argv, input):
        return (0 if self.sudo_ok else 1), b""

    def _find(self, argv, input):
        return 0, (self.foreign if "-quit" in argv else self.listing)

    def _cat(self, argv, input):
        return (0, self.earlier) if self.earlier is not None else (1, b"")

    def _tee(self, argv, input):
        self.written = json.loads(input)
        return 0, b""

    def ran(self, cmd):
        return [c for c in self.calls if c[:3] == ["sudo", "-n", cmd]]


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setattr(own.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(own.os, "getegid", lambda: 1000)
    monkeypatch.setattr(own.sys, "platform", "linux")
    monkeypatch.setattr(own, "mounts_below", lambda root: [])

    def make(**kw):
        f = FakeSudo(**kw)
        monkeypatch.setattr(own, "_run", f)
        return f
    return make


def test_nothing_foreign_changes_nothing(fake, tmp_path):
    f = fake(foreign=b"")
    assert own.adopt(tmp_path) == []
    assert not f.ran("chown") and not f.ran("tee")


def test_no_passwordless_sudo_changes_nothing(fake, tmp_path):
    f = fake(sudo_ok=False, foreign=b"x\n")
    assert own.adopt(tmp_path) == []
    assert not f.ran("find") and not f.ran("chown")


def test_foreign_entries_are_recorded_before_chown(fake, tmp_path):
    listing = b"0 0 755 d \x000 0 644 f rootfs/etc/hosts\x000 0 4755 f rootfs/bin/su\x00"
    f = fake(foreign=b"x\n", listing=listing)
    rows = own.adopt(tmp_path)
    assert [r["path"] for r in rows] == [".", "rootfs/etc/hosts", "rootfs/bin/su"]
    assert f.written["entries"][2] == {"path": "rootfs/bin/su", "uid": 0, "gid": 0, "mode": 0o4755, "type": "f"}
    assert f.written["adopted_to"] == [1000, 1000]
    tee, chown = f.ran("tee"), f.ran("chown")
    assert tee[0][-1] == os.path.join(os.path.realpath(tmp_path), own.RECORD_NAME)
    assert chown == [["sudo", "-n", "chown", "-R", "-P", "--no-dereference", "1000:1000", "--",
                      os.path.realpath(tmp_path)]]
    assert f.calls.index(tee[0]) < f.calls.index(chown[0])  # the record exists before any owner changes
    assert all("-xdev" in c for c in f.ran("find"))


def test_earlier_record_is_merged_not_replaced(fake, tmp_path):
    earlier = json.dumps({"version": 1, "entries": [{"path": "a", "uid": 0, "gid": 0, "mode": 0o600, "type": "f"}]})
    f = fake(foreign=b"x\n", listing=b"0 0 644 f b\x001000 0 644 f " + own.RECORD_NAME.encode() + b"\x00",
             earlier=earlier.encode())
    own.adopt(tmp_path)
    assert [e["path"] for e in f.written["entries"]] == ["a", "b"]


def test_unreadable_earlier_record_refuses_without_chown(fake, tmp_path):
    f = fake(foreign=b"x\n", listing=b"0 0 644 f b\x00", earlier=b"{not json")
    with pytest.raises(sa.ArchiveRefused, match="unreadable"):
        own.adopt(tmp_path)
    assert not f.ran("chown")


def test_mount_inside_workspace_refuses_without_chown(fake, monkeypatch, tmp_path):
    f = fake(foreign=b"x\n", listing=b"0 0 644 f b\x00")
    monkeypatch.setattr(own, "mounts_below", lambda root: [str(tmp_path / "rootfs/dev")])
    with pytest.raises(sa.ArchiveRefused, match="mount point"):
        own.adopt(tmp_path)
    assert not f.ran("chown") and not f.ran("tee")


def test_failed_chown_refuses(fake, monkeypatch, tmp_path):
    f = fake(foreign=b"x\n", listing=b"0 0 644 f b\x00")
    real = f.__call__

    def failing(argv, **kw):
        if argv[:3] == ["sudo", "-n", "chown"]:
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"Operation not permitted")
        return real(argv, **kw)
    monkeypatch.setattr(own, "_run", failing)
    with pytest.raises(sa.ArchiveRefused, match="chown failed"):
        own.adopt(tmp_path)


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


@pytest.mark.skipif(not _sudo_ok(), reason="needs a non-root user with passwordless sudo (pool workers have it)")
def test_root_owned_build_is_archived_and_removed_with_real_sudo(board):
    def build(p):
        (p / "rootfs" / "etc").mkdir(parents=True)
        (p / "rootfs" / "etc" / "shadow").write_text("root:*:1::::::\n")
        subprocess.run(["sudo", "-n", "chown", "-R", "0:0", str(p / "rootfs")], check=True)
        subprocess.run(["sudo", "-n", "chmod", "0700", str(p / "rootfs" / "etc")], check=True)
        subprocess.run(["sudo", "-n", "chmod", "0600", str(p / "rootfs" / "etc" / "shadow")], check=True)

    tid, path = base._plain_task(build)
    try:
        assert base._gc() == 0
        assert not path.exists()
        members = base._members(tid)
        assert members["rootfs/etc/shadow"][1] == b"root:*:1::::::\n"
        record = json.loads(members[own.RECORD_NAME][1])
        by_path = {e["path"]: e for e in record["entries"]}
        assert by_path["rootfs/etc"]["uid"] == 0 and by_path["rootfs/etc"]["mode"] == 0o700
        assert by_path["rootfs/etc/shadow"]["mode"] == 0o600
        sa.verify_complete_archive(base._archive(tid))
    finally:
        if path.exists():
            subprocess.run(["sudo", "-n", "rm", "-rf", "--one-file-system", str(path)], check=False)
