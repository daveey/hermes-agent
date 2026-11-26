"""Update warming must not execute sources, escape the checkout, or fail updates."""
import importlib.util
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hermes_cli import update_bytecode


def test_real_compile_isolated_and_bounded_to_checkout(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    package = root / "hermes_cli"
    package.mkdir()
    source = package / "sample.py"
    source.write_text("raise RuntimeError('must never execute')\n")
    top = root / "entry.py"
    top.write_text("answer = 42\n")
    tests = root / "tests"
    tests.mkdir()
    (tests / "ignored.py").write_text("pass\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "foreign.py"
    external.write_text("pass\n")
    try:
        (root / "agent").symlink_to(outside, target_is_directory=True)
        (root / "foreign.py").symlink_to(external)
        (package / "foreign.py").symlink_to(external)
        (package / "nested").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    prefix = tmp_path / "not-the-checkout"
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(prefix))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    update_bytecode.precompile_checkout(root)
    assert Path(importlib.util.cache_from_source(str(source))).is_file()
    assert Path(importlib.util.cache_from_source(str(top))).is_file()
    assert not (tests / "__pycache__").exists()
    assert not (outside / "__pycache__").exists()
    assert not prefix.exists()


@pytest.mark.parametrize("failure", [OSError("unavailable"), subprocess.TimeoutExpired("compile", 30),
                                     subprocess.SubprocessError("failed"), None])
def test_compile_failures_are_best_effort(tmp_path, monkeypatch, failure):
    (tmp_path / "entry.py").write_text("pass\n")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if failure:
            raise failure
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(update_bytecode.subprocess, "run", run)
    update_bytecode.precompile_checkout(tmp_path)
    argv, options = calls[0]
    assert argv[:9] == [sys.executable, "-I", "-S", "-m", "compileall", "-q", "-j", "1", "-e"]
    assert options["timeout"] == 30
    assert options["stdin"] == subprocess.DEVNULL
    assert options["stdout"] == subprocess.DEVNULL
    assert options["stderr"] == subprocess.DEVNULL


def test_empty_tree_does_not_spawn(tmp_path, monkeypatch):
    monkeypatch.setattr(update_bytecode.subprocess, "run", lambda *a, **kw: pytest.fail("unneeded spawn"))
    update_bytecode.precompile_checkout(tmp_path)


@pytest.mark.parametrize("sweep_fails,compile_fails", [(False, False), (True, False), (False, True)])
def test_update_warms_after_successful_stamp_only(tmp_path, monkeypatch, sweep_fails, compile_fails):
    from hermes_cli import update_cmd_maint as maintenance
    from hermes_cli import main_web_build, update_receipt
    events = []

    def clear(root):
        events.append("clear")
        if sweep_fails:
            raise OSError("cannot clear")
        return 0

    facade = SimpleNamespace(PROJECT_ROOT=tmp_path, _clear_bytecode_cache=clear,
                             _record_bytecode_fingerprint=lambda: events.append("stamp"),
                             _refresh_bootstrap_cache_scripts=lambda branch: events.append("refresh"))
    monkeypatch.setattr(maintenance, "_m", lambda: facade, raising=False)
    from hermes_cli import update_cmd
    monkeypatch.setattr(update_cmd, "_m", lambda: facade)
    monkeypatch.setattr(main_web_build, "_recorded_bytecode_fingerprint", lambda root: None)
    monkeypatch.setattr(main_web_build, "_prune_dropped_directories", lambda *args: 0)
    monkeypatch.setattr(update_receipt, "record_followup", lambda *args, **kw: events.append(args[0]))
    def compile_checkout(root):
        events.append("compile")
        if compile_fails:
            raise RuntimeError("unexpected helper failure")

    monkeypatch.setattr(update_bytecode, "precompile_checkout", compile_checkout)
    maintenance._sweep_bytecode_after_update("main")
    assert events == (["clear", "bytecode_sweep", "refresh"] if sweep_fails else
                      ["clear", "stamp", "compile", "refresh"])


def test_update_sweep_replaces_stale_cache_with_usable_bytecode(tmp_path, monkeypatch):
    from hermes_cli import main, main_web_build, update_cmd, update_cmd_maint
    source = tmp_path / "entry.py"
    source.write_text("value = 'new'\n")
    cache = Path(importlib.util.cache_from_source(str(source)))
    cache.parent.mkdir()
    cache.write_bytes(b"stale")
    facade = SimpleNamespace(PROJECT_ROOT=tmp_path, _clear_bytecode_cache=main._clear_bytecode_cache,
                             _record_bytecode_fingerprint=lambda: None,
                             _refresh_bootstrap_cache_scripts=lambda branch: None)
    monkeypatch.setattr(update_cmd, "_m", lambda: facade)
    monkeypatch.setattr(main_web_build, "_recorded_bytecode_fingerprint", lambda root: None)
    monkeypatch.setattr(main_web_build, "_prune_dropped_directories", lambda *a: 0)
    update_cmd_maint._sweep_bytecode_after_update("main")
    assert cache.read_bytes().startswith(importlib.util.MAGIC_NUMBER)


def test_matching_launch_fingerprint_does_not_compile(tmp_path, monkeypatch):
    from hermes_cli import main, main_web_build
    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(main, "_read_git_revision_fingerprint", lambda root: "same")
    monkeypatch.setattr(main_web_build, "_recorded_bytecode_fingerprint", lambda root: "same")
    monkeypatch.setattr(main, "_clear_bytecode_cache", lambda root: pytest.fail("unneeded sweep"))
    monkeypatch.setattr(update_bytecode, "precompile_checkout", lambda root: pytest.fail("launch warming"))
    main_web_build._sweep_stale_bytecode_if_checkout_changed()
