"""Disposal policy and the real out-of-process ctx round trip."""

import errno
import json
import logging
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace

import pytest

from hermes_cli import path_disposal
from hermes_cli.plugin_isolation import HOST_SKIPPED_CTX_METHODS, HOST_UNSUPPORTED_CTX_METHODS
from hermes_cli.plugins import PluginContext, PluginManager
from tests.hermes_cli.test_plugin_host import _home_with_plugins


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


def dispose(path, reason="expired test cache"):
    return path_disposal.dispose_path(str(path), actor="plugin:probe", reason=reason)


def test_file_and_actor(home, caplog):
    path = home / "cache-file"
    path.write_text("private contents must not be logged")
    ctx = PluginContext(SimpleNamespace(name="probe"), SimpleNamespace())
    with caplog.at_level(logging.INFO, logger=path_disposal.__name__):
        assert ctx.dispose_path(str(path), reason="expired") == {
            "path": str(path), "outcome": "disposed", "detail": ""}
    assert not path.exists()
    assert "plugin:probe" in caplog.text and "expired" in caplog.text
    assert str(path) in caplog.text and "disposed" in caplog.text
    assert "private contents" not in caplog.text


def test_directory(home):
    path = home / "cache-dir"
    (path / "nested").mkdir(parents=True)
    (path / "nested" / "data").write_text("cache")
    assert dispose(path)["outcome"] == "disposed"
    assert not path.exists()


def test_missing(home):
    assert dispose(home / "absent")["outcome"] == "missing"


def test_outside(home, tmp_path):
    path = tmp_path / "ordinary-control"
    path.write_text("keep")
    assert dispose(path)["outcome"] == "refused"
    assert path.read_text() == "keep"


def test_home_root(home):
    (home / "keep").write_text("keep")
    assert dispose(home)["outcome"] == "refused"
    assert (home / "keep").read_text() == "keep"


def test_final_symlink(home, tmp_path):
    target = tmp_path / "outside-target"
    target.write_text("keep")
    link = home / "link"
    link.symlink_to(target)
    assert dispose(link) == {"path": str(link), "outcome": "disposed", "detail": ""}
    assert not link.is_symlink()
    assert target.read_text() == "keep"


def test_traversal(home, tmp_path):
    path = tmp_path / "outside-traversal"
    path.write_text("keep")
    assert dispose(home / ".." / path.name)["outcome"] == "refused"
    assert path.read_text() == "keep"


def test_parent_symlink_traversal(home, tmp_path):
    outside = tmp_path / "outside-dir"
    (outside / "nested").mkdir(parents=True)
    target = outside / "keep"
    target.write_text("keep")
    (home / "parent-link").symlink_to(outside / "nested", target_is_directory=True)
    result = dispose(home / "parent-link" / ".." / "keep")
    assert result["path"] == str(target)
    assert result["outcome"] == "refused"
    assert target.read_text() == "keep"


def test_write_denied(home):
    path = home / "state.db"
    path.write_text("{}")
    assert dispose(path)["outcome"] == "refused"
    assert path.read_text() == "{}"


@pytest.mark.platforms("posix")
def test_read_only_parent_error(home):
    parent = home / "readonly"
    parent.mkdir()
    path = parent / "data"
    path.write_text("keep")
    parent.chmod(0o500)
    try:
        result = dispose(path)
        assert result["outcome"] == "error"
        assert result["detail"]
        assert path.read_text() == "keep"
    finally:
        parent.chmod(0o700)


def test_oserror_text(home, monkeypatch):
    path = home / "data"
    path.write_text("keep")
    def fail(self, *args, **kwargs):
        raise OSError("controlled unlink error")
    monkeypatch.setattr(Path, "unlink", fail)
    assert dispose(path) == {"path": str(path), "outcome": "error", "detail": "controlled unlink error"}


@pytest.mark.parametrize("reason", ["", " ", "\n\t"])
def test_blank_reason(home, reason):
    with pytest.raises(ValueError):
        dispose(home / "absent", reason=reason)


@pytest.mark.parametrize("path", [None, 1, Path("relative")])
def test_non_string_path(path):
    with pytest.raises(TypeError):
        path_disposal.dispose_path(path, actor="plugin:probe", reason="test")


def test_tmp_root_and_child(home):
    # Deliberate root fixture: exercise the literal /tmp allowlist, not TMPDIR.
    with tempfile.TemporaryDirectory(prefix="hermes-disposal-", dir="/tmp") as root:  # no-tmp: ok — policy fixture
        path = Path(root) / "cache"
        path.write_text("cache")
        assert dispose(root)["outcome"] == "refused"
        assert path.exists()
        assert dispose(path)["outcome"] == "disposed"


