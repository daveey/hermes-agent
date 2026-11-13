"""run_tests.sh and run-in-hermes-env share the account's tool store only when they can run from it as is.

A store whose facts match the checkout's pins must still not be shared when
running from it would write to it: a missing or non-executable binary sends
setup-hermes.sh (uv) or `pm install` into an unlocked re-stage, and an env that
differs from what this checkout's package code records makes `pm install`
re-record the shared facts.json.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pm.defaults import DECLINED_FILENAME, default_packages
from pm.environments import install_key
from pm.lock import Facts, Lockfile
from pm.package import StatePackage
from pm.registry import get_package, tool_roots, walk
from pm.store import current_target
from pm.testenv import developer_tools

ROOT = Path(__file__).resolve().parents[2]


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def _matching_store(store: Path) -> Path:
    """A store recording every walked tool at the lock's pins with the env PM
    would record, each entry holding an executable binary, plus setup-hermes.sh's
    own uv copy. PM's entries are named so none is the setup path, which keeps
    the two uv checks apart. Returns setup-hermes.sh's uv path."""
    lock = Lockfile(ROOT / "pm" / "lock.json")
    names, target = lock.names(), current_target()
    facts = Facts(store / "facts.json")
    for package in walk(tool_roots(names) + developer_tools(names) + default_packages(names) + ["venv"]):
        if isinstance(package, StatePackage):
            continue
        version = lock.version(package.name)
        entry = store / f"pm-{package.name}-{version}-{target}"
        entry.mkdir(parents=True)
        binary = package.binary(entry, target)
        _executable(binary if binary is not None else entry / "chrome")  # chromium's is discovered
        facts.record(package.name, version, entry.name, package.env(entry, target), store,
                     target=target, artifacts=[a["sha256"] for a in lock.artifacts(package.name, target)])
    # setup-hermes.sh: entry="$store/uv-$uv_version-$os-$arch"
    return _executable(store / f"uv-{lock.version('uv')}-{'-'.join(target.split('-')[:2])}" / "uv")


def _shares(store: Path, *, hermes_home: Path | None = None, home: Path | None = None) -> bool:
    """Run the check as run_tests.sh does; by default from a borrowing HERMES_HOME beside STORE's root."""
    env = {k: v for k, v in os.environ.items() if k not in ("HERMES_HOME", "HERMES_DATA_DIR_SUFFIX")}
    if home is not None:
        env["HOME"] = str(home)
    else:
        env["HERMES_HOME"] = str(hermes_home or store.parent / "borrower")
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(ROOT / "scripts/tool_store_pins.py"), str(ROOT), str(store)],
        capture_output=True, text=True, timeout=60, check=False, env=env,
    )
    return result.returncode == 0


def _entry_binary(store: Path, name: str) -> Path:
    fact = Facts(store / "facts.json", strict=True).get(name) or {}
    binary = get_package(name).binary(store / fact["entry"], current_target())
    assert binary is not None
    return binary


def _stale_env(store: Path, name: str) -> None:
    data = json.loads((store / "facts.json").read_text())
    data["packages"][name]["env"] = {}
    (store / "facts.json").write_text(json.dumps(data))


BREAKAGES = {
    "setup_uv_missing": lambda store, uv: uv.unlink(),
    "setup_uv_not_executable": lambda store, uv: uv.chmod(0o644),
    "ripgrep_not_executable": lambda store, uv: _entry_binary(store, "ripgrep").chmod(0o644),
    "just_not_executable": lambda store, uv: _entry_binary(store, "just").chmod(0o644),
    "node_env_stale": lambda store, uv: _stale_env(store, "node"),
}


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("breakage", sorted(BREAKAGES))
def test_store_running_from_which_would_write_to_it_is_not_shared(tmp_path, breakage):
    store = tmp_path / "tools"
    uv = _matching_store(store)
    assert _shares(store), "control: a complete store at this checkout's pins must be shared"
    BREAKAGES[breakage](store, uv)
    before = sorted((p.relative_to(store), p.is_file() and p.read_bytes(), p.stat().st_mode)
                    for p in store.rglob("*"))
    assert not _shares(store)
    after = sorted((p.relative_to(store), p.is_file() and p.read_bytes(), p.stat().st_mode)
                   for p in store.rglob("*"))
    assert after == before, "the check must stay read-only (no heal, no re-record, no .corrupt)"


