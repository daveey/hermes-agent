"""A launch under a data root that does not own the checkout leaves the checkout to its owner (#123238).

Layout in every test: the user's default root is ``<tmp>/.hermes`` (``Path.home`` is ``<tmp>``) and
the checkout is ``<tmp>/.hermes/hermes-agent``, the ``install.sh`` layout. A root "has state" for the
checkout when ``<root>/installs/<install_key>/facts.json`` exists -- the record a committed
dependency sync leaves.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
import sys

import pytest

from hermes_cli import venv_sync
from pm.environments import install_key, install_state_dir, owning_home_root, store_root

# Every lane, Windows included: the owner check resolves the platform default root, which differs on Windows, so the
# marked-OS lanes (scripts/ci/list_os_marked_tests.py) must collect this file too.
pytestmark = pytest.mark.platforms("any")


@pytest.fixture(autouse=True)
def _no_tool_downloads(monkeypatch):
    import pm.client

    monkeypatch.setattr(pm.client, "ensure_tools_for_sync", lambda: None)


@pytest.fixture
def completion_tail(monkeypatch):
    """Record the completion child instead of running it (the tail spawns via update_custody.run)."""
    from hermes_cli import update_custody

    spawned: list = []
    monkeypatch.setattr(update_custody, "run", lambda command, **kw: spawned.append(command)
                        or subprocess.CompletedProcess(command, 0))
    return spawned


def _checkout(tmp_path, monkeypatch, *, parent: Path | None = None) -> Path:
    import hermes_constants

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # The layout's default root is ``<tmp>/.hermes`` on every OS. On Windows the platform default is
    # ``%LOCALAPPDATA%\hermes`` (``Path.home`` only backs an unset ``LOCALAPPDATA``), and no
    # ``LOCALAPPDATA`` value spells ``<tmp>/.hermes``, so pin the default itself.
    monkeypatch.setattr(hermes_constants, "_get_platform_default_hermes_home", lambda: tmp_path / ".hermes")
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    root = (parent or tmp_path / ".hermes") / "hermes-agent"
    root.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='example'\n", encoding="utf-8")
    (root / "install-stamp.json").write_text(json.dumps({"updateMechanism": "self", "commit": "abc"}), encoding="utf-8")
    return root


def _state(data_root: Path, checkout: Path) -> Path:
    facts = data_root / "installs" / install_key(checkout) / "facts.json"
    facts.parent.mkdir(parents=True, exist_ok=True)
    facts.write_text(json.dumps({"packages": {"venv": {"stamp": "complete", "extras": ["all"]}}}), encoding="utf-8")
    return facts


def _home(monkeypatch, path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(path))
    return path


def test_a_fresh_install_and_a_custom_root_keep_their_own_store(tmp_path, monkeypatch):
    # Nobody has state yet: the launching root is about to become the owner.
    root = _checkout(tmp_path, monkeypatch, parent=tmp_path / "data" / "h")
    _home(monkeypatch, tmp_path / "data" / "h")
    assert owning_home_root(root) is None
    assert store_root(root) == tmp_path / "data" / "h" / "tools"

    # `install.sh --hermes-home /data/h` owns its tree even if the default root once
    # borrowed it and kept state of its own.
    _state(tmp_path / "data" / "h", root)
    _state(tmp_path / ".hermes", root)
    assert owning_home_root(root) is None
    assert store_root(root) == tmp_path / "data" / "h" / "tools"


def test_a_borrowers_own_dependency_state_never_makes_it_the_owner(tmp_path, monkeypatch):
    """`install.sh --hermes-home <custom>` owns the checkout. The default root launching it borrows,
    and still borrows after its own dependency sync leaves state for the checkout."""
    custom = tmp_path / "data" / "h"
    root = _checkout(tmp_path, monkeypatch, parent=custom)
    _state(custom, root)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert owning_home_root(root) == custom

    _state(tmp_path / ".hermes", root)  # the borrowing launch's own sync
    assert owning_home_root(root) == custom
    assert store_root(root) == custom / "tools"


def test_a_borrower_holding_the_checkout_never_becomes_its_owner(tmp_path, monkeypatch):
    """`install.sh --dir <dir>` puts the default root's checkout in <dir>. Launching it with
    HERMES_HOME=<dir> borrows, and still borrows after that root's own sync leaves state --
    even when an explicit sync points HERMES_RUNTIME_DIR at <dir>'s own store."""
    import os

    import pm
    from hermes_cli import _launchers

    default = tmp_path / ".hermes"
    holder = tmp_path / "data" / "dir"
    root = _checkout(tmp_path, monkeypatch, parent=holder)
    entry = default / "tools" / "python-owner"
    python = entry / ("python.exe" if os.name == "nt" else "bin/python3")
    python.parent.mkdir(parents=True)
    python.touch()
    (default / "tools" / "facts.json").write_text(json.dumps(
        {"schema": 1, "packages": {"python": {"version": "fixture", "entry": entry.name}}}), encoding="utf-8")
    _state(default, root)
    _home(monkeypatch, default)
    local = root / ".hermes" / "bin"
    published = {path: Path(path).read_bytes() for path in _launchers.ensure_install_launchers(root, local)}
    assert published

    _home(monkeypatch, holder)
    _state(holder, root)  # the borrowing launch's own sync
    assert owning_home_root(root) == default
    assert store_root(root) == default / "tools"
    monkeypatch.setattr(pm, "venv_is_current", lambda **kwargs: True)
    assert venv_sync.prepare_launch(root, []) == python
    assert {path: Path(path).read_bytes() for path in published} == published

    # An explicit sync publishes outside prepare_launch, and <dir>/tools matches the owner layout.
    borrowed = holder / "tools" / "python-holder"
    (borrowed / ("python.exe" if os.name == "nt" else "bin/python3")).parent.mkdir(parents=True)
    (borrowed / ("python.exe" if os.name == "nt" else "bin/python3")).touch()
    (holder / "tools" / "facts.json").write_text(json.dumps(
        {"schema": 1, "packages": {"python": {"version": "fixture", "entry": borrowed.name}}}), encoding="utf-8")
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(holder / "tools"))
    monkeypatch.setattr(_launchers, "expose_cli", lambda *args, **kwargs: {"ok": True})
    assert venv_sync.sync(root)["ok"]
    assert {path: Path(path).read_bytes() for path in published} == published
    monkeypatch.delenv("HERMES_RUNTIME_DIR")
    assert owning_home_root(root) == default


