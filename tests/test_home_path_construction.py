"""Only immutable Path construction is memoized; home selection/expansion remains live."""
from pathlib import Path

import hermes_constants as hc


def test_reuses_expanded_path_without_freezing_environment(monkeypatch, tmp_path):
    monkeypatch.setenv('PICKER_HOME_ROOT', str(tmp_path / 'one'))
    first = hc._expand_hermes_home('$PICKER_HOME_ROOT/profile')
    assert hc._expand_hermes_home('$PICKER_HOME_ROOT/profile') is first
    monkeypatch.setenv('PICKER_HOME_ROOT', str(tmp_path / 'two'))
    assert hc._expand_hermes_home('$PICKER_HOME_ROOT/profile') == tmp_path / 'two/profile'
    assert hc._expand_hermes_home('$PICKER_HOME_ROOT/profile') is not first


def test_tilde_expansion_is_live(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path / 'first'))
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'first'))
    assert hc._expand_hermes_home('~/profile') == tmp_path / 'first/profile'
    monkeypatch.setenv('HOME', str(tmp_path / 'second'))
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'second'))
    assert hc._expand_hermes_home('~/profile') == tmp_path / 'second/profile'


def test_process_and_nested_context_home_changes_are_immediate(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'process'))
    assert hc.get_hermes_home() == tmp_path / 'process'
    token = hc.set_hermes_home_override(tmp_path / 'outer')
    try:
        assert hc.get_hermes_home() == tmp_path / 'outer'
        nested = hc.set_hermes_home_override(tmp_path / 'inner')
        try:
            assert hc.get_hermes_home() == tmp_path / 'inner'
        finally:
            hc.reset_hermes_home_override(nested)
        assert hc.get_hermes_home() == tmp_path / 'outer'
    finally:
        hc.reset_hermes_home_override(token)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'next'))
    assert hc.get_hermes_home() == tmp_path / 'next'


def test_relative_path_does_not_freeze_working_directory(monkeypatch, tmp_path):
    relative = hc._expand_hermes_home('relative-home')
    monkeypatch.chdir(tmp_path)
    assert relative.resolve() == tmp_path / 'relative-home'
    assert relative == Path('relative-home')
