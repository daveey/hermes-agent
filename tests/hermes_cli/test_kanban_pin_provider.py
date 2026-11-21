"""A pinned concrete model is bound to a provider at pin time, never late-bound to config.yaml [daveey].

Regression for run 5406 of t_01a1800921b1ff50: ``model_override=claude-opus-5-5`` with ``provider_override``
NULL ran on anthropic twice, then crashed (exit 78, OpenRouter HTTP 400) while the profile config briefly said
``model.default: class:deep`` / ``provider: auto``. Entry paths covered: ``kb.create_task``, the
``kanban_create`` tool, ``hermes kanban create --model``, ``hermes kanban set-model``, and the dispatcher's
legacy-row path (``apply_model_tier`` + worker argv).
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_model_tier as tier
from hermes_cli import models_detect
from hermes_cli.kanban_pin_provider import PinProviderUnresolvedError, resolve_pin_provider

OPUS = "claude-opus-5-5"
AUTO_DEEP = "model:\n  default: class:deep\n  provider: auto\n"
AUTO_ONLY = "model:\n  default: claude-opus-5-5\n  provider: auto\n"


def _home(tmp_path, monkeypatch, config: str, creds=("anthropic",)):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(config)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_SESSION_ID", "HERMES_SESSION_KEY", "HERMES_INFERENCE_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    authed = set(creds)
    monkeypatch.setattr(models_detect, "provider_has_credentials", lambda pid: pid in authed)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


@pytest.fixture(params=[AUTO_DEEP, AUTO_ONLY], ids=["class-deep-auto", "auto"])
def auto_home(tmp_path, monkeypatch, request):
    return _home(tmp_path, monkeypatch, request.param)


def _argv(monkeypatch, tmp_path, task):
    """(model, provider) the profile owner runs *task* with. Workers are ``-m hermes_cli.kanban_worker_client``
    (upstream) and the owner builds the turn's policy from ``owner_model_pin`` (gateway.session_kanban), so the
    pin no longer rides the worker argv."""
    with kbc.connect_closing() as conn:
        return tier.owner_model_pin(conn, task)


def _parser():
    from hermes_cli import kanban as cli
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers())
    return cli, parser


# --- create paths -----------------------------------------------------------------------------------------

def test_create_task_binds_anthropic_under_auto_config(auto_home, monkeypatch, tmp_path):
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError, match='model-neutral'):
            kb.create_task(conn, title='t', assignee='default', model_override=OPUS)
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0


def test_kanban_create_tool_binds_anthropic(auto_home):
    from tools import kanban_tools as tools
    out = json.loads(tools._handle_create({"title": "t", "assignee": "default", "model": OPUS}))
    assert 'model-neutral' in out['error']
    with kbc.connect_closing() as conn:
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0


def test_cli_create_and_set_model_bind_anthropic(auto_home, monkeypatch):
    cli, parser = _parser()
    monkeypatch.setattr(cli, "_check_dispatcher_presence", lambda: (True, ""))
    assert cli._cmd_create(parser.parse_args(["kanban", "create", "via cli", "--assignee", "default",
                                              "--model", OPUS])) == 2
    with kbc.connect_closing() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        plain = kb.create_task(conn, title="plain", assignee="default")
    assert cli._cmd_set_model(parser.parse_args(["kanban", "set-model", plain, OPUS])) == 2
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, plain)
        assert (task.model_override, task.provider_override) == (None, None)



# --- refusals ---------------------------------------------------------------------------------------------

def test_unknown_model_is_refused_everywhere(auto_home, monkeypatch, capsys):
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError, match='model-neutral') as exc:
            kb.create_task(conn, title="t", assignee="default", model_override="no-such-model-zz9")
        assert 'concrete model/provider' in str(exc.value)
        tid = kb.create_task(conn, title="keep", assignee="default")
        with pytest.raises(ValueError, match='model-neutral'):
            kb.set_model_override(conn, tid, "no-such-model-zz9")
        assert kb.get_task(conn, tid).model_override is None
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    from tools import kanban_tools as tools
    out = json.loads(tools._handle_create({"title": "t", "assignee": "default", "model": "no-such-model-zz9"}))
    assert 'model-neutral' in out['error'], out
    cli, parser = _parser()
    monkeypatch.setattr(cli, "_check_dispatcher_presence", lambda: (True, ""))
    assert cli._cmd_create(parser.parse_args(["kanban", "create", "x", "--model", "no-such-model-zz9"])) == 2
    assert 'model-neutral' in capsys.readouterr().err


def test_ambiguous_model_is_refused_naming_the_providers(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, AUTO_DEEP, creds=("openai-api", "openai-codex"))
    with pytest.raises(PinProviderUnresolvedError) as exc:
        resolve_pin_provider("gpt-5.5", None, assignee="default")
    assert set(exc.value.providers) == {"openai-api", "openai-codex"}
    assert "openai-api" in str(exc.value) and "openai-codex" in str(exc.value)


def test_configured_provider_breaks_the_tie(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, "model:\n  default: gpt-5.5\n  provider: openai-codex\n",
          creds=("openai-api", "openai-codex"))
    assert resolve_pin_provider("gpt-5.5", None, assignee="default") == ("gpt-5.5", "openai-codex")


def test_custom_profile_provider_serves_its_private_model(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, "model:\n  default: my-local-llm\n  provider: custom:lab\n", creds=("custom:lab",))
    assert resolve_pin_provider("my-local-llm", None, assignee="default") == ("my-local-llm", "custom:lab")


def test_profile_provider_live_catalog_serves_an_uncatalogued_id(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, "model:\n  default: x\n  provider: nous\n", creds=("nous",))
    monkeypatch.setattr(models_detect, "current_provider_catalog_match",
                        lambda name, prov: name if prov == "nous" and name == "portal-only-zz1" else None)
    assert resolve_pin_provider("portal-only-zz1", None, assignee="default") == ("portal-only-zz1", "nous")


def test_no_credentials_refuses_and_names_the_serving_provider(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, AUTO_DEEP, creds=())
    with pytest.raises(PinProviderUnresolvedError) as exc:
        resolve_pin_provider(OPUS, None, assignee="default")
    assert exc.value.providers == ["anthropic"]


def test_explicit_provider_is_kept(auto_home):
    assert resolve_pin_provider(OPUS, "openrouter") == (OPUS, "openrouter")


# --- class selectors unchanged ----------------------------------------------------------------------------

def test_class_selector_keeps_provider_auto(auto_home, monkeypatch, tmp_path):
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, kb.create_task(conn, title="t", assignee="default", model_override="class:deep"))
    assert (task.model_override, task.provider_override) == ('class:deep', 'auto')
    assert _argv(monkeypatch, tmp_path, task) == ("class:deep", "auto")


# --- dispatcher: legacy rows ------------------------------------------------------------------------------

def test_dispatcher_binds_legacy_null_provider_row(auto_home, monkeypatch, tmp_path):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="legacy", assignee="default")
        conn.execute("UPDATE tasks SET model_override=?, provider_override=NULL WHERE id=?", (OPUS, tid))
        conn.commit()
        task = kb.claim_task(conn, tid)
        assert tier.apply_model_tier(conn, task, lane='ready', cfg={}).startswith('default')
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='model_tier'",
                          (tid,)).fetchone()
    assert json.loads(ev[0])['reason'].startswith('default')
    assert _argv(monkeypatch, tmp_path, task) == ('class:standard', 'auto')
