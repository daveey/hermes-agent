"""[daveey] signed_python_app: the Developer-ID twin replaces the store Python only on a version match."""
import plistlib
from pathlib import Path

import pytest

from hermes_cli._launchers import signed_python_app


def _bundle(home: Path, version: str) -> Path:
    contents = home / "Applications" / "Hermes Python.app" / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    exe = contents / "MacOS" / "python3.14"
    exe.write_bytes(b"")
    (contents / "Info.plist").write_bytes(plistlib.dumps(
        {"CFBundleShortVersionString": version, "CFBundleExecutable": "python3.14"}))
    return exe


@pytest.mark.platforms("macos")
def test_twin_used_only_for_matching_store_version(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    store = tmp_path / "tools" / "python-3.14.7+20260901-darwin-arm64" / "bin" / "python3"
    assert signed_python_app(store) is None  # no bundle: keep the store
    exe = _bundle(tmp_path, "3.14.7")
    assert signed_python_app(store) == exe
    newer = tmp_path / "tools" / "python-3.14.8+20261001-darwin-arm64" / "bin" / "python3"
    assert signed_python_app(newer) is None  # stale bundle never runs a newer store's deps
