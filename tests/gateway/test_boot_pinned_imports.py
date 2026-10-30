"""A long-running process must not mix module revisions after an in-place core update.

Regression for t_01a1824adc287c5a: a Desktop ``hermes serve`` booted at 2aa3397018 had the old
``agent.model_routing_managed`` loaded; ``hermes update`` moved the tree to a commit that added
``ManagedPinRejectedError``; the first-time lazy import of the NEW ``kanban_model_assignment``
then failed with ``cannot import name 'ManagedPinRejectedError'``.
"""

from __future__ import annotations

import importlib
import inspect
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from gateway import boot_pinned_imports
from gateway.boot_pinned_imports import BootPinnedFinder
from hermes_cli.main import _read_git_revision_fingerprint

_GIT = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *_GIT, *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, files: dict[str, str | None], msg: str) -> str:
    for rel, text in files.items():
        path = repo / rel
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


OLD_ROUTING = "class ModelRoutingError(Exception):\n    pass\n"
NEW_ROUTING = OLD_ROUTING + "\n\nclass ManagedPinRejectedError(ModelRoutingError):\n    pass\n"
OLD_ASSIGN = ("def validate():\n"
              "    from {pkg}.routing import ModelRoutingError\n"
              "    return 'old:' + ModelRoutingError.__name__\n")
