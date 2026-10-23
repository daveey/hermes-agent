"""Two plugin dirs in one scope declaring the same manifest ``name`` (#daveey).

A ``slack-workspace.bak-premacops/`` backup (``name: slack-workspace``) left next to the live
``slack-workspace/`` silently won discovery because the scan is sorted and later manifests win, so
every fix to the live plugin never ran. Discovery now skips backup-named dirs (``*.bak*``,
``*.orig``, ``*.old``) and, for any remaining same-name pair, loads the dir whose basename equals the
name, warning with both paths. ``hermes plugins list`` and ``hermes doctor`` surface the ignored copy.
"""

import logging
import sys

import hermes_yaml as yaml
import pytest

from hermes_cli.plugins import PluginManager


def _write_plugin(base, dirname, name=None, probe=None):
    plugin_dir = base / dirname
    plugin_dir.mkdir(parents=True, exist_ok=True)
    name = name or dirname
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump({"name": name, "version": "0.1.0", "description": f"{dirname} copy"}))
    body = f"import sys; sys._dup_probe = {probe or dirname!r}"
    (plugin_dir / "__init__.py").write_text(f"def register(ctx):\n    {body}\n")
    return plugin_dir


def _enable(home, names):
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": list(names)}}))


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    (home / "plugins").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(tmp_path / "empty-bundled"))
    (tmp_path / "empty-bundled").mkdir()
    yield home
    if hasattr(sys, "_dup_probe"):
        del sys._dup_probe


def _load(home, caplog):
    with caplog.at_level(logging.INFO, logger="hermes_cli.plugins"):
        mgr = PluginManager()
        mgr.discover_and_load()
    return mgr


class TestSameNameDirsInOneScope:
    def test_backup_dir_never_shadows_the_live_plugin(self, hermes_home, caplog):
        plugins = hermes_home / "plugins"
        live = _write_plugin(plugins, "slack-workspace", probe="live")
        backup = _write_plugin(plugins, "slack-workspace.bak-premacops", name="slack-workspace", probe="backup")
        _enable(hermes_home, ["slack-workspace"])
        mgr = _load(hermes_home, caplog)
        assert mgr._plugins["slack-workspace"].manifest.path == str(live)
        assert sys._dup_probe == "live"
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(str(backup) in w and "backup" in w for w in warnings), warnings

    @pytest.mark.parametrize("suffix", [".bak", ".bak2", ".BAK-old", ".orig", ".old"])
    def test_backup_suffixes_are_skipped_even_when_unique(self, hermes_home, caplog, suffix):
        _write_plugin(hermes_home / "plugins", f"solo{suffix}", name="solo")
        _enable(hermes_home, ["solo"])
        mgr = _load(hermes_home, caplog)
        assert "solo" not in mgr._plugins
        assert not hasattr(sys, "_dup_probe")
        assert any(f"solo{suffix}" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)

    def test_non_backup_copy_loses_to_basename_match_and_both_paths_are_named(self, hermes_home, caplog):
        plugins = hermes_home / "plugins"
        live = _write_plugin(plugins, "thing", probe="live")
        copy = _write_plugin(plugins, "thing-copy", name="thing", probe="copy")  # sorts after "thing"
        _enable(hermes_home, ["thing"])
        mgr = _load(hermes_home, caplog)
        assert mgr._plugins["thing"].manifest.path == str(live)
        assert sys._dup_probe == "live"
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(str(live) in w and str(copy) in w for w in warnings), warnings

    def test_basename_match_wins_regardless_of_scan_order(self, hermes_home, caplog):
        plugins = hermes_home / "plugins"
        copy = _write_plugin(plugins, "a-zeta-copy", name="zeta", probe="copy")  # sorts before "zeta"
        live = _write_plugin(plugins, "zeta", probe="live")
        _enable(hermes_home, ["zeta"])
        mgr = _load(hermes_home, caplog)
        assert mgr._plugins["zeta"].manifest.path == str(live)
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(str(live) in w and str(copy) in w for w in warnings), warnings

    def test_no_basename_match_keeps_first_and_warns(self, hermes_home, caplog):
        plugins = hermes_home / "plugins"
        first = _write_plugin(plugins, "x1", name="thing", probe="x1")
        second = _write_plugin(plugins, "x2", name="thing", probe="x2")
        _enable(hermes_home, ["thing"])
        mgr = _load(hermes_home, caplog)
        assert mgr._plugins["thing"].manifest.path == str(first)
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(str(first) in w and str(second) in w for w in warnings), warnings

    def test_unrelated_plugins_raise_no_conflict(self, hermes_home, caplog):
        from hermes_cli.plugins_discovery import plugin_dir_conflicts
        _write_plugin(hermes_home / "plugins", "alpha")
        _write_plugin(hermes_home / "plugins", "beta")
        _enable(hermes_home, ["alpha", "beta"])
        _load(hermes_home, caplog)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING and "plugin" in r.getMessage().lower()
                    and ("backup" in r.getMessage() or "same name" in r.getMessage())]
        assert plugin_dir_conflicts(hermes_home / "plugins") == []


