"""Inventory rejects incomplete, ambiguous and externally dependent state."""
import hashlib
import io
import tarfile

import pytest

from hermes_cli import kanban_archive_inventory as inventory


@pytest.mark.parametrize("kind", ["valid", "traversal", "absolute", "duplicate", "symlink", "hardlink", "fifo",
                                  "alternates", "gitfile", "limit", "bad-sha"])
def test_archive_inventory(tmp_path, monkeypatch, kind):
    path = tmp_path / "archive.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        root = tarfile.TarInfo(".")
        root.type, root.mode = tarfile.DIRTYPE, 0o755
        tar.addfile(root)
        member = tarfile.TarInfo({"traversal": "../outside", "absolute": "/outside", "duplicate": ".",
                                 "alternates": ".git/objects/info/alternates", "gitfile": ".git"}.get(kind, "file"))
        member.type = {"symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE, "fifo": tarfile.FIFOTYPE}.get(kind, tarfile.REGTYPE)
        member.mode = 0o644
        member.size = 4 if member.isfile() else 0
        member.linkname = "outside" if member.issym() or member.islnk() else ""
        tar.addfile(member, io.BytesIO(b"data") if member.isfile() else None)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if kind == "limit":
        monkeypatch.setattr(inventory, "MAX_ENTRIES", 1)
    if kind == "bad-sha":
        digest = "0" * 64
    if kind != "valid":
        with pytest.raises(ValueError):
            inventory.archive_inventory(path, digest)
    else:
        root = tmp_path / "workspace"
        root.mkdir(mode=0o755)
        (root / "file").write_bytes(b"data")
        (root / "file").chmod(0o644)
        assert inventory.archive_inventory(path, digest) == inventory.tree_inventory(root)


@pytest.mark.parametrize("kind", ["missing", "symlink", "gitfile", "alternates", "limit", "unreadable"])
def test_local_inventory_fails_closed(tmp_path, monkeypatch, kind):
    root = tmp_path / "workspace"
    root.mkdir()
    def alternates():
        info = root / ".git" / "objects" / "info"
        info.mkdir(parents=True)
        (info / "alternates").write_text("/outside")

    def limited():
        (root / "file").write_text("data")
        monkeypatch.setattr(inventory, "MAX_ENTRIES", 1)

    def unreadable():
        def walk(*args, **kwargs):
            kwargs["onerror"](PermissionError("unreadable"))
        monkeypatch.setattr(inventory.os, "walk", walk)

    {"missing": root.rmdir,
     "symlink": lambda: (root / "file").symlink_to("outside"),
     "gitfile": lambda: (root / ".git").write_text("gitdir: /outside"),
     "alternates": alternates, "limit": limited, "unreadable": unreadable}[kind]()
    with pytest.raises((ValueError, OSError)):
        inventory.tree_inventory(root)