NEW_ASSIGN = ("def validate():\n"
              "    from {pkg}.routing import ManagedPinRejectedError\n"
              "    return 'new:' + ManagedPinRejectedError.__name__\n")


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A git checkout on sys.path holding a uniquely named package at a boot commit."""
    pkg = f"bootpin_{uuid.uuid4().hex[:10]}"
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "-q")
    boot = _commit(repo, {f"{pkg}/__init__.py": "",
                          f"{pkg}/routing.py": OLD_ROUTING,
                          f"{pkg}/assign.py": OLD_ASSIGN.format(pkg=pkg),
                          f"{pkg}/gone.py": "VALUE = 'boot'\n"}, "boot")
    monkeypatch.syspath_prepend(str(repo))
    importlib.invalidate_caches()
    finders: list = []
    yield pkg, repo, boot, finders
    for finder in finders:
        if finder in sys.meta_path:
            sys.meta_path.remove(finder)
    for name in [n for n in sys.modules if n == pkg or n.startswith(pkg + ".")]:
        del sys.modules[name]


def _finder(repo: Path, boot: str, finders: list) -> BootPinnedFinder:
    finder = BootPinnedFinder(str(repo), boot, lambda: boot_pinned_imports._sha_of(
        _read_git_revision_fingerprint(repo)))
    sys.meta_path.insert(0, finder)
    finders.append(finder)
    return finder


def _core_update(repo: Path, pkg: str) -> None:
    """What ``hermes update`` does to the running tree: the routing module gains a class and the
    not-yet-imported module starts importing it."""
    _commit(repo, {f"{pkg}/routing.py": NEW_ROUTING,
                   f"{pkg}/assign.py": NEW_ASSIGN.format(pkg=pkg),
                   f"{pkg}/gone.py": None,
                   f"{pkg}/added.py": "VALUE = 'added'\n"}, "advance")
    importlib.invalidate_caches()


def test_unguarded_process_mixes_revisions_and_raises_the_reported_import_error(tree):
    """Control: without the finder the scenario reproduces the incident."""
    pkg, repo, _boot, _ = tree
    importlib.import_module(f"{pkg}.routing")  # loaded at boot
    _core_update(repo, pkg)
    assign = importlib.import_module(f"{pkg}.assign")  # first-time lazy import: NEW file
    with pytest.raises(ImportError, match="cannot import name 'ManagedPinRejectedError'"):
        assign.validate()


def test_lazy_import_after_update_loads_the_boot_revision_instead_of_raising(tree):
    pkg, repo, boot, finders = tree
    importlib.import_module(f"{pkg}.routing")  # loaded at boot
    finder = _finder(repo, boot, finders)
    _core_update(repo, pkg)

    assign = importlib.import_module(f"{pkg}.assign")

    assert assign.validate() == "old:ModelRoutingError"  # one consistent revision, no ImportError
    assert finder.pinned == [f"{pkg}.assign"]
    assert assign.__file__ == str(repo / pkg / "assign.py")
    assert "ModelRoutingError" in inspect.getsource(assign.validate)  # source shown = code run


def test_a_module_deleted_by_the_update_still_imports_from_the_boot_commit(tree):
    pkg, repo, boot, finders = tree
    importlib.import_module(pkg)
    _finder(repo, boot, finders)
    _core_update(repo, pkg)
    assert not (repo / pkg / "gone.py").exists()
    assert importlib.import_module(f"{pkg}.gone").VALUE == "boot"


def test_a_module_new_since_boot_falls_back_to_disk(tree):
    pkg, repo, boot, finders = tree
    importlib.import_module(pkg)
    finder = _finder(repo, boot, finders)
    _core_update(repo, pkg)
    assert importlib.import_module(f"{pkg}.added").VALUE == "added"
    assert f"{pkg}.added" not in finder.pinned


def test_no_update_means_normal_disk_imports(tree):
    """Before the checkout moves the finder stays out of the way (uncommitted edits load)."""
    pkg, repo, boot, finders = tree
    finder = _finder(repo, boot, finders)
    (repo / pkg / "assign.py").write_text("def validate():\n    return 'dirty'\n", encoding="utf-8")
    assert importlib.import_module(f"{pkg}.assign").validate() == "dirty"
    assert finder.pinned == [] and finder.is_skewed() is False


def test_an_unreadable_boot_commit_falls_back_to_the_old_behaviour(tree):
    pkg, repo, _boot, finders = tree
    importlib.import_module(pkg)
    finder = _finder(repo, "f" * 40, finders)  # not an object in this repo
    _core_update(repo, pkg)
    assert importlib.import_module(f"{pkg}.added").VALUE == "added"
    assert finder.pinned == []


def test_skew_is_latched_even_if_the_tree_returns(tree):
    pkg, repo, boot, finders = tree
    finder = _finder(repo, boot, finders)
    _core_update(repo, pkg)
    assert finder.is_skewed() is True
    _git(repo, "reset", "-q", "--hard", boot)
    assert finder.is_skewed() is True


def test_install_is_a_noop_under_pytest_and_when_disabled(tree, monkeypatch):
    _pkg, repo, boot, _ = tree
    monkeypatch.setattr(boot_pinned_imports, "_installed", None)
    monkeypatch.delenv("HERMES_BOOT_PINNED_IMPORTS", raising=False)
    assert boot_pinned_imports.install(repo, f"git:HEAD:{boot}", lambda: None) is None
    monkeypatch.setenv("HERMES_BOOT_PINNED_IMPORTS", "0")
    assert boot_pinned_imports.install(repo, f"git:HEAD:{boot}", lambda: None) is None
    monkeypatch.setenv("HERMES_BOOT_PINNED_IMPORTS", "1")
    assert boot_pinned_imports.install(repo, "git:refs/heads/main:unresolved", lambda: None) is None


def test_code_skew_wiring_installs_a_finder_bound_to_the_boot_commit(tree, monkeypatch):
    """The serve lifespan / gateway entry point: ``record_boot_fingerprint`` then
    ``install_boot_pinned_imports`` pins lazy imports once the checkout moves."""
    from gateway import code_skew

    pkg, repo, boot, finders = tree
    importlib.import_module(f"{pkg}.routing")
    monkeypatch.setenv("HERMES_BOOT_PINNED_IMPORTS", "1")
    monkeypatch.setattr(boot_pinned_imports, "_installed", None)
    monkeypatch.setattr(code_skew, "_PROJECT_ROOT", repo)
    monkeypatch.setattr(code_skew, "_boot_fingerprint", None)
    code_skew.record_boot_fingerprint()
    code_skew.install_boot_pinned_imports()
    finder = boot_pinned_imports._installed
    assert finder is not None and sys.meta_path[0] is finder
    finders.append(finder)
    assert finder.boot_sha == boot

    _core_update(repo, pkg)
    assert importlib.import_module(f"{pkg}.assign").validate() == "old:ModelRoutingError"


def test_an_update_between_process_start_and_gateway_bootstrap_still_pins_the_boot_revision(
        tree, monkeypatch):
    """Regression for agent-529f2244ac (mini, 2026-10-29): ``hermes_cli.main`` imported the old
    ``process_identity`` at process start, ``hermes update`` moved the checkout before
    ``start_gateway`` recorded the boot commit, so the finder took the NEW commit as "boot", saw no
    skew, and the lazy ``code_kernel`` import loaded new code that wants ``spawn_owned``.

    Real order: ``hermes_bootstrap`` reads the revision before any other in-tree import, eager
    imports follow, the update lands, then ``start_gateway`` records + installs."""
    from gateway import code_skew
    from hermes_cli import git_fingerprint

    pkg, repo, boot, finders = tree
    monkeypatch.setenv("HERMES_BOOT_PINNED_IMPORTS", "1")
    monkeypatch.setattr(boot_pinned_imports, "_installed", None)
    monkeypatch.setattr(code_skew, "_PROJECT_ROOT", repo)
    monkeypatch.setattr(code_skew, "_boot_fingerprint", None)
    monkeypatch.setattr(git_fingerprint, "_process_boot", {})

    git_fingerprint.record_process_boot_fingerprint(repo)  # hermes_bootstrap, first thing
    importlib.import_module(f"{pkg}.routing")  # hermes_cli.main's eager imports (old code)
    _core_update(repo, pkg)  # `hermes update` lands while the process is still starting
    code_skew.record_boot_fingerprint()  # start_gateway / web_server_app lifespan
    code_skew.install_boot_pinned_imports()
    finder = boot_pinned_imports._installed
    assert finder is not None
    finders.append(finder)

    assert finder.boot_sha == boot  # the commit the process actually started on
    assert importlib.import_module(f"{pkg}.assign").validate() == "old:ModelRoutingError"
    assert finder.pinned == [f"{pkg}.assign"]


def test_hermes_bootstrap_records_the_boot_revision_before_hermes_cli_main_loads():
    """Entry-point wiring: importing ``hermes_bootstrap`` (the first import of every entry point)
    leaves the checkout's revision recorded, before ``hermes_cli.main`` and its eager imports run."""
    root = Path(__file__).resolve().parents[2]
    probe = ("import sys, hermes_bootstrap\n"
             "import hermes_cli.git_fingerprint as g\n"
             "print('main-loaded' if 'hermes_cli.main' in sys.modules else 'main-not-loaded')\n"
             f"print(g.process_boot_fingerprint({str(root)!r}))\n"
             # Read the reference in the child too: this worktree's .git lives in the real hermes
             # home, which the in-process home-I/O guard refuses to touch.
             f"from pathlib import Path\nprint(g.read_git_revision_fingerprint(Path({str(root)!r})))\n")
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    done = subprocess.run([sys.executable, "-c", probe], cwd=root, env=env, capture_output=True, check=False,
                          text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    lines = done.stdout.strip().splitlines()
    assert lines[-3] == "main-not-loaded"
    assert lines[-2].startswith("git:") and lines[-2] == lines[-1]