def _account_store(tmp_path: Path) -> Path:
    store = tmp_path / "account" / ".hermes" / "tools"
    _matching_store(store)
    return store


@pytest.mark.platforms("posix")
def test_a_custom_hermes_home_borrows_the_account_store(tmp_path):
    """A card's fake HERMES_HOME (outside the account root) used to download the whole toolchain."""
    store = _account_store(tmp_path)
    assert _shares(store, hermes_home=tmp_path / "card" / "hh")
    assert _shares(store, hermes_home=tmp_path / "card" / "hh" / "profiles" / "p"), "a profile of a borrower"


@pytest.mark.platforms("posix")
def test_a_faked_home_borrows_the_account_store(tmp_path):
    store = _account_store(tmp_path)
    assert _shares(store, home=tmp_path / "fake-home")


@pytest.mark.platforms("posix")
def test_the_stores_own_root_never_borrows_it(tmp_path):
    """The owner (or one of its profiles) provisions its own store: no borrowed sharing decision."""
    store = _account_store(tmp_path)
    assert not _shares(store, hermes_home=store.parent)
    assert not _shares(store, hermes_home=store.parent / "profiles" / "work")
    assert not _shares(store, home=tmp_path / "account"), "HERMES_HOME unset under the account HOME"


def _drop_fact(store: Path, name: str) -> None:
    data = json.loads((store / "facts.json").read_text())
    del data["packages"][name]
    (store / "facts.json").write_text(json.dumps(data))


def _snapshot(root: Path) -> list:
    return sorted((p.relative_to(root), p.is_file() and p.read_bytes(), p.stat().st_mode) for p in root.rglob("*"))


@pytest.mark.platforms("posix")
def test_an_optional_default_the_store_never_installed_is_declined_by_the_borrower(tmp_path):
    """worker-7: cua-driver fails to install headless (no libXi), so the account store has no
    fact for it. That used to refuse sharing, and every fake home downloaded the whole toolchain."""
    store = _account_store(tmp_path)
    lock = Lockfile(ROOT / "pm" / "lock.json")
    defaults = default_packages(lock.names(), declined_names=frozenset())
    if not defaults:
        pytest.skip("no optional default on this target")
    for name in defaults:
        _drop_fact(store, name)
    before = _snapshot(store)
    borrower = tmp_path / "card" / "hh"
    assert _shares(store, hermes_home=borrower)
    assert _snapshot(store) == before, "the decline must not touch the shared store"
    record = borrower / "installs" / install_key(ROOT) / DECLINED_FILENAME
    assert record.is_file(), "the borrower records its own decline, so its pm install skips the default"
    assert set(json.loads(record.read_text())["declined"]) >= set(defaults)
    assert _shares(store, hermes_home=borrower), "a rerun still shares"
    assert not (borrower / "tools").exists()


@pytest.mark.platforms("posix")
def test_an_optional_default_at_another_version_is_still_not_shared(tmp_path):
    """Only a default absent at every version is declined; one recorded at other pins would be re-staged."""
    store = _account_store(tmp_path)
    defaults = default_packages(Lockfile(ROOT / "pm" / "lock.json").names(), declined_names=frozenset())
    if not defaults:
        pytest.skip("no optional default on this target")
    data = json.loads((store / "facts.json").read_text())
    data["packages"][defaults[0]]["version"] = "0.0.0-other"
    (store / "facts.json").write_text(json.dumps(data))
    borrower = tmp_path / "card" / "hh"
    assert not _shares(store, hermes_home=borrower)
    assert not borrower.exists(), "a refusal writes nothing, not even the borrower's decline"


