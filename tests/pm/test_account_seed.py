"""A fresh HERMES_HOME links the account home's verified state instead of fetching its own copy."""
from __future__ import annotations

import errno
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
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


@pytest.fixture(autouse=True)
def _borrowing_enabled(monkeypatch):
    """CI's setup-pm exports the opt-out; these tests exercise the borrowing it turns off."""
    monkeypatch.delenv("HERMES_PM_ACCOUNT_SEED", raising=False)


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
    import hermes_constants
    from pm import environments

    monkeypatch.setattr(hermes_constants, "_account_home", lambda: tmp_path / "nobody")
    monkeypatch.setenv("HOME", str(tmp_path / "nobody"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nobody")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "fresh"))
    assert environments.account_home_root() is None


@pytest.mark.parametrize("suffix", ["", "-canary"])
def test_fake_home_without_override_uses_passwd_account_cache(tmp_path, monkeypatch, suffix):
    import hermes_constants
    from pm.environments import account_home_root
    from pm.packages import uv_cache_path

    account = tmp_path / (".hermes" + suffix)
    cache = account / "cache" / "uv"
    cache.mkdir(parents=True)
    fake = tmp_path / "fake-home"
    fake.mkdir()
    monkeypatch.setattr(hermes_constants, "_account_home", lambda: tmp_path)
    monkeypatch.setattr(Path, "home", lambda: fake)
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setenv("HERMES_DATA_DIR_SUFFIX", suffix)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert account_home_root() == account
    assert uv_cache_path() == cache
    monkeypatch.setenv("HERMES_PM_ACCOUNT_SEED", "0")
    assert account_home_root() is None
    assert uv_cache_path() == fake / (".hermes" + suffix) / "cache" / "uv"


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


# --- opt-out ----------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["0", "off", "False", " no "])
def test_opt_out_borrows_nothing(tool, monkeypatch, tmp_path, value):
    """CI (setup-pm) and a primary home beside a stale ~/.hermes keep every piece of state their own."""
    from pm import environments
    from pm.packages import uv_cache_path

    (tmp_path / ".hermes" / "cache" / "uv").mkdir(parents=True)
    monkeypatch.setenv("HERMES_PM_ACCOUNT_SEED", value)
    assert environments.account_home_root() is None
    assert uv_cache_path() == tmp_path / "borrower" / "cache" / "uv"
    assert _environment(environments.installs_root() / "k" / "environments" / "g" / "venv")._compile_bytecode()
    installed = tool.install()
    assert installed.read_bytes() == b"pinned bytes"
    assert os.stat(installed).st_ino != os.stat(tool.account_file).st_ino


@pytest.mark.parametrize("value", ["", "1", "yes"])
def test_other_opt_out_values_keep_borrowing(tmp_path, monkeypatch, value):
    from pm import environments

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "fresh"))
    monkeypatch.setenv("HERMES_PM_ACCOUNT_SEED", value)
    assert environments.account_home_root() == tmp_path / ".hermes"


_ACTIVATION = Path(__file__).resolve().parents[2] / "scripts" / "_activation.sh"


def test_borrowing_consults_the_opt_out():
    library = _ACTIVATION.read_text(encoding="utf-8")
    borrow = library[library.index("hermes_borrow_account_store() {") :]
    assert "    hermes_account_seed_enabled || return 1\n" in borrow[: borrow.index("\n}\n")]


@pytest.mark.parametrize(
    "value", ["", "1", "yes", "0", "off", "False", "NO", " no ", "\toff\n", "  0", "n o", "0x"]
)
def test_run_tests_opt_out_matches_pm(monkeypatch, value):
    """scripts/run_tests.sh and run-in-hermes-env (scripts/_activation.sh) decide tool-store
    borrowing with the same opt-out pm uses.

    Regression: its ``case`` skipped pm's whitespace strip, so ``' no '`` stopped pm borrowing
    but not run_tests.sh's."""
    from pm import environments

    monkeypatch.setenv("HERMES_PM_ACCOUNT_SEED", value)
    shell = subprocess.run(
        ["bash", "-c", '. "$1" && hermes_account_seed_enabled', "bash", str(_ACTIVATION)],
        env={"PATH": os.environ.get("PATH", ""), "HERMES_PM_ACCOUNT_SEED": value},
        check=False,
    )
    assert shell.returncode in (0, 1)
    assert (shell.returncode == 0) is environments.account_seed_enabled()


# --- seed marker ------------------------------------------------------------------------------