class TestConflictsAreSurfaced:
    def _setup(self, home):
        plugins = home / "plugins"
        live = _write_plugin(plugins, "slack-workspace")
        backup = _write_plugin(plugins, "slack-workspace.bak-premacops", name="slack-workspace")
        dup = _write_plugin(plugins, "slack-copy", name="slack-workspace")
        return live, backup, dup

    def test_plugin_dir_conflicts_lists_backup_and_shadowed_copy(self, hermes_home):
        from hermes_cli.plugins_discovery import plugin_dir_conflicts
        live, backup, dup = self._setup(hermes_home)
        conflicts = plugin_dir_conflicts(hermes_home / "plugins")
        by_path = {c.ignored: c for c in conflicts}
        assert set(by_path) == {str(backup), str(dup)}
        assert by_path[str(backup)].kind == "backup"
        assert by_path[str(dup)].kind == "duplicate"
        assert by_path[str(dup)].kept == str(live)
        assert by_path[str(dup)].name == "slack-workspace"

    def test_listing_shows_the_live_copy(self, hermes_home):
        from hermes_cli.plugins_cmd import _discover_all_plugins
        live, _backup, _dup = self._setup(hermes_home)
        rows = {r[5]: r for r in _discover_all_plugins()}
        assert str(rows["slack-workspace"][4]) == str(live)

    def test_plugins_list_prints_the_shadowed_copies(self, hermes_home, capsys):
        from types import SimpleNamespace
        from hermes_cli.plugins_cmd_listing import cmd_list
        _live, backup, dup = self._setup(hermes_home)
        cmd_list(SimpleNamespace(plain=True, json=False))
        out = capsys.readouterr()
        text = out.out + out.err
        assert str(backup) in text and str(dup) in text

    def test_doctor_warns_about_shadowed_copies(self, hermes_home, monkeypatch):
        from hermes_cli import doctor_state as ds
        _live, backup, dup = self._setup(hermes_home)
        warned = []
        monkeypatch.setattr(ds, "check_warn", lambda text, detail="": warned.append(f"{text} {detail}"))
        monkeypatch.setattr(ds, "check_ok", lambda *a, **k: None)
        finding = ds._check_plugin_dir_conflicts(False)
        joined = "\n".join(warned)
        assert str(backup) in joined and str(dup) in joined
        assert finding.manual_issues

    def test_doctor_is_quiet_when_clean(self, hermes_home, monkeypatch):
        from hermes_cli import doctor_state as ds
        _write_plugin(hermes_home / "plugins", "alpha")
        warned = []
        monkeypatch.setattr(ds, "check_warn", lambda text, detail="": warned.append(text))
        monkeypatch.setattr(ds, "check_ok", lambda *a, **k: None)
        finding = ds._check_plugin_dir_conflicts(False)
        assert warned == [] and not finding.manual_issues
