"""Unreadable assignee configuration refuses one card, not sibling dispatch."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_db_dispatch import dispatch_once
from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(h))
    (h / "config.yaml").write_text(_managed_yaml())
    kb.init_db()
    return h


@pytest.mark.parametrize("bad", ["model_routing: [unclosed\n", "- just\n- a list\n"], ids=["yaml_error", "non_mapping"])
def test_a_broken_profile_config_does_not_stop_sibling_cards(home, bad):
    profile = home / "profiles" / "broken"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(bad)
    with kbc.connect_closing() as conn:
        first = kb.create_task(conn, title="card of the broken profile", assignee="broken", priority=100)
        second = kb.create_task(conn, title="ordinary card", assignee="default", priority=1)
        launched = []
        try:
            result = dispatch_once(conn, spawn_fn=lambda task, *a, **k: launched.append(task.id))
        except Exception as exc:
            pytest.fail(f"dispatch tick raised {type(exc).__name__}: {exc}; launched={launched}")
        print("SKIPPED", result.skipped_ineligible, "LAUNCHED", launched)
        assert second in launched, (launched, result.skipped_ineligible)
        assert (first, 'model_unroutable:config_unavailable') in result.skipped_ineligible
        task = kb.get_task(conn, first)
        assert task is not None
        assert task.status == 'ready'
        assert task.current_run_id is None
        assert task.consecutive_failures == 0
        assert kb.list_runs(conn, first) == []
        assert first not in launched
        # The refusal is a fresh config read, not a sticky persisted hold.
        (profile / 'config.yaml').write_text(_managed_yaml())
        dispatch_once(conn, spawn_fn=lambda task, *a, **k: launched.append(task.id))
        assert first in launched
        assert len(kb.list_runs(conn, first)) == 1
