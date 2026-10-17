"""[daveey] Children's bare python3 resolves to the signed Hermes Python.app, not the ad-hoc store Python."""
import plistlib
from pathlib import Path

import pytest

from hermes_cli._launchers import prefer_signed_python_on_path


def _bundle(home: Path, version: str) -> Path:
    contents = home / "Applications" / "Hermes Python.app" / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    exe = contents / "MacOS" / "python3.14"
    exe.write_bytes(b"")
    (contents / "Info.plist").write_bytes(plistlib.dumps(
        {"CFBundleShortVersionString": version, "CFBundleExecutable": "python3.14"}))
    return exe


@pytest.mark.platforms("macos")
def test_signed_dir_goes_before_matching_store_bin(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    store_bin = str(tmp_path / "tools" / "python-3.14.7+20260901-darwin-arm64" / "bin")
    path = f"/a/node/bin:{store_bin}:/usr/bin"
    assert prefer_signed_python_on_path(path) == path  # no bundle: unchanged
    exe = _bundle(tmp_path, "3.14.7")
    signed = str(exe.parent)
    assert prefer_signed_python_on_path(path) == f"/a/node/bin:{signed}:{store_bin}:/usr/bin"
    # idempotent, and an existing later copy of the signed dir moves up instead of duplicating
    assert prefer_signed_python_on_path(f"{store_bin}:/usr/bin:{signed}") == f"{signed}:{store_bin}:/usr/bin"


@pytest.mark.platforms("macos")
def test_version_mismatch_keeps_store_first(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _bundle(tmp_path, "3.14.7")
    newer = str(tmp_path / "tools" / "python-3.14.8+20261001-darwin-arm64" / "bin")
    assert prefer_signed_python_on_path(f"{newer}:/usr/bin") == f"{newer}:/usr/bin"


@pytest.mark.platforms("macos")
def test_child_env_resolves_signed_python(tmp_path, monkeypatch):
    """The terminal/cron sanitizer (``_scrubbed_env``) applies it to every child PATH."""
    from tools.environments import local

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(local, "_prepend_hermes_bin_dir", lambda p: p)
    exe = _bundle(tmp_path, "3.14.7")
    store_bin = str(tmp_path / "tools" / "python-3.14.7+20260901-darwin-arm64" / "bin")
    env = local._scrubbed_env([({"PATH": f"{store_bin}:/usr/bin"}, True)], frozenset(), lambda p: p)
    dirs = env["PATH"].split(":")
    assert dirs.index(str(exe.parent)) < dirs.index(store_bin)
