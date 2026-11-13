"""A fresh HERMES_HOME links the account home's verified state instead of fetching its own copy."""
from __future__ import annotations

import errno
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from pm import paths
from pm.lock import Facts, Lockfile
from pm.package import InstallError, Package
from pm.store import Store, current_target
from tests.pm._range_server import RangeHandler, url
from tests.pm._range_server import dl_server as dl_server  # noqa: PLC0414 -- the self-alias suppresses F811 for the fixture parameter

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Windows seeds copy, never link")

engine = importlib.import_module("pm.install")


def _zip(body: bytes) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("tool.txt", body)
    return data.getvalue()


@pytest.fixture
def tool(tmp_path, dl_server, monkeypatch):
    """One pinned package, installed into the account home's store by a real download."""
    monkeypatch.setattr(paths, "partials_root", lambda: tmp_path / "partials")
    package = Package()
    package.name = "seed-tool"
    lock = Lockfile(tmp_path / "lock.json")

    def pin(version: str, body: bytes):
        archive = _zip(body)
        digest = hashlib.sha256(archive).hexdigest()
        RangeHandler.payloads[f"/{version}.zip"] = archive
        lock.set_pin(package.name, version, {"any": {"url": url(dl_server, f"/{version}.zip"), "sha256": digest}})

    pin("1.0", b"pinned bytes")
    account = Store(tmp_path / ".hermes" / "tools")
    account_facts = Facts(account.root / "facts.json")
    engine._install(package, lock, account_facts, account, current_target())

    borrower = tmp_path / "borrower"
    borrower.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(borrower))
    store = Store(borrower / "tools")

    fact = account_facts.get(package.name)
    assert fact is not None
    t = SimpleNamespace(package=package, lock=lock, pin=pin, store=store, account=account,
                        account_file=account.entry(fact["entry"]) / "tool.txt")

    def install():
        facts = Facts(store.root / "facts.json")
        entry = engine._install(package, lock, facts, store, current_target())
        recorded = facts.get(package.name)
        assert recorded is not None and recorded["digest"]
        return entry / "tool.txt"

    t.install = install
    return t


def test_borrowing_home_links_the_account_entry_without_downloading(tool):
    RangeHandler.payloads.clear()  # a download would now fail the install
    installed = tool.install()
    assert installed.read_bytes() == b"pinned bytes"
    assert os.stat(installed).st_ino == os.stat(tool.account_file).st_ino


def test_link_failure_copies_the_verified_entry(tool, monkeypatch):
    def refuse(src, dst):
        raise OSError(errno.EXDEV, "cross-device")

    monkeypatch.setattr(engine.os, "link", refuse)
    RangeHandler.payloads.clear()
    installed = tool.install()
    assert installed.read_bytes() == b"pinned bytes"
    assert os.stat(installed).st_ino != os.stat(tool.account_file).st_ino


def test_seed_that_vanishes_mid_link_falls_back_to_download(tool, monkeypatch):
    def vanish(src, dst, **_):
        raise FileNotFoundError(errno.ENOENT, "gone", src)

    monkeypatch.setattr(engine, "_link_or_copy", vanish)
    installed = tool.install()
    assert installed.read_bytes() == b"pinned bytes"
    assert os.stat(installed).st_ino != os.stat(tool.account_file).st_ino


def test_tampered_seed_is_never_linked(tool):
    tool.account_file.write_bytes(b"tampered")  # same inode, bytes no longer match the recorded digest
    installed = tool.install()
    assert installed.read_bytes() == b"pinned bytes"
    assert tool.account_file.read_bytes() == b"tampered"


def test_seed_on_another_pin_is_not_used(tool):
    tool.pin("2.0", b"new pin")
    installed = tool.install()
    assert installed.read_bytes() == b"new pin"


