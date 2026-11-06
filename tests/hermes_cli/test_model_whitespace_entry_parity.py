"""Every entry point that takes a model id refuses the same spaced ids [daveey].

``/model`` (validate_requested_model) has refused "a b" on cloud providers for a long time. The
startup ``-m``/``--model`` flag (chat, TUI, ``-z`` one-shot) and Kanban ``--model`` pins did not, so a
shell loop that failed to split a list sent ``-m "claude-opus-5-5 openai-codex/gpt-6-astra ..."`` to
Anthropic (404 x3) or, on a host with a fallback chain, "passed" on a different model. This file runs
the same bad/good ids through each entry point so they cannot drift apart again.
"""
from argparse import Namespace
from pathlib import Path
import sys
import types

import pytest

SPACES_MSG = "Model names cannot contain spaces"

# (model, provider) a cloud provider can never serve.
BAD = [
    ("claude-opus-5-5 openai-codex/gpt-6-astra xai-oauth/grok-4.7", "anthropic"),
    ("a b", "anthropic"),
    ("claude\topus", "anthropic"),
    ("gpt 5.4", "openai-codex"),
]
# Self-hosted / user endpoints really do serve spaced ids: every entry point must keep accepting them.
GOOD_LOCAL = [
    ("My Model", "custom", "http://localhost:8000/v1"),
    ("Meta Llama 3.1 8B", "ollama", "http://127.0.0.1:11434/v1"),
]


# ── /model ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("model,provider", BAD)
def test_slash_model_rejects(model, provider):
    from hermes_cli.models_validate import validate_requested_model

    result = validate_requested_model(model, provider)
    assert result["accepted"] is False
    assert SPACES_MSG in result["message"]


@pytest.mark.parametrize("model,provider,base_url", GOOD_LOCAL)
def test_shared_helper_allows_local(model, provider, base_url):
    from hermes_cli.models_validate import model_whitespace_error

    assert model_whitespace_error(model, provider, base_url) is None


@pytest.mark.parametrize("model,provider", BAD)
def test_shared_helper_rejects(model, provider):
    from hermes_cli.models_validate import model_whitespace_error

    assert SPACES_MSG in (model_whitespace_error(model, provider) or "")


# ── startup -m / --model (chat, TUI, -z) ────────────────────────────────

def _chat_args(**overrides):
    base = dict(
        continue_last=None, model=None, provider=None, resume=None, no_restore_cwd=False,
        toolsets=None, skills=None, tui=False, tui_dev=False, cli=True, verbose=None,
        quiet=True, query="Reply with exactly ROUTE_OK", image=None, worktree=False,
        checkpoints=False, pass_session_id=False, max_turns=None, ignore_rules=False,
        ignore_user_config=False, safe_mode=False, compact=False, source=None, yolo=False,
        accept_hooks=False,
    )
    base.update(overrides)
    return Namespace(**base)


@pytest.fixture
def main_mod(monkeypatch):
    import hermes_cli.main as mod

    monkeypatch.setattr(mod, "_has_any_provider_configured", lambda: True)
    monkeypatch.setattr(mod, "_sync_bundled_skills_for_startup", lambda: None)
    monkeypatch.setattr(mod, "_pin_kanban_board_env", lambda: None)
    monkeypatch.setattr(mod, "_resolve_session_by_name_or_id", lambda val: val)

    class _NoTTY:
        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdin", _NoTTY())
    return mod


@pytest.fixture
def fake_cli(monkeypatch):
    """Stands in for cli.main: anything captured here means an agent (and its HTTP client) was built."""
    captured = {}
    monkeypatch.setitem(sys.modules, "cli", types.SimpleNamespace(main=lambda **kw: captured.update(kw)))
    return captured


def _config(monkeypatch, provider="anthropic", base_url=""):
    model = {"provider": provider, "default": "claude-opus-5-5"}
    if base_url:
        model["base_url"] = base_url
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"model": model})


@pytest.fixture
def no_http(monkeypatch):
    """Any Anthropic client construction fails the test: the refusal must come before the first call."""
    def _boom(*a, **k):
        raise AssertionError("an Anthropic client was built for a spaced model id")

    import agent.anthropic_adapter as adapter
    monkeypatch.setattr(adapter, "build_anthropic_client", _boom, raising=False)


@pytest.mark.parametrize("model,provider", BAD)
def test_cmd_chat_m_rejects_before_any_call(main_mod, fake_cli, no_http, monkeypatch, capsys, model, provider):
    _config(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        main_mod.cmd_chat(_chat_args(model=model, provider=provider))
    assert exc.value.code == 2
    assert not fake_cli
    err = capsys.readouterr().err
    assert "Model names cannot contain spaces. Pass one model per run." in err


def test_cmd_chat_m_rejects_with_config_provider(main_mod, fake_cli, no_http, monkeypatch, capsys):
    """No --provider: the configured cloud provider decides (the MacBook incident's shape)."""
    _config(monkeypatch, provider="anthropic")
    with pytest.raises(SystemExit) as exc:
        main_mod.cmd_chat(_chat_args(model=BAD[0][0]))
    assert exc.value.code == 2
    assert not fake_cli


def test_oneshot_z_rejects(main_mod, no_http, monkeypatch, capsys):
    _config(monkeypatch)
    called = []
    # -z runs through launch_gateway_chat; its startup -m guard must refuse before the gateway launch.
    monkeypatch.setattr("hermes_cli.gateway_chat.launch_from_args", lambda *a, **k: called.append(1) or 0)
    args = _chat_args(model="a b", provider="anthropic")
    args.oneshot = "hi"
    with pytest.raises(SystemExit) as exc:
        main_mod._run_oneshot_from_args(args)
    assert exc.value.code == 2
    assert not called


@pytest.mark.parametrize("model,provider,base_url", GOOD_LOCAL)
def test_cmd_chat_m_accepts_local_spaced_id(main_mod, fake_cli, monkeypatch, model, provider, base_url):
    _config(monkeypatch, provider=provider, base_url=base_url)
    main_mod.cmd_chat(_chat_args(model=model, provider=provider))
    assert fake_cli["model"] == model


# ── Kanban --model pins ─────────────────────────────────────────────────

@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text("kanban:\n  orchestrator_profile: supervisor\n")
    for name in ("worker", "supervisor"):
        profile = home / "profiles" / name
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text(
            "model:\n  default: claude-opus-5-5\n  provider: anthropic\n"
            "platform_toolsets:\n  cli: [terminal, file, kanban]\nterminal:\n  backend: local\n")
    from hermes_cli import kanban_db as kb

    kb.init_db()
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as conn:
        yield conn


@pytest.mark.parametrize("model,provider", BAD)
def test_kanban_create_rejects(board, model, provider):
    from hermes_cli import kanban_db as kb

    with pytest.raises(ValueError, match=SPACES_MSG):
        kb.create_task(board, title="probe", assignee="worker", model_override=model, provider_override=provider)
    assert board.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


@pytest.mark.parametrize("model,provider", BAD)
def test_kanban_set_model_rejects(board, model, provider):
    from hermes_cli import kanban_db as kb

    tid = kb.create_task(board, title="probe", assignee="worker")
    with pytest.raises(ValueError, match=SPACES_MSG):
        kb.set_model_override(board, tid, model, provider)
    assert kb.get_task(board, tid).model_override is None


def test_kanban_create_accepts_custom_spaced_id(board):
    from hermes_cli import kanban_db as kb

    tid = kb.create_task(board, title="probe", assignee="worker", model_override="My Model",
                         provider_override="custom")
    assert kb.get_task(board, tid).model_override == "My Model"