def test_a_borrowing_launch_syncs_its_own_dependencies_and_nothing_of_the_checkout(
    tmp_path, monkeypatch, completion_tail
):
    """The first launch under a temporary home: its own dependency generation, but no tail (products,
    maintenance, install stamp), no launcher publication and no owner marker touched."""
    import pm
    from hermes_cli import _launchers

    root = _checkout(tmp_path, monkeypatch)
    _state(tmp_path / ".hermes", root)
    _home(monkeypatch, tmp_path / "tmp" / "hermes-flash-compat-x" / "a")
    # The owner's store, so no tool store is downloaded into the temporary home.
    assert owning_home_root(root) == tmp_path / ".hermes"
    assert store_root(root) == tmp_path / ".hermes" / "tools"
    stamp = (root / "install-stamp.json").read_bytes()
    owner_marker = root / ".update-incomplete"
    owner_marker.write_text("owner's update\n", encoding="utf-8")
    borrowed_facts = install_state_dir(root) / "facts.json"
    syncs = []

    def sync(extras=None, **kwargs):
        syncs.append(extras)
        borrowed_facts.parent.mkdir(parents=True, exist_ok=True)
        borrowed_facts.write_text("{}", encoding="utf-8")

    python = tmp_path / ".hermes" / "tools" / "python" / "bin" / "python3"
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: borrowed_facts.is_file())
    monkeypatch.setattr(pm, "sync_venv", sync)
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda _: python)
    monkeypatch.setattr(venv_sync, "publish_launchers", lambda *a, **kw: pytest.fail("published the owner's launchers"))

    assert venv_sync.prepare_launch(root, ["kanban", "init"]) == python  # relaunch into the synced env
    assert len(syncs) == 1
    assert completion_tail == [], "a borrowing launch ran the checkout's update tail"
    assert not venv_sync.completion_pending_path(root).exists(), "a tail was armed for a borrowing root"
    assert owner_marker.is_file(), "the owner's update marker was removed"
    assert (root / "install-stamp.json").read_bytes() == stamp

    # Its relaunch finds everything current and does nothing further.
    monkeypatch.setattr(sys, "executable", str(python))
    assert venv_sync.prepare_launch(root, ["kanban", "init"]) is None
    assert len(syncs) == 1 and completion_tail == []