@pytest.mark.parametrize("damage", ["no-digest", "corrupt"])
def test_unreadable_or_unvouched_account_facts_download(tool, damage):
    facts_path = tool.account.root / "facts.json"
    if damage == "corrupt":
        facts_path.write_text("{not json", encoding="utf-8")
    else:
        data = json.loads(facts_path.read_text(encoding="utf-8"))
        del data["packages"][tool.package.name]["digest"]
        facts_path.write_text(json.dumps(data), encoding="utf-8")
    installed = tool.install()
    assert installed.read_bytes() == b"pinned bytes"
    assert os.stat(installed).st_ino != os.stat(tool.account_file).st_ino


def test_account_home_installs_never_seed_from_themselves(tool, monkeypatch, tmp_path):
    # A borrowing home whose store IS the account's (HERMES_RUNTIME_DIR) has nothing to seed.
    assert engine._account_seed(tool.package, tool.lock, tool.account, "1.0", current_target()) is None
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    assert engine._account_seed(tool.package, tool.lock, tool.store, "1.0", current_target()) is None


def test_failed_seed_leaves_nothing_in_the_downloaded_tree(tool, monkeypatch):
    """An unpack that does not empty its destination (npm, .deb) must not inherit a dropped seed."""
    def merge_unpack(archive, staged, target):
        staged.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as opened:
            opened.extractall(staged)

    monkeypatch.setattr(tool.package, "unpack", merge_unpack)
    (tool.account_file.parent / "extra.txt").write_bytes(b"not in the pin")  # digest no longer matches
    installed = tool.install()
    assert installed.read_bytes() == b"pinned bytes"
    assert not (installed.parent / "extra.txt").exists()


def test_cross_target_stages_are_never_linked(tool, monkeypatch):
    """A stage feeds a bundle that tooling may rewrite in place, so it never shares inodes."""
    monkeypatch.setattr(engine, "_lockfile", lambda: tool.lock)
    monkeypatch.setattr(engine, "_store", lambda: tool.store)
    monkeypatch.setattr(engine, "get_package", lambda _: tool.package)
    RangeHandler.payloads.clear()
    with pytest.raises(InstallError):
        engine.stage_only(tool.package.name, current_target())


# --- account_home_root ------------------------------------------------------------------------

def test_account_home_root_is_only_another_home_on_the_same_filesystem(tmp_path, monkeypatch):
    from pm import environments

    account = tmp_path / ".hermes"
    assert environments.account_home_root() is None  # the account home itself
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "fresh" / "not-yet-created"))
    assert environments.account_home_root() == account

    real_stat = os.stat

    def other_device(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if Path(path) == account:
            fields = list(result)
            fields[2] += 1  # st_dev
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(environments.os, "stat", other_device)
    assert environments.account_home_root() is None


def test_no_account_home_means_no_borrowing(tmp_path, monkeypatch):
    from pm import environments

    monkeypatch.setenv("HOME", str(tmp_path / "nobody"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nobody")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "fresh"))
    assert environments.account_home_root() is None


# --- uv cache ---------------------------------------------------------------------------------

def test_borrowing_home_shares_the_account_uv_cache(tmp_path, monkeypatch):
    from pm.packages import uv_cache_path

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "fresh"))
    assert uv_cache_path() == tmp_path / "fresh" / "cache" / "uv"  # the account has no cache to share
    shared = tmp_path / ".hermes" / "cache" / "uv"
    shared.mkdir(parents=True)
    assert uv_cache_path() == shared
    shared.chmod(0o500)
    try:
        if not os.access(shared, os.W_OK):  # root ignores the mode
            assert uv_cache_path() == tmp_path / "fresh" / "cache" / "uv"
    finally:
        shared.chmod(0o700)


# --- bytecode ---------------------------------------------------------------------------------

def _environment(destination: Path):
    from pm.environment import PythonEnvironment

    return PythonEnvironment(uv=Path("uv"), python=Path("python"), destination=destination,
                             cache=Path("cache"), env={})


def test_borrowing_home_environments_compile_bytecode_lazily(tmp_path, monkeypatch):
    from pm.environments import installs_root

    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    assert _environment(installs_root() / "k" / "environments" / "g" / "venv")._compile_bytecode()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "fresh"))
    assert not _environment(installs_root() / "k" / "environments" / "g" / "venv")._compile_bytecode()
    assert not _environment(installs_root() / "k" / "pm-runtime" / "generations" / "g")._compile_bytecode()
    assert _environment(tmp_path / "payload" / "venv")._compile_bytecode()  # a bundle build keeps it


