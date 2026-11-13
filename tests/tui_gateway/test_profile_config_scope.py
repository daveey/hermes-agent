from __future__ import annotations

from pathlib import Path

import pytest

from agent.secret_scope import current_secret_scope, current_secret_scope_home, get_secret, set_multiplex_active
from hermes_constants import get_hermes_home, get_hermes_home_override
from tools.terminal_scope import get_terminal_scope
from tui_gateway import launch_profile_policy as policy


@pytest.fixture(params=[False, True], ids=["single", "multiplex"])
def homes(request, tmp_path, monkeypatch):
    launch = tmp_path / ".hermes"
    work = launch / "profiles" / "work"
    work.mkdir(parents=True)
    (launch / "config.yaml").write_text("terminal:\n  backend: local\n", encoding="utf-8")
    (launch / ".env").write_text("LAUNCH_FILE_KEY=launch-file\nSHARED_KEY=launch\n", encoding="utf-8")
    (work / "config.yaml").write_text("terminal:\n  backend: docker\n", encoding="utf-8")
    (work / ".env").write_text("WORK_KEY=work\nSHARED_KEY=work\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setenv("LAUNCH_ENV_ONLY", "env-only")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(policy, "_snapshot", None)
    if request.param:
        policy.activate_multi_profile_hosting()
    try:
        yield launch, work, request.param
    finally:
        set_multiplex_active(False)


def _secrets() -> dict:
    return {k: get_secret(k) for k in ("LAUNCH_FILE_KEY", "LAUNCH_ENV_ONLY", "WORK_KEY", "SHARED_KEY")}


def test_a_secondary_home_gets_only_its_own_files(homes):
    _, work, _ = homes
    with policy.profile_config_scope(work):
        assert get_hermes_home() == work
        assert current_secret_scope_home() == str(work)
        assert _secrets() == {"LAUNCH_FILE_KEY": None, "LAUNCH_ENV_ONLY": None, "WORK_KEY": "work", "SHARED_KEY": "work"}
        assert (get_terminal_scope() or {}).get("TERMINAL_ENV") == "docker"
    assert (get_hermes_home_override(), current_secret_scope(), get_terminal_scope()) == (None, None, None)


def test_the_launch_home_inside_a_secondary_scope_is_the_launch_home(homes):
    launch, work, _ = homes
    with policy.profile_config_scope(work), policy.profile_config_scope(launch):
        assert get_hermes_home() == launch
        assert current_secret_scope_home() is None
        assert _secrets() == {"LAUNCH_FILE_KEY": "launch-file", "LAUNCH_ENV_ONLY": "env-only", "WORK_KEY": None,
                              "SHARED_KEY": "launch"}
        assert (get_terminal_scope() or {}).get("TERMINAL_ENV") != "docker"
    assert (get_hermes_home_override(), current_secret_scope(), get_terminal_scope()) == (None, None, None)


def test_the_launch_home_keeps_the_launch_rules(homes):
    launch, _, multiplex = homes
    with policy.profile_config_scope(launch):
        assert current_secret_scope() is not None
        assert current_secret_scope_home() is None
        assert _secrets() == {"LAUNCH_FILE_KEY": "launch-file", "LAUNCH_ENV_ONLY": "env-only", "WORK_KEY": None,
                              "SHARED_KEY": "launch"}
        if multiplex:
            assert get_hermes_home_override() == str(launch)
            assert (get_terminal_scope() or {}).get("TERMINAL_ENV") == "local"
        else:
            assert (get_hermes_home_override(), get_terminal_scope()) == (None, None)
    assert (get_hermes_home_override(), current_secret_scope(), get_terminal_scope()) == (None, None, None)


@pytest.mark.parametrize("bound", ["launch", "work"])
def test_the_launch_home_inside_a_bound_scope_binds_the_launch_scope(homes, bound):
    """A kanban dispatch or scoped gateway turn binds a whole profile scope (``bind_profile_scope``);
    unbinding its terminal policy is refused there, so the launch home binds its own scope."""
    from agent.runtime_scope import bind_profile_scope
    from hermes_scope import current_scope

    launch, work, multiplex = homes
    frozen_before = policy._snapshot
    with bind_profile_scope(launch if bound == "launch" else work, hydrate_secrets=False) as outer:
        with policy.profile_config_scope(launch, launch_home=launch):
            assert Path(current_scope().agent) == launch
            assert get_hermes_home() == launch
            assert current_secret_scope_home() is None
            assert _secrets() == {"LAUNCH_FILE_KEY": "launch-file", "LAUNCH_ENV_ONLY": "env-only", "WORK_KEY": None,
                                  "SHARED_KEY": "launch"}
            assert (get_terminal_scope() or {}).get("TERMINAL_ENV") == "local"
        assert current_scope() == outer
        assert get_hermes_home() == (launch if bound == "launch" else work)
    assert (get_hermes_home_override(), current_secret_scope(), get_terminal_scope()) == (None, None, None)
    # Neither bind_profile_scope(launch) (``_bind_whole_profile`` -> ``launch_terminal_env()``) nor the
    # nested launch scope freezes a single-profile host's launch env early.
    if not multiplex:
        assert policy._snapshot is frozen_before is None


def test_launch_terminal_env_reads_the_live_env_until_multiplexing(homes, monkeypatch):
    """Single-profile: the live ``TERMINAL_*`` env, nothing frozen (a later startup bridge still
    counts). Multiplexing: the snapshot frozen at activation, never re-read. [daveey]"""
    _, _, multiplex = homes

    def overlay() -> dict:
        env = policy.launch_terminal_env()
        assert all(k.startswith("TERMINAL_") for k in env)
        return {k: env.get(k) for k in ("TERMINAL_ENV", "TERMINAL_SSH_HOST")}

    assert overlay()["TERMINAL_ENV"] == "local"
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "bridged-late")
    if multiplex:
        assert overlay()["TERMINAL_ENV"] == "local"
        assert overlay()["TERMINAL_SSH_HOST"] != "bridged-late"
        assert policy._snapshot is not None
    else:
        assert overlay() == {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "bridged-late"}
        assert policy._snapshot is None


def test_the_launch_home_inside_a_bound_launch_turn_keeps_the_turn(homes):
    """``scope_for_profile`` returns the bound scope when it already is the launch home, so the
    turn's session, turn id and fence survive the nested launch bind."""
    from agent.runtime_scope import bind_profile_scope, bind_turn
    from hermes_scope import Fence, TurnScope, current_scope

    launch, _, _ = homes
    with bind_profile_scope(launch, hydrate_secrets=False), bind_turn("session-1", "turn-1", Fence(1, 1)) as turn:
        assert isinstance(turn, TurnScope)
        with policy.profile_config_scope(launch, launch_home=launch):
            assert current_scope() == turn
            assert get_hermes_home() == launch
        assert current_scope() == turn
    assert current_scope() is None


def test_a_secondary_home_inside_a_bound_launch_scope_gets_only_its_own_files(homes):
    from agent.runtime_scope import bind_profile_scope

    launch, work, _ = homes
    with bind_profile_scope(launch, hydrate_secrets=False):
        with policy.profile_config_scope(work, launch_home=launch):
            assert get_hermes_home() == work
            assert _secrets() == {"LAUNCH_FILE_KEY": None, "LAUNCH_ENV_ONLY": None, "WORK_KEY": "work", "SHARED_KEY": "work"}
            assert (get_terminal_scope() or {}).get("TERMINAL_ENV") == "docker"
        assert get_hermes_home() == launch