def test_the_owner_still_owes_and_runs_its_tail(tmp_path, monkeypatch, completion_tail):
    """Unchanged for the owner: a stale environment syncs, arms and runs the tail."""
    import pm
    from hermes_cli import _launchers

    root = _checkout(tmp_path, monkeypatch)
    facts = _state(tmp_path / ".hermes", root)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    facts.unlink()
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: facts.is_file())
    monkeypatch.setattr(pm, "sync_venv", lambda *a, **kw: facts.write_text("{}", encoding="utf-8"))
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda _: Path(sys.executable))
    published = []
    monkeypatch.setattr(venv_sync, "publish_launchers", lambda r, **kw: published.append(r))

    assert venv_sync.prepare_launch(root, []) == Path(sys.executable)
    assert len(completion_tail) == 1
    assert published == [root]


def test_borrowed_launch_already_on_the_store_interpreter_does_not_reexec(tmp_path, monkeypatch):
    """A fresh borrowed home syncs, then stays on the store interpreter.

    Returning that interpreter anyway re-execs. A script that mints a new HERMES_HOME on
    each start would sync and re-exec forever once the import no longer dies first.
    """
    import pm
    from hermes_cli import _launchers

    root = _checkout(tmp_path, monkeypatch)
    _state(tmp_path / ".hermes", root)
    _home(monkeypatch, tmp_path / "tmp" / "borrowed")
    borrowed_facts = install_state_dir(root) / "facts.json"
    python = tmp_path / ".hermes" / "tools" / "python" / "bin" / "python3"
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: borrowed_facts.is_file())
    monkeypatch.setattr(pm, "sync_venv", lambda *a, **kw: (
        borrowed_facts.parent.mkdir(parents=True, exist_ok=True)
        or borrowed_facts.write_text("{}", encoding="utf-8")))
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda _: python)

    assert venv_sync.prepare_launch(root, ["kanban", "init"]) is None
    assert borrowed_facts.is_file()


def test_temp_home_relaunch_imports_hermes_state_without_dropping_venv_deps(tmp_path):
    """A venv-launched script under a temp HERMES_HOME re-enters on a bare interpreter.

    That interpreter has none of the launching venv's packages. The script imports
    hermes_state (which needs ruamel) before any entry point would import
    hermes_bootstrap. The re-entry must load this checkout's bootstrap first so those
    deps are selected, not dropped (# borrowed-home relaunch).
    """
    checkout = tmp_path / "checkout"
    deps = tmp_path / "deps"
    (deps / "ruamel").mkdir(parents=True)
    (deps / "ruamel" / "__init__.py").write_text("", encoding="utf-8")
    (deps / "ruamel" / "yaml.py").write_text("YAML = object\n", encoding="utf-8")
    checkout.mkdir()
    # Stands in for activate_dependencies: the committed env, not the bare interpreter.
    (checkout / "hermes_bootstrap.py").write_text(
        "import site\n" + f"site.addsitedir({str(deps)!r})\n",
        encoding="utf-8",
    )
    (checkout / "hermes_state.py").write_text(
        "from ruamel.yaml import YAML\nclass SessionDB:\n    pass\n",
        encoding="utf-8",
    )
    script = tmp_path / "launch.py"
    script.write_text(
        "import os, sys, tempfile\n"
        "home = tempfile.mkdtemp(prefix='borrowed-home-')\n"
        "os.environ['HERMES_HOME'] = home\n"
        "os.environ.pop('HERMES_KANBAN_DB', None)\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from hermes_state import SessionDB\n"
        "print('ok')\n"
        "print(home)\n",
        encoding="utf-8",
    )
    bare = tmp_path / "bare-venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(bare)], check=True, timeout=60)
    python = bare / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    argv = [str(script), str(checkout)]
    # Same shape as a borrowed relaunch: store interpreter, -I, run_path of the script.
    command = venv_sync.relaunch_command(python, checkout, argv, [str(python), *argv], None)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, env=env, check=False)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "ok"
    assert lines[1].startswith(str(tmp_path)) or "borrowed-home-" in lines[1]