HOST_PLUGIN = '''
import json, os

def register(ctx):
    assert "dispose_path" in ctx._methods
    def handler(args, **kw):
        result = ctx.dispose_path(args["path"], reason="expired via host")
        return json.dumps({"result": result, "pid": os.getpid()})
    ctx.register_tool(name="disposal_probe", toolset="disposal_probe",
        schema={"name": "disposal_probe", "description": "test",
                "parameters": {"type": "object", "properties": {}}}, handler=handler)
'''


def test_host_crossing(tmp_path, monkeypatch, caplog):
    from tools.registry import registry
    assert "dispose_path" not in HOST_UNSUPPORTED_CTX_METHODS
    assert "dispose_path" not in HOST_SKIPPED_CTX_METHODS
    home = _home_with_plugins(tmp_path, monkeypatch, {"disposal-probe": HOST_PLUGIN})
    path = home / "cache-file"
    path.write_text("cache")
    manager = PluginManager()
    manager.discover_and_load()
    try:
        assert manager._plugins["disposal-probe"].error is None
        with caplog.at_level(logging.INFO, logger=path_disposal.__name__):
            reply = json.loads(registry.dispatch("disposal_probe", {"path": str(path)}, scope=manager.scope_key))
        assert reply["pid"] != os.getpid()
        assert reply["result"] == {"path": str(path), "outcome": "disposed", "detail": ""}
        assert "plugin:disposal-probe" in caplog.text
        assert not path.exists()
        missing = json.loads(registry.dispatch("disposal_probe", {"path": str(path)}, scope=manager.scope_key))
        assert missing["result"]["outcome"] == "missing"
    finally:
        manager.unload()
        manager._plugin_host().shutdown()


# Keep the original frozen host probe and its default-mode tests unchanged.
HOST_EMPTY_PLUGIN = HOST_PLUGIN.replace(
    'reason="expired via host")',
    'reason="expired via host", empty_only=args["empty_only"])',
)


def empty_dispose(path):
    return path_disposal.dispose_path(
        str(path), actor="plugin:probe", reason="empty sweep", empty_only=True)


def test_empty_only_directory(home, caplog):
    path = home / "empty-dir"
    path.mkdir()
    ctx = PluginContext(SimpleNamespace(name="probe"), SimpleNamespace())
    with caplog.at_level(logging.INFO, logger=path_disposal.__name__):
        result = ctx.dispose_path(str(path), reason="empty sweep", empty_only=True)
    assert result == {"path": str(path), "outcome": "disposed", "detail": ""}
    assert not path.exists()
    assert "empty_only=True" in caplog.text


def test_empty_only_nonempty(home):
    path = home / "nonempty"
    path.mkdir()
    (path / "keep").write_text("keep")
    assert empty_dispose(path) == {
        "path": str(path), "outcome": "refused", "detail": "empty_only: directory is not empty"}
    assert (path / "keep").read_text() == "keep"


@pytest.mark.parametrize("empty_only", [True, False])
def test_empty_only_arrival(home, empty_only):
    path = home / "observed-empty"
    path.mkdir()
    assert not any(path.iterdir())
    (path / "arrival").write_text("arrived after observation")
    result = path_disposal.dispose_path(
        str(path), actor="plugin:probe", reason="sweep", empty_only=empty_only)
    if empty_only:
        assert result == {"path": str(path), "outcome": "refused",
                          "detail": "empty_only: directory is not empty"}
        assert (path / "arrival").read_text() == "arrived after observation"
    else:
        assert result == {"path": str(path), "outcome": "disposed", "detail": ""}
        assert not path.exists()


def test_empty_only_symlink(home):
    target = home / "target-dir"
    target.mkdir()
    (target / "keep").write_text("keep")
    link = home / "directory-link"
    link.symlink_to(target, target_is_directory=True)
    assert empty_dispose(link) == {
        "path": str(link), "outcome": "refused", "detail": "empty_only: path is not a directory"}
    assert link.is_symlink()
    assert (target / "keep").read_text() == "keep"


def test_empty_only_file(home):
    path = home / "regular-file"
    path.write_text("keep")
    assert empty_dispose(path) == {
        "path": str(path), "outcome": "refused", "detail": "empty_only: path is not a directory"}
    assert path.read_text() == "keep"


def test_empty_only_missing(home):
    assert empty_dispose(home / "absent")["outcome"] == "missing"


def test_empty_only_scope(home, tmp_path):
    outside = tmp_path / "outside-empty"
    outside.mkdir()
    assert empty_dispose(outside)["outcome"] == "refused"
    assert outside.is_dir()
    assert empty_dispose(home)["outcome"] == "refused"
    assert home.is_dir()


