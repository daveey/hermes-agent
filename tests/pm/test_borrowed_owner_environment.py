"""A root that borrows a checkout runs the owner's committed generation instead of building its own.

Layout: the owner is ``<tmp>/owner`` and the checkout ``<tmp>/owner/hermes-agent`` (the root the
checkout sits in owns it, ``owning_home_root``); the borrower is a fresh ``HERMES_HOME``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pm import environments
from pm.environments import borrowed_venv, install_key, install_state_dir, owning_home_root

posix = pytest.mark.platforms("linux", "macos")


def _checkout(tmp_path: Path, monkeypatch) -> Path:
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "_get_platform_default_hermes_home", lambda: tmp_path / "default")
    root = tmp_path / "owner" / "hermes-agent"
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='example'\n[project.optional-dependencies]\nall=[]\n",
                                         encoding="utf-8")
    return root


def _owner_generation(owner: Path, root: Path, *, stamp: str = "owner-stamp", extras=("all",),
                      environment: Path | None = None) -> Path:
    state = owner / "installs" / install_key(root)
    venv = state / "environments" / "gen" / "venv"
    environments.site_packages(venv).mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    (venv.parent / ".lease-managed").write_text("", encoding="utf-8")
    (environments.site_packages(venv) / "owner_only.py").write_text("", encoding="utf-8")
    (state / "facts.json").write_text(json.dumps({"schema": 1, "packages": {"venv": {
        "environment": str(environment or venv), "extras": list(extras), "stamp": stamp}}}), encoding="utf-8")
    return venv


def _borrow(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "scratch" / "borrower"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_a_borrower_resolves_the_owners_generation_read_only(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch)
    venv = _owner_generation(tmp_path / "owner", root)
    facts = (tmp_path / "owner" / "installs" / install_key(root) / "facts.json").read_bytes()
    _borrow(tmp_path, monkeypatch)
    assert owning_home_root(root) == tmp_path / "owner"
    assert borrowed_venv(root) == venv.resolve()
    assert not install_state_dir(root).exists()
    assert (tmp_path / "owner" / "installs" / install_key(root) / "facts.json").read_bytes() == facts

    # The owner itself borrows nothing.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "owner"))
    assert borrowed_venv(root) is None


def test_an_owner_record_outside_its_own_state_is_not_borrowed(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere" / "venv"
    elsewhere.mkdir(parents=True)
    (elsewhere / "pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    _owner_generation(tmp_path / "owner", root, environment=elsewhere)
    _borrow(tmp_path, monkeypatch)
    assert borrowed_venv(root) is None


@pytest.fixture
def stamps(monkeypatch):
    """Stand in for the lock/layout digest: the stamp names the enabled extras and plugin members."""
    from pm import extras, install, packages

    members: list[str] = []
    monkeypatch.setattr(packages.Venv, "expected_stamp",
                        lambda self, enabled, **kw: "stamp:" + ",".join(sorted(enabled)) + "|" + ",".join(members))
    monkeypatch.setattr(extras, "first_generation_selection", lambda root: ["all"])
    monkeypatch.setattr(extras, "pre_pm_baseline", lambda root: [])
    return members


def test_the_owners_generation_serves_a_borrower_with_the_same_request(tmp_path, monkeypatch, stamps):
    from pm.install import borrowed_venv_is_current

    root = _checkout(tmp_path, monkeypatch)
    _owner_generation(tmp_path / "owner", root, stamp="stamp:all|")
    _borrow(tmp_path, monkeypatch)
    assert borrowed_venv_is_current(project_root=root)
    assert not install_state_dir(root).exists(), "the check wrote borrower state"

    # A borrower enabling other plugins keeps its own environment.
    stamps.append("plugin-x")
    assert not borrowed_venv_is_current(project_root=root)


def test_a_borrower_with_a_selection_of_its_own_keeps_it(tmp_path, monkeypatch, stamps):
    from pm.install import borrowed_venv_is_current

    root = _checkout(tmp_path, monkeypatch)
    _owner_generation(tmp_path / "owner", root, stamp="stamp:all|")
    _borrow(tmp_path, monkeypatch)
    own = install_state_dir(root) / "facts.json"
    own.parent.mkdir(parents=True)
    own.write_text(json.dumps({"schema": 1, "packages": {"venv": {"environment": "x", "stamp": "s"}}}),
                   encoding="utf-8")
    assert not borrowed_venv_is_current(project_root=root)


def test_an_owner_generation_missing_requested_extras_does_not_serve(tmp_path, monkeypatch, stamps):
    from pm import extras
    from pm.install import borrowed_venv_is_current

    root = _checkout(tmp_path, monkeypatch)
    _owner_generation(tmp_path / "owner", root, stamp="stamp:all|", extras=())
    _borrow(tmp_path, monkeypatch)
    monkeypatch.setattr(extras, "first_generation_selection", lambda root: ["all", "voice"])
    assert not borrowed_venv_is_current(project_root=root)


@posix
@pytest.mark.parametrize("owned", [True, False])
def test_a_store_python_borrower_activates_the_owners_generation(tmp_path, monkeypatch, owned):
    """`HERMES_HOME=<tmp> hermes --version`: nothing committed under the borrower, so PM's store Python
    runs the owner's generation (leased in place) instead of refusing; with no owner it still refuses."""
    root = _checkout(tmp_path, monkeypatch)
    venv = _owner_generation(tmp_path / "owner", root) if owned else None
    home = _borrow(tmp_path, monkeypatch)
    base_prefix = Path(sys.base_prefix).resolve()
    repo = Path(__file__).resolve().parents[2]
    code = (
        "import sys, importlib.util; from pathlib import Path; sys.path.insert(0, sys.argv[1])\n"
        "import hermes_constants; hermes_constants._get_platform_default_hermes_home = lambda: Path(sys.argv[3])\n"
        "from pm.environments import activate_dependencies\n"
        "try:\n    activate_dependencies(Path(sys.argv[2]))\n"
        "except RuntimeError as exc:\n    print('refused:', exc); raise SystemExit(0)\n"
        "import os\n"
        "print('owner importable:', importlib.util.find_spec('owner_only') is not None)\n"
        "print('leases:', len(os.listdir(sys.argv[4])) if os.path.isdir(sys.argv[4]) else 0)\n"
    )
    env = {**os.environ, "HERMES_HOME": str(home), "HERMES_RUNTIME_DIR": str(base_prefix.parent)}
    env.pop("PYTHONPATH", None)
    leases = str(venv.parent / ".leases") if venv else str(tmp_path / "none")
    result = subprocess.run([getattr(sys, "_base_executable", sys.executable), "-I", "-c", code, str(repo),
                             str(root), str(tmp_path / "default"), leases],
                            env=env, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    if owned:
        assert result.stdout.splitlines() == ["owner importable: True", "leases: 1"], result.stdout
        assert not (home / "installs").exists(), "the borrower built state of its own"
    else:
        assert result.stdout.startswith("refused: no dependency environment is committed"), result.stdout


@posix
def test_a_served_borrowers_relaunch_runs_the_owners_generation_without_rechecking(tmp_path, monkeypatch):
    """The store interpreter a served borrowing launch relaunches into (same pid, the parent's
    verdict handed over in the environment) asks no probe at all -- neither currency nor the owner
    check -- records the root as current for check_runtime, and boots the owner's generation."""
    root = _checkout(tmp_path, monkeypatch)
    (root / ".git").mkdir()
    (root / "install-stamp.json").write_text(json.dumps({"updateMechanism": "self", "commit": "abc"}),
                                              encoding="utf-8")
    venv = _owner_generation(tmp_path / "owner", root)
    home = _borrow(tmp_path, monkeypatch)
    base_prefix = Path(sys.base_prefix).resolve()
    repo = Path(__file__).resolve().parents[2]
    code = (
        "import importlib.util, os, sys; from pathlib import Path; sys.path.insert(0, sys.argv[1])\n"
        "import hermes_constants; hermes_constants._get_platform_default_hermes_home = lambda: Path(sys.argv[3])\n"
        "root = Path(sys.argv[2]).resolve()\n"
        "from hermes_cli import _launchers, launch_probe, venv_sync\n"
        "probes = []\n"
        "launch_probe.probe = lambda root, operation='currency', **kw: probes.append(operation)\n"
        "venv_sync._launch_currency = lambda root, command: probes.append('currency')\n"
        "_launchers.resolve_store_python = lambda *a, **kw: Path(sys.executable)\n"
        "os.environ[venv_sync.LAUNCH_HANDOFF_ENV] = f'current:{os.getpid()}:{root}'\n"
        "print('relaunch:', venv_sync.prepare_launch(root, ['config', 'path']))\n"
        "print('probes:', probes)\n"
        "print('current:', root in venv_sync._current_roots)\n"
        "from pm.environments import activate_dependencies\n"
        "activate_dependencies(root)\n"
        "print('owner importable:', importlib.util.find_spec('owner_only') is not None)\n"
    )
    env = {**os.environ, "HERMES_HOME": str(home), "HERMES_RUNTIME_DIR": str(base_prefix.parent)}
    for name in ("PYTHONPATH", "HERMES_DISABLE_LAZY_INSTALLS", "HERMES_LAUNCH_CURRENT_HANDOFF"):
        env.pop(name, None)
    result = subprocess.run([getattr(sys, "_base_executable", sys.executable), "-I", "-c", code, str(repo),
                             str(root), str(tmp_path / "default")],
                            env=env, capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["relaunch: None", "probes: []", "current: True",
                                          "owner importable: True"], result.stdout + result.stderr
    assert not (home / "installs").exists(), "the borrower built state of its own"
    assert venv.is_dir()
