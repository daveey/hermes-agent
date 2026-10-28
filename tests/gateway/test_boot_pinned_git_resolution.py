"""Boot pinning uses the shared passive resolver without losing explicit git overrides."""

from gateway.boot_pinned_imports import BootPinnedFinder
from hermes_platform.resolver import Candidate, Resolution


def test_boot_finder_uses_resolved_git(tmp_path, monkeypatch):
    executable = str(tmp_path / "git")
    calls = []

    def resolve(name):
        calls.append(name)
        return Resolution("path_executable", (Candidate(executable, "PATH", True),))

    monkeypatch.setattr("gateway.boot_pinned_imports.locate_command", resolve)
    finder = BootPinnedFinder(str(tmp_path), "a" * 40, lambda: None)
    assert finder._git == executable
    assert calls == ["git"]


def test_boot_finder_missing_git_disables_blob_reads(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.boot_pinned_imports.locate_command",
                        lambda name: Resolution("missing"))
    finder = BootPinnedFinder(str(tmp_path), "a" * 40, lambda: None)
    assert finder._run_git("show", "HEAD:missing.py") is None


def test_boot_finder_explicit_git_skips_discovery(tmp_path, monkeypatch):
    def unexpected(name):
        raise AssertionError("explicit git must not trigger PATH discovery")

    monkeypatch.setattr("gateway.boot_pinned_imports.locate_command", unexpected)
    finder = BootPinnedFinder(str(tmp_path), "a" * 40, lambda: None, git="/explicit/git")
    assert finder._git == "/explicit/git"
