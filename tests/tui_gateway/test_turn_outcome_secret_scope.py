"""Post-turn refresh owns its credential scope after the turn released its own."""
from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor

import pytest

import hermes_state_outcome as so
from agent import secret_scope
from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
from tui_gateway import server
from tui_gateway.turn_outcome import refresh_turn_outcome
from tests.tui_gateway import test_turn_outcome_refresh as helpers

# Reuse the real store and hook-cleanup fixture, not a fake derivation.
db = helpers.db


@pytest.mark.parametrize("foreign", [False, True])
@pytest.mark.parametrize("judge_fails", [False, True])
def test_post_turn_refresh_scopes_judge_and_restores_caller(monkeypatch, tmp_path, db, foreign, judge_fails):
    launch_home = tmp_path / "launch"
    launch_home.mkdir()
    (launch_home / ".env").write_text("ANTHROPIC_TOKEN=fixture-launch\n", encoding="utf-8")
    home = tmp_path / "foreign" if foreign else launch_home
    expected = "fixture-foreign" if foreign else "fixture-launch"
    if foreign:
        home.mkdir()
        (home / ".env").write_text("ANTHROPIC_TOKEN=fixture-foreign\n", encoding="utf-8")
    monkeypatch.setattr(server, "_launch_home", lambda: launch_home)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    # Store selection has separate coverage; keep both profiles in this owned fixture store.
    from contextlib import nullcontext
    monkeypatch.setattr(server, "_session_db", lambda session: nullcontext(db))
    monkeypatch.setenv("ANTHROPIC_TOKEN", "fixture-wrong-process")
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    token = secret_scope.set_secret_scope(None)
    seen = []

    def credential_read():
        value = secret_scope.get_secret("ANTHROPIC_TOKEN")
        seen.append((value, get_hermes_home()))
        if judge_fails:
            raise RuntimeError("controlled judge failure")
        return True

    def judge(sid, text, **kwargs):
        # Same context-copy/thread boundary as session-outcome._with_budget.
        ctx = contextvars.copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(ctx.run, credential_read).result()

    db.create_session("post-turn", source="desktop")
    db.append_message("post-turn", role="user", content="finish the check")
    db.append_message("post-turn", role="assistant", content=helpers.SUMMARY)
    so.register_outcome_judge(judge)
    # Match the post-turn boundary: home still bound, secret already reset.
    home_token = set_hermes_home_override(str(home))
    try:
        with pytest.raises(secret_scope.UnscopedSecretError):
            secret_scope.get_secret("ANTHROPIC_TOKEN")
        before_home = get_hermes_home()
        refresh_turn_outcome({"session_key": "post-turn", "profile_home": str(home) if foreign else None})
        assert seen == [(expected, home)]
        assert secret_scope.current_secret_scope() is None
        assert get_hermes_home() == before_home
        with pytest.raises(secret_scope.UnscopedSecretError):
            secret_scope.get_secret("ANTHROPIC_TOKEN")
        stored = so.get_outcome("post-turn", db=db)
        assert stored is not None
        assert bool(stored.get("judged")) is not judge_fails
    finally:
        reset_hermes_home_override(home_token)
        secret_scope.reset_secret_scope(token)
