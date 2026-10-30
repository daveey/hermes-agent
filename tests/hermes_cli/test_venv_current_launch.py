"""prepare_launch() recognizes the store interpreter it would re-exec into (#122513).

PM spells the store Python through HERMES_HOME, which may contain '..', while
the OS reports ``sys.executable`` normalized. The identity check must be
lexical: comparing the raw spellings relaunched every child forever, and
following symlinks would treat a venv interpreter as the store interpreter.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hermes_cli import venv_sync


@pytest.fixture
def launch(tmp_path, monkeypatch):
    import pm
    from hermes_cli import _launchers

    root = tmp_path / "checkout"
    (root / ".git").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='example'\n", encoding="utf-8")
    (root / "install-stamp.json").write_text('{"updateMechanism": "self"}', encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    monkeypatch.setattr(venv_sync, "_launch_currency", lambda root, command: True)
    store_python = tmp_path / "store" / "cpython-3.14" / "bin" / "python3"
    store_python.parent.mkdir(parents=True)
    store_python.write_text("#!/bin/sh\n", encoding="utf-8")
    # Intact checkout launchers, as an earlier publication left them.
    from hermes_cli._launchers import WINDOWS_BIN_LAUNCHERS
    for name in WINDOWS_BIN_LAUNCHERS:
        launcher = root / ".hermes" / "bin" / name
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text(f"#!/bin/sh\nexec {store_python} -I -c 'pass' \"$@\"\n", encoding="utf-8")
    published = []
    monkeypatch.setattr(venv_sync, "publish_launchers", published.append)

    def run(*, store_spelling: Path, executable: Path):
        monkeypatch.setattr(_launchers, "resolve_store_python", lambda _, **__: store_spelling)
        monkeypatch.setattr(sys, "executable", str(executable))
        return venv_sync.prepare_launch(root, []), published

    return tmp_path, store_python, run


def test_store_python_spelled_through_dotdot_home_is_current(launch):
    """HERMES_HOME=<x>/work/../store...: the normalized sys.executable is the same interpreter."""
    tmp_path, store_python, run = launch
    dotted = tmp_path / "work" / ".." / store_python.relative_to(tmp_path)
    (tmp_path / "work").mkdir()

    assert run(store_spelling=dotted, executable=store_python) == (None, [])


@pytest.mark.platforms("posix")
def test_venv_python_symlinked_to_the_store_binary_still_relaunches(launch):
    """Same binary, different interpreter: the venv carries its own sys.prefix."""
    tmp_path, store_python, run = launch
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(store_python)

    target, published = run(store_spelling=store_python, executable=venv_python)
    assert target == store_python
    assert published == [], "a current interpreter handoff must not load config or republish"


@pytest.mark.platforms("posix")
def test_unknown_currency_still_hands_off_to_the_managed_interpreter(launch, monkeypatch):
    """A timed-out probe skips repair and publication, never interpreter isolation."""
    tmp_path, store_python, run = launch
    monkeypatch.setattr(venv_sync, "_launch_currency", lambda root, command: None)
    pending = venv_sync.arm_completion(tmp_path / "checkout")
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(store_python)

    assert run(store_spelling=store_python, executable=venv_python) == (store_python, [])
    assert run(store_spelling=store_python, executable=store_python) == (None, [])
    assert pending.is_file(), "an unknown verdict must leave completion debt for a later launch"


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("damage", ["missing", "dead interpreter", "stale store pin"])
def test_current_launch_republishes_only_a_missing_dead_or_stale_launcher(launch, damage):
    from pm.environments import store_root

    tmp_path, store_python, run = launch
    launcher = tmp_path / "checkout" / ".hermes" / "bin" / "hermes"
    if damage == "missing":
        launcher.unlink()
    elif damage == "dead interpreter":
        launcher.write_text(f"#!/bin/sh\nexec {tmp_path / 'gone' / 'python3'} -I -c 'pass'\n", encoding="utf-8")
    else:
        # A live, older Python in this root's own store: publication would repin it, so
        # skipping publication must not keep it until that Python is collected.
        old = store_root((tmp_path / "checkout").resolve()) / "cpython-3.13" / "bin" / "python3"
        old.parent.mkdir(parents=True)
        old.write_text("#!/bin/sh\n", encoding="utf-8")
        launcher.write_text(f"#!/bin/sh\nexec {old} -I -c 'pass'\n", encoding="utf-8")

    assert run(store_spelling=store_python, executable=store_python) == (
        None, [(tmp_path / "checkout").resolve()])


@pytest.mark.platforms("posix")
def test_current_launch_keeps_a_shared_launcher_bound_outside_its_store(launch):
    """A borrowed checkout's launcher bound to another home's live Python is not republished."""
    tmp_path, store_python, run = launch
    other = tmp_path / "other-home" / "tools" / "python3"
    other.parent.mkdir(parents=True)
    other.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher = tmp_path / "checkout" / ".hermes" / "bin" / "hermes"
    launcher.write_text(f"#!/bin/sh\nexec {other} -I -c 'pass'\n", encoding="utf-8")

    assert run(store_spelling=store_python, executable=store_python) == (None, [])


@pytest.mark.platforms("posix")
def test_current_handoff_spares_the_execd_child_a_second_probe(launch, monkeypatch):
    """The exec'd child (same pid) trusts its parent's fresh verdict once; nobody else does."""
    import os

    tmp_path, store_python, run = launch
    root = (tmp_path / "checkout").resolve()
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(store_python)

    assert run(store_spelling=store_python, executable=venv_python) == (store_python, [])
    handoff = venv_sync.launch_handoff_env()
    assert handoff == {venv_sync.LAUNCH_HANDOFF_ENV: f"current:{os.getpid()}:{root}"}

    probes = []
    monkeypatch.setattr(venv_sync, "_launch_currency", lambda root, command: probes.append(root) or None)
    for name, value in handoff.items():
        monkeypatch.setenv(name, value)
    assert run(store_spelling=store_python, executable=store_python) == (None, [])
    assert probes == [], "the exec'd child repeated its parent's probe"
    assert venv_sync.LAUNCH_HANDOFF_ENV not in os.environ, "the handoff must not reach subprocesses"
    assert venv_sync.launch_handoff_env() == {}

    # A re-exec after the handoff was consumed (an early-recovery restore) and a foreign
    # process (another pid) both probe for themselves.
    assert run(store_spelling=store_python, executable=store_python) == (None, [])
    monkeypatch.setenv(venv_sync.LAUNCH_HANDOFF_ENV, f"current:{os.getpid() + 1}:{root}")
    assert run(store_spelling=store_python, executable=store_python) == (None, [])
    assert probes == [root, root]


@pytest.mark.platforms("posix")
def test_unknown_currency_hands_off_as_unknown_without_a_second_wait(launch, monkeypatch):
    """A timed-out probe is handed on as unknown: the exec'd child neither re-probes
    (a second deadline) nor treats it as current (it still owes the debt)."""
    import os

    tmp_path, store_python, run = launch
    root = (tmp_path / "checkout").resolve()
    probes = []
    monkeypatch.setattr(venv_sync, "_launch_currency", lambda root, command: probes.append(root) or None)
    monkeypatch.setattr(venv_sync, "_unknown_roots", set())
    monkeypatch.setattr(venv_sync, "_current_roots", set())
    pending = venv_sync.arm_completion(root)
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(store_python)

    assert run(store_spelling=store_python, executable=venv_python) == (store_python, [])
    handoff = venv_sync.launch_handoff_env()
    assert handoff == {venv_sync.LAUNCH_HANDOFF_ENV: f"unknown:{os.getpid()}:{root}"}
    for name, value in handoff.items():
        monkeypatch.setenv(name, value)
    assert run(store_spelling=store_python, executable=store_python) == (None, [])
    assert probes == [root], "the exec'd child waited out a second probe"
    assert root in venv_sync._unknown_roots and root not in venv_sync._current_roots
    assert pending.is_file()
