"""An Anthropic ``credits_required`` 429 benches the model until the account's weekly reset. [daveey]

MacBook Desktop, 2026-11-20: every Claude Max account had spent its Fable allowance. The last one
(azaraybloomin) answered ``claude-fable-5-2`` with 429 ``Usage credits are required for this model``
(``details.error_code = credits_required``) and no reset the pool could use, so the default one-hour
bench applied. The pinned chat then read "quota is out until 17:19" and the pool kept retrying an
account that could not serve Fable before its weekly window reopened.

Real ``agent.credential_pool`` against a real temp auth store and the real in-process usage cache.
"""
import json
import time
from datetime import datetime, timedelta, timezone, UTC

import pytest

KEY = "sk-ant-oat01-synthetic-test-token-0000"
FABLE = "claude-fable-5-2"
OPUS = "claude-opus-5-5"
CREDITS_BODY = {"type": "error", "error": {
    "type": "rate_limit_error", "message": "Usage credits are required for this model.",
    "details": {"error_code": "credits_required", "model": FABLE, "exhausted_included_allowance": False}}}


@pytest.fixture
def pool(tmp_path, monkeypatch):
    root = tmp_path / "hermes-root"
    root.mkdir()
    (tmp_path / "fakehome").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "fakehome"))
    for var in ("ANTHROPIC_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(root))
    import hermes_constants
    hermes_constants._default_hermes_root_memo = None  # type: ignore[attr-defined]
    (root / "auth.json").write_text(json.dumps({"credential_pool": {"anthropic": [
        {"id": "seat", "label": "seat", "auth_type": "api_key", "priority": 0, "source": "manual", "access_token": KEY},
        {"id": "other", "label": "other", "auth_type": "api_key", "priority": 1, "source": "manual",
         "access_token": KEY + "-other"},
    ]}}))
    from agent import account_usage_cache
    monkeypatch.setattr(account_usage_cache, "_snapshots", {})
    monkeypatch.setattr(account_usage_cache, "_started", {})
    from agent.credential_pool import load_pool
    return load_pool("anthropic")


def _remember_weekly(pool, entry_id, weekly_reset, session_reset=None, model_week=None):
    """The windows ``_fetch_anthropic_account_usage`` produces: session, account week, model weeks."""
    from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
    from agent.account_usage_cache import _identity_id_for, remember_account_usage
    entry = next(e for e in pool.entries() if e.id == entry_id)
    windows = [AccountUsageWindow("Current week", 60.0, weekly_reset)]
    if model_week is not None:  # an Opus week is the model's, never the account's
        windows.insert(0, AccountUsageWindow("Opus week", 90.0, model_week, scope="model"))
    if session_reset is not None:
        windows.insert(0, AccountUsageWindow("Current session (5h)", 10.0, session_reset))
    snapshot = AccountUsageSnapshot("anthropic", "test", datetime.now(UTC), windows=tuple(windows))
    remember_account_usage("anthropic", snapshot, identity_id=_identity_id_for("anthropic", entry))


def _error(body):
    class _Err(Exception):
        pass
    err = _Err("Error code: 429")
    err.body = body
    err.response = None
    return err


def test_error_context_keeps_the_anthropic_error_code():
    from agent.agent_runtime_helpers import extract_api_error_context
    ctx = extract_api_error_context(_error(CREDITS_BODY))
    assert ctx["error_code"] == "credits_required"
    assert ctx["message"] == "Usage credits are required for this model."


def test_credits_required_benches_the_model_until_the_weekly_reset(pool):
    weekly = datetime.now(UTC) + timedelta(days=3)
    _remember_weekly(pool, "seat", weekly, session_reset=datetime.now(UTC) + timedelta(hours=2))
    from agent.agent_runtime_helpers import extract_api_error_context
    ctx = {**extract_api_error_context(_error(CREDITS_BODY)), "reset_at": time.time() + 3600}

    nxt = pool.mark_exhausted_and_rotate(status_code=429, error_context=ctx, credential_id="seat",
                                         failure_reason="rate_limit", model=FABLE)

    seat = next(e for e in pool.entries() if e.id == "seat")
    assert seat.model_cooldowns[FABLE] == pytest.approx(weekly.timestamp(), abs=2)
    assert OPUS not in (seat.model_cooldowns or {})  # the credential stays good for other models
    assert seat.last_status is None
    assert nxt is not None and nxt.id == "other"  # rotation still moves to the next account


def test_credits_required_never_shortens_a_later_reset(pool):
    weekly = datetime.now(UTC) + timedelta(hours=3)
    _remember_weekly(pool, "seat", weekly)
    later = time.time() + 5 * 86400
    from agent.agent_runtime_helpers import extract_api_error_context
    ctx = {**extract_api_error_context(_error(CREDITS_BODY)), "reset_at": later}
    pool.mark_exhausted_and_rotate(status_code=429, error_context=ctx, credential_id="seat",
                                   failure_reason="rate_limit", model=FABLE)
    seat = next(e for e in pool.entries() if e.id == "seat")
    assert seat.model_cooldowns[FABLE] == pytest.approx(later, abs=2)


def test_without_a_usage_snapshot_the_existing_bench_stands(pool):
    from agent.agent_runtime_helpers import extract_api_error_context
    reset = time.time() + 3600
    ctx = {**extract_api_error_context(_error(CREDITS_BODY)), "reset_at": reset}
    pool.mark_exhausted_and_rotate(status_code=429, error_context=ctx, credential_id="seat",
                                   failure_reason="rate_limit", model=FABLE)
    seat = next(e for e in pool.entries() if e.id == "seat")
    assert seat.model_cooldowns[FABLE] == pytest.approx(reset, abs=2)


def test_a_plain_rate_limit_429_keeps_its_own_reset(pool):
    _remember_weekly(pool, "seat", datetime.now(UTC) + timedelta(days=3))
    reset = time.time() + 600
    ctx = {"message": "This request would exceed your account's rate limit. Please try again later.",
           "reason": "rate_limit_error", "reset_at": reset}
    pool.mark_exhausted_and_rotate(status_code=429, error_context=ctx, credential_id="seat",
                                   failure_reason="rate_limit", model=FABLE)
    seat = next(e for e in pool.entries() if e.id == "seat")
    assert seat.model_cooldowns[FABLE] == pytest.approx(reset, abs=2)


def _real_credits_error():
    import anthropic
    import httpx
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429, json=CREDITS_BODY, request=request)
    return anthropic.RateLimitError("Error code: 429", response=response, body=CREDITS_BODY)