@pytest.mark.platforms("posix")
def test_a_root_with_its_own_store_keeps_it(tmp_path):
    store = _account_store(tmp_path)
    borrower = tmp_path / "card" / "hh"
    assert _shares(store, hermes_home=borrower), "control"
    (borrower / "tools").mkdir(parents=True)
    (borrower / "tools" / "facts.json").write_text('{"packages": {}, "schema": 1}')
    assert not _shares(store, hermes_home=borrower)


def _entry_points_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A checkout whose scripts, pm and sources are this one's, but whose setup-hermes.sh only
    records the HERMES_RUNTIME_DIR / UV_PYTHON_INSTALL_DIR it was provisioned with (and fails,
    so nothing downloads). Returns the repo and that record."""
    repo, record = tmp_path / "repo", tmp_path / "provisioned-with"
    (repo / "scripts").mkdir(parents=True)
    for entry in ROOT.iterdir():
        if entry.name not in {"scripts", "setup-hermes.sh", ".venv", "venv", ".git"}:
            (repo / entry.name).symlink_to(entry)
    for entry in (ROOT / "scripts").iterdir():
        (repo / "scripts" / entry.name).symlink_to(entry)
    setup = repo / "setup-hermes.sh"
    setup.write_text(f'#!/bin/sh\nprintf "%s\\n%s\\n" "${{HERMES_RUNTIME_DIR-}}" "${{UV_PYTHON_INSTALL_DIR-}}" > "{record}"\nexit 1\n')
    setup.chmod(0o755)
    # hermes_bootstrap_python's first candidate: this interpreter, with its own site.
    _executable(repo / ".venv" / "bin" / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    return repo, record


def _provision_from_faked_home(tmp_path: Path, repo: Path, entry: list[str]) -> subprocess.CompletedProcess:
    """Run ENTRY from REPO the way a review card does: env cut to PATH, a fresh fake HOME, and an
    OS account (getent) whose home is tmp_path/account."""
    stubs = tmp_path / "stubs"
    _executable(stubs / "getent").write_text(f'#!/bin/sh\necho "me:x:{os.getuid()}:{os.getgid()}::{tmp_path / "account"}:/bin/sh"\n')
    env = {"PATH": f"{stubs}:{os.environ['PATH']}", "HOME": str(tmp_path / "fake-home")}
    return subprocess.run(["bash", *entry], cwd=repo, env=env, capture_output=True, text=True,
                          timeout=120, check=False)


ENTRY_POINTS = {
    "run-in-hermes-env": ["scripts/run-in-hermes-env", "true"],
    "run_tests.sh": ["scripts/run_tests.sh", "tests/scripts/test_tool_store_pins.py"],
}


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("entry", sorted(ENTRY_POINTS))
def test_every_entry_point_provisions_a_faked_home_from_the_account_store(tmp_path, entry):
    """worker-7: review cards call run-in-hermes-env directly from a scratch HOME, and only
    run_tests.sh borrowed, so each card staged a private ~1.4 GB tools/ (python, node, chromium)."""
    store = _account_store(tmp_path)
    py_minor = ".".join(Lockfile(ROOT / "pm" / "lock.json").version("python").split("+")[0].split(".")[:2])
    uv_pythons = tmp_path / "account" / ".local" / "share" / "uv" / "python"
    (uv_pythons / f"cpython-{py_minor}.0-linux-x86_64-gnu").mkdir(parents=True)
    repo, record = _entry_points_repo(tmp_path)
    result = _provision_from_faked_home(tmp_path, repo, ENTRY_POINTS[entry])
    assert record.is_file(), result.stderr
    assert record.read_text().splitlines() == [str(store), str(uv_pythons)], result.stderr
    assert not (tmp_path / "fake-home" / ".hermes" / "tools").exists()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("entry", sorted(ENTRY_POINTS))
def test_no_entry_point_borrows_an_account_store_at_other_pins(tmp_path, entry):
    store = _account_store(tmp_path)
    _drop_fact(store, "node")
    repo, record = _entry_points_repo(tmp_path)
    result = _provision_from_faked_home(tmp_path, repo, ENTRY_POINTS[entry])
    assert record.is_file(), result.stderr
    assert record.read_text().splitlines() == ["", ""], result.stderr
