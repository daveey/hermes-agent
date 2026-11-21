"""Source-policy reuse retains payloads without retaining config across calls."""

import copy

import pytest

from hermes_cli import config, session_listing as listing
from hermes_constants import get_hermes_home


def test_policy_reads_each_home_once_and_preserves_stamped_payload(monkeypatch, tmp_path):
    root = tmp_path / "home"
    root.mkdir()
    (root / "config.yaml").write_text(
        "sessions:\n  automated_sources: ' EVAL, cron '\n"
        "  interactive_sources: [desktop, CLI]\n  sidebar_profiles: [default]\n"
    )
    original = config.load_config_readonly
    calls = []

    def counted():
        calls.append(str(get_hermes_home()))
        return original()

    monkeypatch.setattr(config, "load_config_readonly", counted)
    rows = [
        {"id": "human", "source": "local", "created_source": "desktop"},
        {"id": "bot", "source": "eval"},
        {"id": "unknown", "source": "new-bot"},
        {"id": "legacy", "source": None},
    ]
    expected = listing.stamp_automated(
        copy.deepcopy(rows), automated_sources=listing.AutomatedSourcePolicy(
            ["eval", "cron"], ["desktop", "cli"]
        )
    )
    assert listing.stamp_automated(copy.deepcopy(rows), root) == expected
    assert calls == [str(root)]


def test_fresh_policies_observe_file_env_and_root_edits(monkeypatch, tmp_path):
    root = tmp_path / "home"
    named = root / "profiles" / "bot"
    named.mkdir(parents=True)
    (root / "config.yaml").write_text("sessions:\n  sidebar_profiles: [default]\n")
    path = named / "config.yaml"
    path.write_text("sessions:\n  interactive_sources: '${D2_HUMAN_SOURCES}'\n")
    monkeypatch.setenv("D2_HUMAN_SOURCES", "desktop,cli")
    first = listing.automated_session_sources(named)
    assert first.interactive == ["desktop", "cli"]
    first.interactive.append("eval")
    first.append("*")
    second = listing.automated_session_sources(named)
    assert not list(second)
    assert second.interactive == ["desktop", "cli"]
    monkeypatch.setenv("D2_HUMAN_SOURCES", "cli")
    assert listing.automated_session_sources(named).interactive == ["cli"]
    path.write_text("{}\n")
    assert listing.automated_session_sources(named) == ["*"]
    (root / "config.yaml").write_text("sessions:\n  sidebar_profiles: [default, bot]\n")
    assert not listing.automated_session_sources(named)
    path.unlink()
    assert not listing.automated_session_sources(named)


def test_unreadable_config_fails_open_for_single_and_multi_key(monkeypatch, tmp_path):
    prior_home = get_hermes_home()
    root = tmp_path / "home"
    named = root / "profiles" / "bot"
    named.mkdir(parents=True)

    def broken():
        raise RuntimeError("probe: unreadable config")

    monkeypatch.setattr(config, "load_config_readonly", broken)
    assert listing._sessions_source_list(root, "interactive_sources") == []
    assert listing._sessions_source_list(root, ("automated_sources", "sidebar_profiles")) == ([], [])
    for home in (root, named):
        policy = listing.automated_session_sources(home)
        assert isinstance(policy, listing.AutomatedSourcePolicy)
        assert not policy and list(policy) == [] and policy.interactive == []
    rows = listing.stamp_automated([{"id": "a", "source": "eval"}], root)
    assert rows == [{"id": "a", "source": "eval", "automated": False}]
    assert get_hermes_home() == prior_home


def test_errors_restore_home_and_do_not_memoize_fallback(monkeypatch, tmp_path):
    from hermes_cli.config_backend import ConfigBackendUnavailable

    prior_home = get_hermes_home()
    home = tmp_path / "profile"
    with monkeypatch.context() as patch:
        def unavailable():
            raise ConfigBackendUnavailable("D2 synthetic unavailable")
        patch.setattr(config, "load_config_readonly", unavailable)
        with pytest.raises(ConfigBackendUnavailable, match="D2 synthetic unavailable"):
            listing.automated_session_sources(home)
    assert get_hermes_home() == prior_home
    with monkeypatch.context() as patch:
        patch.setattr(config, "load_config_readonly", lambda: {"sessions": None})
        assert not listing.automated_session_sources(home)
    with monkeypatch.context() as patch:
        patch.setattr(config, "load_config_readonly", lambda: {"sessions": {"automated_sources": ["EVAL"]}})
        assert listing.automated_session_sources(home) == ["eval"]
    assert get_hermes_home() == prior_home
