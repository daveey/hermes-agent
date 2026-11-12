"""The outcome judge runs under the owning profile's secret scope even when its caller bound none.

Notice writers (``tui_gateway.kanban_unloaded`` / ``kanban_remote_notices``), orphan reap, completion notes
and the web refresh all call ``refresh_outcome[s_quietly]`` from sweeper threads or request handlers whose
context carries no profile scope. On a multiplexed gateway the judge's provider read then failed with
``UnscopedSecretError`` at 0.0 s (hub agent.log, 2026-11-12 16:23 / 17:00, t_01a1d0d08799272e)."""
from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor

import pytest

import hermes_state_outcome as so
from agent import secret_scope
from hermes_constants import get_hermes_home, get_hermes_home_override
from hermes_state import SessionDB

SUMMARY = "Fixed the flaky test by waiting on the event instead of sleeping; all 12 tests pass now."


@pytest.fixture
def owner(tmp_path, monkeypatch):
    """A profile home that owns the chat: its own ``.env`` and ``state.db``; the process env holds another value."""
    so._reset_hooks_for_tests()
    home = tmp_path / "profiles" / "owner"
    home.mkdir(parents=True)
    (home / ".env").write_text("ANTHROPIC_TOKEN=fixture-owner\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_TOKEN", "fixture-wrong-process")
    monkeypatch.setattr(so, "_card_items", lambda ids: ([], []))  # no board in this fixture
    db = SessionDB(db_path=home / "state.db")
    db.create_session("chat", source="desktop")
    db.append_message("chat", role="user", content="please fix the flaky test")
    db.append_message("chat", role="assistant", content=SUMMARY)
    yield home, db
    db.close()
    so._reset_hooks_for_tests()


def _register_reading_judge(seen: list, *, fail: bool = False) -> None:
    def read():
        seen.append((secret_scope.get_secret("ANTHROPIC_TOKEN"), get_hermes_home()))
        if fail:
            raise RuntimeError("controlled judge failure")
        return True

    def judge(sid, text, **kwargs):
        # The plugin's _with_budget boundary: a copy of the caller's context on another thread.
        ctx = contextvars.copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(ctx.run, read).result()

    so.register_outcome_judge(judge)


@pytest.mark.parametrize("judge_fails", [False, True])
def test_unscoped_refresh_binds_owner_scope_for_judge(owner, monkeypatch, judge_fails):
    home, db = owner
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    seen: list = []
    _register_reading_judge(seen, fail=judge_fails)

    def notice_path():  # what a sweeper thread does after writing a passive notice row
        so.refresh_outcomes_quietly(["chat"], "notice", db=db)
        return secret_scope.current_secret_scope(), get_hermes_home_override()

    after = contextvars.Context().run(notice_path)  # an empty context: no scope, no home override
    assert seen == [("fixture-owner", home)]
    assert after == (None, None)  # nothing leaks back into the caller's context
    stored = so.get_outcome("chat", db=db)
    assert bool(stored.get("judged")) is not judge_fails


def test_derive_outcome_binds_owner_scope_for_judge(owner, monkeypatch):
    home, db = owner
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    seen: list = []
    _register_reading_judge(seen)
    out = contextvars.Context().run(lambda: so.derive_outcome("chat", db=db))
    assert out["kind"] == "done" and out.get("judged")
    assert seen == [("fixture-owner", home)]


def test_bound_caller_scope_is_kept(owner, monkeypatch):
    """A caller that already bound a scope (the post-turn refresh) keeps it: no rebinding over it."""
    home, db = owner
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    seen: list = []
    _register_reading_judge(seen)

    def bound():
        token = secret_scope.set_secret_scope({"ANTHROPIC_TOKEN": "fixture-caller"}, profile_home=str(home))
        try:
            so.refresh_outcome("chat", "turn_end", db=db)
            return secret_scope.current_secret_scope()
        finally:
            secret_scope.reset_secret_scope(token)

    assert contextvars.Context().run(bound) == {"ANTHROPIC_TOKEN": "fixture-caller"}
    assert [value for value, _home in seen] == ["fixture-caller"]


def test_caller_home_override_is_restored(owner, monkeypatch, tmp_path):
    """The post-turn shape (home override still bound, secret already reset): the judge runs as the chat's owner
    and the caller's own override comes back afterwards."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home, db = owner
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    seen: list = []
    _register_reading_judge(seen)
    other = tmp_path / "caller"

    def caller():
        token = set_hermes_home_override(str(other))
        try:
            so.refresh_outcome("chat", "turn_end", db=db)
            return get_hermes_home_override(), secret_scope.current_secret_scope()
        finally:
            reset_hermes_home_override(token)

    assert contextvars.Context().run(caller) == (str(other), None)
    assert seen == [("fixture-owner", home)]


def test_handle_without_a_path_binds_nothing(monkeypatch):
    """A store handle with no ``db_path`` names no owner: the judge runs in the caller's context unchanged."""
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)

    def probe():
        with so._owner_scope(object()):
            return secret_scope.current_secret_scope(), get_hermes_home_override()

    assert contextvars.Context().run(probe) == (None, None)


def test_single_profile_process_binds_nothing(owner, monkeypatch):
    """Without multiplexing an unscoped read keeps process-env semantics; the judge sees no new scope."""
    _home, db = owner
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", False)
    scopes: list = []

    def judge(sid, text, **kwargs):
        scopes.append((secret_scope.current_secret_scope(), get_hermes_home_override()))
        return secret_scope.get_secret("ANTHROPIC_TOKEN") == "fixture-wrong-process"

    so.register_outcome_judge(judge)
    out = contextvars.Context().run(lambda: so.refresh_outcome("chat", "manual", db=db))
    assert scopes == [(None, None)]
    assert out["kind"] == "done" and out.get("judged")