@pytest.mark.parametrize("value", [1, "true", None])
def test_empty_only_nonbool(home, value):
    with pytest.raises(TypeError, match="empty_only"):
        path_disposal.dispose_path(str(home / "absent"), actor="probe", reason="sweep", empty_only=value)


@pytest.mark.parametrize("error", [PermissionError("controlled permission error"),
                                  OSError(errno.EBUSY, "controlled busy error"),
                                  OSError(errno.ENOTDIR, "controlled swapped-path error")])
def test_empty_only_oserror(home, monkeypatch, error):
    path = home / "busy"
    path.mkdir()
    calls = []
    def fail(target):
        calls.append(target)
        raise error
    with monkeypatch.context() as patcher:
        patcher.setattr(os, "rmdir", fail)
        result = empty_dispose(path)
    assert result == {"path": str(path), "outcome": "error", "detail": str(error)}
    assert calls == [path]
    assert path.is_dir()


@pytest.mark.parametrize("error_number", [errno.EEXIST, errno.ENOTEMPTY])
def test_empty_only_notempty_errno(home, monkeypatch, error_number):
    path = home / "notempty-error"
    path.mkdir()
    calls = []
    def fail(target):
        calls.append(target)
        raise OSError(error_number, "controlled notempty error")
    with monkeypatch.context() as patcher:
        patcher.setattr(os, "rmdir", fail)
        result = empty_dispose(path)
    assert result == {"path": str(path), "outcome": "refused",
                      "detail": "empty_only: directory is not empty"}
    assert calls == [path]
    assert path.is_dir()


def test_empty_only_write_denied(home):
    path = home / "state.db"
    path.mkdir()
    assert empty_dispose(path) == {
        "path": str(path), "outcome": "refused", "detail": "path is write-denied"}
    assert path.is_dir()


def test_empty_only_tmp_root(home):
    with tempfile.TemporaryDirectory(prefix="hermes-disposal-", dir="/tmp") as root:  # no-tmp: ok — policy fixture
        assert empty_dispose(root)["outcome"] == "refused"
        child = Path(root) / "empty"
        child.mkdir()
        assert empty_dispose(child) == {"path": str(child.resolve()), "outcome": "disposed", "detail": ""}
        assert not child.exists()


def test_empty_only_single_atomic_attempt(home, monkeypatch):
    path = home / "atomic-empty"
    path.mkdir()
    calls = []
    real_rmdir = os.rmdir
    def remove(target):
        calls.append(target)
        # Simulate an arrival after lstat, immediately before the kernel operation.
        (path / "arrival").write_text("keep")
        return real_rmdir(target)
    real_iterdir = Path.iterdir
    def no_target_scan(target):
        if target == path:
            pytest.fail("empty_only must not pre-check emptiness")
        return real_iterdir(target)
    def forbidden(*args, **kwargs):
        pytest.fail("empty_only must not recurse or unlink")
    with monkeypatch.context() as patcher:
        patcher.setattr(os, "rmdir", remove)
        patcher.setattr(Path, "iterdir", no_target_scan)
        patcher.setattr(path_disposal.shutil, "rmtree", forbidden)
        patcher.setattr(Path, "unlink", forbidden)
        result = empty_dispose(path)
    assert result == {"path": str(path), "outcome": "refused",
                      "detail": "empty_only: directory is not empty"}
    assert calls == [path]
    assert (path / "arrival").read_text() == "keep"


@pytest.mark.parametrize("nonempty", [False, True])
def test_host_crossing_empty_only(tmp_path, monkeypatch, nonempty):
    from tools.registry import registry
    home = _home_with_plugins(tmp_path, monkeypatch, {"disposal-probe": HOST_EMPTY_PLUGIN})
    path = home / "host-directory"
    path.mkdir()
    if nonempty:
        (path / "keep").write_text("keep")
    manager = PluginManager()
    manager.discover_and_load()
    try:
        assert manager._plugins["disposal-probe"].error is None
        reply = json.loads(registry.dispatch(
            "disposal_probe", {"path": str(path), "empty_only": True}, scope=manager.scope_key))
        assert reply["pid"] != os.getpid()
        ctx = PluginContext(SimpleNamespace(name="disposal-probe"), SimpleNamespace())
        if nonempty:
            assert (path / "keep").read_text() == "keep"
        else:
            assert not path.exists()
            path.mkdir()
        assert reply["result"] == ctx.dispose_path(
            str(path), reason="expired via host", empty_only=True)
        assert reply["result"]["outcome"] == ("refused" if nonempty else "disposed")
        if nonempty:
            assert reply["result"]["detail"] == "empty_only: directory is not empty"
            assert (path / "keep").read_text() == "keep"
        else:
            assert not path.exists()
    finally:
        manager.unload()
        manager._plugin_host().shutdown()
