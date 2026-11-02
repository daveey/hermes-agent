"""The routing pool's ``SubscriptionQuotaUnavailable`` refusal parks a cron job past its reset
(cron/quota_hold.py, self-heal cron-0f5ca72d35).

The pool refusal carries an absolute ``resets_at`` epoch, not a ``retry after <N>s`` hint, and
``_resolve_job_runtime`` re-raises it as a ``RuntimeError`` ``from`` the refusal. Before this
fix the hold only recognized the Codex ``AuthError``, so x-reply-discovery and
bsky-reply-discovery re-fired hourly into a window the pool had already said was closed.

The contract test enumerates every 429-class ``ModelRoutingError`` subclass in the source tree,
so a new quota refusal type cannot bypass the hold the way this one did.
"""

import ast
import importlib
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent.model_routing import ModelRoutingError
import agent.model_routing_unavailable as mru
from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
from cron import quota_hold as qh
from cron.jobs import create_job, get_due_jobs, get_job
from hermes_cli.auth import CODEX_RATE_LIMITED_CODE, AuthError
from hermes_cli.fallback_errors import ProviderFallbackError

from tests.cron.test_quota_hold import _tick

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def tmp_cron_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _wrapped(exc: BaseException) -> RuntimeError:
    """The exact shape ``_resolve_job_runtime`` raises: RuntimeError(text) from the refusal."""
    try:
        raise RuntimeError(str(exc)) from exc
    except RuntimeError as wrapped:
        return wrapped


def test_runtime_error_from_subscription_quota_holds_until_reset():
    reset = time.time() + 2 * 86400
    hold = qh.hold_seconds_from_failure(
        _wrapped(SubscriptionQuotaUnavailable("openai-codex", "gpt-6-astra", reset)))
    assert hold is not None
    assert abs(hold - (reset - time.time())) < 5


def test_passed_or_bogus_reset_gives_no_hold():
    past = SubscriptionQuotaUnavailable("openai-codex", "gpt-6-astra", time.time() - 10)
    assert qh.hold_seconds_from_failure(_wrapped(past)) is None
    for bogus in (None, "soon", float("nan"), float("inf")):
        exc = SubscriptionQuotaUnavailable("openai-codex", "gpt-6-astra", bogus)
        assert qh.hold_seconds_from_failure(_wrapped(exc)) is None, bogus
    # Text alone never parks a job.
    assert qh.hold_seconds_from_failure(RuntimeError(
        "Subscription quota unavailable for openai-codex/gpt-6-astra; resets at 1793774148.0")) is None


def test_fallback_chain_of_pool_refusals_holds_for_the_earliest_reset():
    now = time.time()
    failures = [
        ("openai-codex", "gpt-6-astra", SubscriptionQuotaUnavailable("openai-codex", "gpt-6-astra", now + 7200)),
        ("openai-codex", "gpt-6", AuthError("Codex quota exhausted (429); retry after 3600s",
                                           provider="openai-codex", code=CODEX_RATE_LIMITED_CODE)),
    ]
    hold = qh.hold_seconds_from_failure(ProviderFallbackError(failures))
    assert hold is not None and abs(hold - 3600) < 5
    # One route that is not quota-closed means the chain is not known closed: no hold.
    failures.append(("anthropic", "claude", RuntimeError("connection reset")))
    assert qh.hold_seconds_from_failure(ProviderFallbackError(failures)) is None


def _quota_routing_error_classes() -> set[type]:
    """Every ModelRoutingError subclass in the source tree with HTTP 429 semantics."""
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO)
        if rel.parts[0] in {"tests", ".venv", "node_modules", "website", "apps"} or ".worktrees" in rel.parts:
            continue
        try:
            src = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "ModelRoutingError" not in src and "SubscriptionQuotaUnavailable" not in src:
            continue
        tree = ast.parse(src)
        if any(isinstance(n, ast.ClassDef) and n.bases for n in tree.body):
            importlib.import_module(".".join(rel.with_suffix("").parts))
    found, todo = set(), [ModelRoutingError]
    while todo:
        cls = todo.pop()
        for sub in cls.__subclasses__():
            todo.append(sub)
            if getattr(sub, "status_code", None) == 429:
                found.add(sub)
    return found


# A factory per quota refusal type: a new 429 ModelRoutingError fails this test until it is
# both listed here and mapped to a wait by agent.model_routing_unavailable.quota_wait_seconds.
_QUOTA_FACTORIES = {
    SubscriptionQuotaUnavailable: lambda reset: SubscriptionQuotaUnavailable("p", "m", reset),
}


def test_every_quota_routing_error_maps_to_a_cron_hold():
    classes = _quota_routing_error_classes()
    assert SubscriptionQuotaUnavailable in classes
    assert classes <= set(_QUOTA_FACTORIES), f"unmapped quota refusal types: {classes - set(_QUOTA_FACTORIES)}"
    for cls in classes:
        exc = _QUOTA_FACTORIES[cls](time.time() + 3600)
        assert mru.quota_wait_seconds(exc) is not None, cls
        assert qh.hold_seconds_from_failure(_wrapped(exc)) is not None, cls
    # The Codex AuthError path is a quota type too.
    codex = AuthError("quota", provider="openai-codex", code=CODEX_RATE_LIMITED_CODE, retry_after=900)
    assert qh.hold_seconds_from_failure(_wrapped(codex)) == 900.0


def test_scheduler_tick_parks_job_on_pool_refusal(tmp_cron_home, cron_owner):
    """The real path: resolve_runtime_provider raises the pool refusal; the tick fails once and
    stamps quota_hold_until past the reset instead of re-firing hourly."""
    (tmp_cron_home / "config.yaml").write_text(
        "platforms:\n  slack:\n    enabled: true\n    token: xoxb-test\n", encoding="utf-8")
    job = create_job("bsky reply discovery", "7 * * * *", deliver="slack:alerts")
    now = datetime.now(UTC)
    reset = now.timestamp() + 2 * 86400

    def refuse(**_kw):
        raise SubscriptionQuotaUnavailable("openai-codex", "gpt-6-astra", reset)

    deliveries: list = []
    _tick(get_job(job["id"]), tmp_cron_home, deliveries, refuse)
    j = get_job(job["id"])
    assert j is not None
    assert j["last_status"] == "error"
    assert j.get(qh.STATE_KEY), "the pool refusal must park the job"
    parked = datetime.fromisoformat(j["next_run_at"])
    assert parked.timestamp() >= reset, "next_run_at must land past the reset"
    assert j[qh.STATE_KEY] == j["next_run_at"]
    assert all(d["id"] != job["id"] for d in get_due_jobs())
    assert len(deliveries) == 1 and "held" in deliveries[0], deliveries


def test_quota_failure_without_a_hold_logs_one_warning(caplog):
    """Detection: a quota-class failure the hold cannot map (here: reset already passed) logs
    'quota failure not held' so the self-heal lane sees the gap, not an hourly traceback."""
    exc = _wrapped(SubscriptionQuotaUnavailable("openai-codex", "gpt-6-astra", None))
    with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
        assert qh.classify_failure_hold({"name": "x"}, exc) is None
    assert [r.getMessage() for r in caplog.records if "quota failure not held" in r.getMessage()], caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
        assert qh.classify_failure_hold({"name": "x"}, RuntimeError("model said no")) is None
    assert "quota failure not held" not in caplog.text