def test_borrowing_home_compiles_eagerly_when_lazy_bytecode_is_never_written(tmp_path, monkeypatch):
    """PYTHONDONTWRITEBYTECODE (the Docker image) drops lazy pyc: each start would recompile."""
    from pm.environments import installs_root

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "fresh"))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    assert _environment(installs_root() / "k" / "environments" / "g" / "venv")._compile_bytecode()


def test_sync_passes_compile_bytecode_only_when_wanted(tmp_path, monkeypatch):
    import subprocess

    from pm.environment import PythonEnvironment

    seen = []

    def run(self, args, *, cwd, timeout):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(PythonEnvironment, "_run", run)
    for wanted in (True, False):
        monkeypatch.setattr(PythonEnvironment, "_compile_bytecode", lambda self, w=wanted: w)
        _environment(tmp_path / "venv").sync(tmp_path)
        assert ("--compile-bytecode" in seen[-1]) is wanted


# --- source snapshot --------------------------------------------------------------------------

def test_snapshot_links_identical_files_from_the_donor(tmp_path):
    from pm.workspace import _snapshot_copier

    source, donor, out = tmp_path / "src", tmp_path / "donor", tmp_path / "out"
    for root in (source, donor, out):
        root.mkdir()
    for name, mine, theirs in (("same.py", b"a", b"a"), ("changed.py", b"new", b"old"),
                               ("mode.py", b"m", b"m"), ("only-mine.py", b"x", None)):
        (source / name).write_bytes(mine)
        if theirs is not None:
            (donor / name).write_bytes(theirs)
    (source / "mode.py").chmod(0o755)
    (donor / "mode.py").chmod(0o644)
    copy = _snapshot_copier(source, donor)
    for name in ("same.py", "changed.py", "mode.py", "only-mine.py"):
        copy(str(source / name), str(out / name))
        assert (out / name).read_bytes() == (source / name).read_bytes()
    assert os.stat(out / "same.py").st_ino == os.stat(donor / "same.py").st_ino
    for name in ("changed.py", "mode.py", "only-mine.py"):
        assert os.stat(out / name).st_nlink == 1


def test_snapshot_donor_is_the_account_selection_for_this_checkout(tmp_path, monkeypatch):
    from pm.environments import install_key
    from pm.workspace import _snapshot_donor

    source = tmp_path / "checkout"
    source.mkdir()
    state = tmp_path / ".hermes" / "installs" / install_key(source)
    generation = state / "environments" / "g"
    (generation / "workspace").mkdir(parents=True)
    (state / "facts.json").write_text(json.dumps(
        {"schema": 1, "packages": {"venv": {"environment": str(generation / "venv")}}}), encoding="utf-8")
    assert _snapshot_donor(source) is None  # the account home borrows from nobody
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "fresh"))
    assert _snapshot_donor(source) == generation / "workspace"
    assert _snapshot_donor(tmp_path / "other-checkout") is None
    (generation / "workspace").rmdir()
    assert _snapshot_donor(source) is None  # a selection whose snapshot is gone is no donor


def test_install_error_is_not_swallowed_by_the_seed(tool, monkeypatch):
    """A seed refusal never hides the real install's failure."""
    RangeHandler.payloads.clear()
    tool.account_file.write_bytes(b"tampered")
    with pytest.raises(InstallError):
        tool.install()