def test_homes_on_different_locks_do_not_rerun_each_others_seed(tmp_path, monkeypatch):
    """The account and a borrowing home share one uv cache; the marker keeps each lock's seed.

    Regression: a single-value ``.seeded`` flipped to the borrower's lock, so the account merged
    its whole payload cache again on its next call."""
    from pm import packages

    shared = tmp_path / ".hermes" / "cache" / "uv"
    shared.mkdir(parents=True)
    payload = tmp_path / "payload"
    (payload / "uv-cache" / "archive-v0" / "bucket").mkdir(parents=True)
    (payload / "uv-cache" / "archive-v0" / "bucket" / "module.py").write_text("seeded = True\n", encoding="utf-8")
    (payload / "tools").mkdir()
    checkouts = {}
    for name in ("account", "borrower"):
        checkouts[name] = tmp_path / f"code-{name}"
        checkouts[name].mkdir()
        (checkouts[name] / "uv.lock").write_text(f"version = 1\n# {name}\n", encoding="utf-8")
    merges = []
    real_seed_tree = packages._seed_tree

    def counting_seed_tree(src, dst, *rest):  # upstream added the cache ``root`` argument
        if src.parent.name == "uv-cache":  # one payload bucket merged; nested calls recurse
            merges.append(src)
        real_seed_tree(src, dst, *rest)

    monkeypatch.setattr(packages, "_seed_tree", counting_seed_tree)

    def call(home):
        if home == "account":
            monkeypatch.delenv("HERMES_HOME", raising=False)
            monkeypatch.setattr(paths, "store_root", lambda: payload / "tools")
        else:
            monkeypatch.setenv("HERMES_HOME", str(tmp_path / "fresh"))
            monkeypatch.setattr(paths, "store_root", lambda: tmp_path / "fresh" / "tools")
        monkeypatch.setattr(paths, "repo_root", lambda: checkouts[home])
        assert packages.uv_cache_dir().resolve() == shared.resolve()

    call("account")
    assert len(merges) == 1
    call("borrower")
    call("account")
    assert len(merges) == 1, "the borrower's lock evicted the account's seed record"
    assert (shared / "archive-v0" / "bucket" / "module.py").is_file()


def _recorded_digest(store: Store, name: str) -> str:
    fact = Facts(store.root / "facts.json").get(name)
    assert fact is not None
    return fact["digest"]


# --- shared inodes are never written in place --------------------------------------------------

def test_borrower_lifecycle_never_rewrites_an_account_inode(tool):
    """Install, heal, re-pin: every account byte and its recorded digest survive the borrower.

    A borrowing home's entry files ARE the account's inodes (pm/AGENTS.md); only
    ``_heal_exec_bit`` may change one in place, and only by adding execute bits."""
    from pm.store import digest_matches

    account_entry = tool.account_file.parent
    tool.account_file.chmod(0o644)
    before = {path: path.read_bytes() for path in account_entry.rglob("*") if path.is_file()}
    recorded = _recorded_digest(tool.account, tool.package.name)

    installed = tool.install()
    assert os.stat(installed).st_ino == os.stat(tool.account_file).st_ino
    assert engine._heal_exec_bit(installed)
    assert os.stat(tool.account_file).st_mode & 0o777 == 0o755  # +x is the one shared change
    tool.pin("2.0", b"new pin")
    assert tool.install().read_bytes() == b"new pin"

    assert {path: path.read_bytes() for path in account_entry.rglob("*") if path.is_file()} == before
    assert digest_matches(account_entry, recorded)


def test_snapshot_rewrites_never_reach_a_donor_file(tmp_path, monkeypatch):
    """The generation's own rewrites (the root pyproject.toml drops the release-age cutoff) land
    on copies; only unchanged package files are links to the account's snapshot."""
    from pm import workspace

    source, donor, root = tmp_path / "checkout", tmp_path / "donor", tmp_path / "generation"
    pyproject = ('[project]\nname = "core"\nversion = "0"\n'
                 '[tool.setuptools.packages.find]\ninclude = ["pkg*"]\n'
                 '[tool.uv]\nexclude-newer = "3 days"\n')
    for tree in (source, donor):
        (tree / "pkg").mkdir(parents=True)
        (tree / "pkg" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
        (tree / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    monkeypatch.setattr(workspace, "_snapshot_donor", lambda _source: donor)
    before = {path: path.read_bytes() for path in donor.rglob("*") if path.is_file()}

    workspace._generate_pyproject([], root, source=source, python=Path("python"))

    assert os.stat(root / "pkg" / "mod.py").st_ino == os.stat(donor / "pkg" / "mod.py").st_ino
    assert "exclude-newer" not in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert {path: path.read_bytes() for path in donor.rglob("*") if path.is_file()} == before


# --- digest reuse -----------------------------------------------------------------------------

def test_seeded_install_hashes_the_tree_once(tool, monkeypatch):
    """The digest verified against the account's fact is the one recorded, not a second hash."""
    from pm import store as store_module

    hashed = []
    real = store_module._tree_digest

    def counting(root, *, skip_metadata):
        hashed.append(Path(root))
        return real(root, skip_metadata=skip_metadata)

    monkeypatch.setattr(store_module, "_tree_digest", counting)
    RangeHandler.payloads.clear()
    tool.install()
    assert len(hashed) == 1, hashed
    assert _recorded_digest(tool.store, tool.package.name) == _recorded_digest(tool.account, tool.package.name)


def test_repaired_seed_records_the_published_digest(tool, monkeypatch):
    """A staged repair may rewrite the seed, so the verified digest is not reused after one."""
    from pm.store import tree_digest

    seen = []

    def fail_once(entry, target):
        if not seen:
            seen.append(entry)
            return "needs repair"
        return ""

    def repair(entry, target, reason):
        (entry / "repaired.txt").write_bytes(b"added by repair")
        return "", ""

    monkeypatch.setattr(tool.package, "verify", fail_once)
    monkeypatch.setattr(tool.package, "repair_staged_verification", repair)
    RangeHandler.payloads.clear()
    installed = tool.install()
    assert _recorded_digest(tool.store, tool.package.name) == tree_digest(installed.parent)
