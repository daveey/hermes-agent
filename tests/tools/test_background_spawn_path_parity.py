"""Background terminal PATH matches the foreground's Hermes-managed dirs.

A gateway started by systemd lacks ``$HERMES_HOME/bin`` on PATH. The foreground
terminal (``_make_run_env``) appended it, the background spawn
(``ProcessRegistry._spawn_env``) did not, so ``terminal(background=true)`` ran
``hermes-core-wt`` as "No such file or directory" on the hub while the same
command in the foreground worked.
"""

import os

import pytest

from tools.environments import local as local_mod
from tools.environments.local import _make_run_env
from tools.process_registry import ProcessRegistry

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture
def hermes_bin(monkeypatch, tmp_path):
    home = tmp_path / "hermes-home"
    (home / "bin").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(local_mod, "_git_bash_bin_dirs", list)
    monkeypatch.setattr(local_mod, "_resolve_hermes_bin_dir", lambda: None)
    return str(home / "bin")


def test_background_spawn_env_has_hermes_home_bin(hermes_bin):
    entries = ProcessRegistry._spawn_env({})["PATH"].split(os.pathsep)
    assert hermes_bin in entries
    # The user's own entries keep precedence over appended Hermes dirs.
    assert entries.index("/usr/bin") < entries.index(hermes_bin)


def test_background_path_is_superset_of_foreground_path(hermes_bin):
    foreground = set(_make_run_env({})["PATH"].split(os.pathsep))
    background = set(ProcessRegistry._spawn_env({})["PATH"].split(os.pathsep))
    assert foreground - background == set()
