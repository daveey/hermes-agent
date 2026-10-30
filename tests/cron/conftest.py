"""Cron-test fixtures.

Provides a default ``HERMES_MODEL`` for cron run_job tests so each one
doesn't have to spell out a model. The global conftest blanks
HERMES_MODEL hermetically; without this autouse fixture every cron test
that exercises ``run_job`` would hit the fail-fast guard added in
``cron/scheduler.py`` (see issue #23979) and have to be rewritten.

Tests that specifically need ``HERMES_MODEL`` unset — model-resolution
edge cases — call ``monkeypatch.delenv("HERMES_MODEL", raising=False)``
inside the test, which overrides this fixture's value for that scope.
"""

import pytest


@pytest.fixture()
def make_cron_provider():
    """Factory for minimal CronScheduler test doubles.

    ``make_cron_provider(register_job=...)`` returns a real ``CronScheduler``
    subclass instance whose ``register_job`` is the given callable — so tests
    exercising the creation-registration contract share one stub instead of
    redefining inline spy/failing classes, and an ABC rename breaks them
    loudly instead of silently passing a duck-type.
    """
    from cron.scheduler_provider import CronScheduler

    def _make(register_job=None, name="stub"):
        class _StubProvider(CronScheduler):
            @property
            def name(self):  # pragma: no cover - trivial
                return name

            def start(self, stop_event, **kw):  # pragma: no cover - unused
                pass

            def register_job(self, job):
                if register_job is not None:
                    return register_job(job)
                return None

        return _StubProvider()

    return _make


@pytest.fixture(autouse=True)
def _no_managed_store(tmp_path, monkeypatch):
    """Point PM's store at an empty dir: script runs must not select the host install's
    dependency venv (POSIX cron scripts run on it when a store is committed)."""
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "no-pm-store"))


@pytest.fixture(autouse=True)
def _default_cron_test_model(monkeypatch):
    """Pin a default HERMES_MODEL so cron run_job tests have a resolvable model."""
    monkeypatch.setenv("HERMES_MODEL", "test-cron-default-model")
    yield


@pytest.fixture(autouse=True)
def _reset_session_context_vars():
    """Restore session ContextVars around cron tests that call run_job directly.

    Production confines each cron run to a copied context, but direct unit tests
    share the pytest context. ``run_job`` intentionally clears ordinary session
    variables to explicit empty values, which would otherwise shadow legacy env
    fallbacks used by later approval tests in the same process.
    """
    from gateway.session_context import _UNSET, _VAR_MAP

    def _reset_all():
        for var in _VAR_MAP.values():
            var.set(_UNSET)

    _reset_all()
    yield
    _reset_all()


@pytest.fixture(autouse=True)
def _drain_shared_metrics_worker(monkeypatch):
    """Let the fire-and-forget shared-metrics worker finish before the test is torn down.

    Cron runs hand ``hermes.cron.run`` recordings to a background thread
    (``shared_metrics_gateway._submit``) that reads config and compiles regexes. Left running it
    races the next phase: pytest's per-phase ``catch_warnings`` setup compiles filter regexes, and
    ``re._compile`` evicting its cache while another thread inserts raises "dictionary changed
    size during iteration" (a teardown ERROR, then "previous item was not torn down properly" on
    the next test). Requesting ``monkeypatch`` makes this fixture tear down first, so the worker
    drains while the test's patched home/env are still in place. The worker is FIFO, so the drain
    marker returns once every earlier recording is done; the timeout only bounds a hung worker.
    """
    yield
    from hermes_cli.observability import shared_metrics_gateway as smg

    if smg._executor is not None:
        smg.drain(timeout=120.0)