def test_the_real_429_classifies_rotates_and_benches_fable_until_the_account_week(pool):
    """The SDK error as it arrives: no reset in body or headers, a nearer model-scoped week beside it."""
    from agent.agent_runtime_helpers import extract_api_error_context
    from agent.credential_pool_model_cooldowns import pool_failure_reason
    from agent.error_classifier import classify_api_error
    weekly = datetime.now(UTC) + timedelta(days=4)
    _remember_weekly(pool, "seat", weekly, model_week=datetime.now(UTC) + timedelta(days=1))
    err = _real_credits_error()
    ctx = extract_api_error_context(err)
    assert "reset_at" not in ctx and ctx["error_code"] == "credits_required"
    classified = classify_api_error(err, provider="anthropic", model=FABLE)
    reason = pool_failure_reason(classified.reason, ctx, False)

    nxt = pool.mark_exhausted_and_rotate(status_code=429, error_context=ctx, credential_id="seat",
                                         failure_reason=reason, model=FABLE)

    seat = next(e for e in pool.entries() if e.id == "seat")
    assert seat.model_cooldowns[FABLE] == pytest.approx(weekly.timestamp(), abs=2)
    assert seat.last_status is None and pool.select(model=OPUS) is not None
    assert nxt is not None and nxt.id == "other"


def test_without_a_snapshot_the_real_429_keeps_the_default_bench(pool):
    from agent.agent_runtime_helpers import extract_api_error_context
    from agent.credential_pool import EXHAUSTED_TTL_429_SECONDS
    before = time.time()
    pool.mark_exhausted_and_rotate(status_code=429, error_context=extract_api_error_context(_real_credits_error()),
                                   credential_id="seat", failure_reason="rate_limit", model=FABLE)
    seat = next(e for e in pool.entries() if e.id == "seat")
    assert before + EXHAUSTED_TTL_429_SECONDS - 2 <= seat.model_cooldowns[FABLE] <= time.time() + EXHAUSTED_TTL_429_SECONDS + 2
