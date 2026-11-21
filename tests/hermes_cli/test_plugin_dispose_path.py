"""Disposal policy and the real out-of-process ctx round trip."""

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
