"""A gate tick heals a failure streak made only of pre-agent gate (and fire) failures. [daveey]

hermes-hub cron 6b4342ad83d9 ("ask-eve deploy-guide watcher", a monitor job) showed ``error, 5
failures in a row`` for hours after it recovered: four monitor-source failures (GitHub HTTP 403),
then one owner refusal (``GatewayRPCError``) during a config outage, then only healthy unchanged-
monitor ticks. ba4de177bd4 let a gate tick clear a streak made only of fire refusals, but a monitor
source failure was booked as an *agent* failure (``last_error_kind = "agent"``), so the streak never
cleared: the agent only wakes when the watched output changes.

A monitor-source failure never reaches the agent, and the next good gate tick proves the source
works again. It is now flagged ``_gate_error`` (riding the cron result like ``_agent_skipped``) and
booked ``last_error_kind = "gate"``; a gate tick clears a streak made only of gate/fire failures.
Any agent failure in the streak keeps the keep-verdict behaviour.
"""
import ast
import inspect
import textwrap

import pytest

import cron.jobs as J
import cron.scheduler as S
from tests.cron.test_gated_tick_clears_fire_streak import (
    _agent_failure, _fire_fault, _gate_tick, _make_due, _owner,
)
from tests.cron.test_tick_gateway_not_ready import (
    _bypass_gate, _due_agent_job, _stored, cron_home as _cron_home,
)

pytestmark = pytest.mark.usefixtures("_cron_home")
assert _cron_home  # pytest registers the imported fixture under this module-local name

_MON_ERR = "monitor script failed: HTTP Error 403: rate limit exceeded"


def _monitor_failure(monkeypatch, job_id):
    """The owner ran the monitor gate and the source failed (what session_cron reports back)."""
    _owner(monkeypatch, (False, "# monitor source failed\n", "alert", _MON_ERR), {"_gate_error": True})
    _make_due(job_id)
    S.tick(verbose=False)


def _assert_healed(job_id):
    healed = _stored(job_id)
    assert healed["failure_streak"] == 0, healed
    assert healed["last_status"] == "ok" and healed["last_error"] is None
    assert "last_error_kind" not in healed
    assert not healed.get("preflight_alerted"), "a future break must alert again"
    assert healed["last_failure"]["detail"], "the sticky last-failure stamp survives"


def test_monitor_failures_are_booked_as_gate(monkeypatch):
    _bypass_gate(monkeypatch)
    job_id, _slot = _due_agent_job()
    for n in (1, 2):
        _monitor_failure(monkeypatch, job_id)
        stored = _stored(job_id)
        assert stored["failure_streak"] == n and stored["last_error_kind"] == "gate"


def test_gate_tick_clears_a_streak_made_only_of_monitor_failures(monkeypatch):
    """(a) four monitor-source failures, then an unchanged-monitor tick."""
    _bypass_gate(monkeypatch)
    job_id, _slot = _due_agent_job()
    for _ in range(4):
        _monitor_failure(monkeypatch, job_id)
    J.mark_preflight_alerted(job_id)
    assert _stored(job_id)["failure_streak"] == 4

    _gate_tick(monkeypatch, job_id)

    _assert_healed(job_id)


def test_gate_tick_clears_the_live_monitor_then_fire_sequence(monkeypatch):
    """(b) the hub sequence: four monitor failures, one owner refusal, then a no_change tick."""
    _bypass_gate(monkeypatch)
    job_id, _slot = _due_agent_job()
    for _ in range(4):
        _monitor_failure(monkeypatch, job_id)
    _owner(monkeypatch, None, refuse_submit=True)
    _make_due(job_id)
    S.tick(verbose=False)
    stored = _stored(job_id)
    assert stored["failure_streak"] == 5 and stored["last_error_kind"] in ("gate", "fire")

    _gate_tick(monkeypatch, job_id)

    _assert_healed(job_id)


def test_gate_tick_keeps_a_streak_that_holds_an_agent_failure(monkeypatch):
    """(c) an agent verdict in the streak survives a gate tick, even after later gate failures."""
    _bypass_gate(monkeypatch)
    job_id, _slot = _due_agent_job()
    _agent_failure(monkeypatch, job_id)
    _monitor_failure(monkeypatch, job_id)
    stored = _stored(job_id)
    assert stored["failure_streak"] == 2 and stored["last_error_kind"] == "agent"

    _gate_tick(monkeypatch, job_id)

    kept = _stored(job_id)
    assert kept["failure_streak"] == 2 and kept["last_status"] == "error"
    assert kept["last_error"] == stored["last_error"] and kept["last_error_kind"] == "agent"


def test_fire_then_gate_streak_stays_pre_agent(monkeypatch):
    _bypass_gate(monkeypatch)
    _fire_fault(monkeypatch)
    job_id, _slot = _due_agent_job()
    S.tick(verbose=False)
    _monitor_failure(monkeypatch, job_id)
    assert _stored(job_id)["last_error_kind"] in ("gate", "fire")
    _gate_tick(monkeypatch, job_id)
    _assert_healed(job_id)


def test_real_monitor_script_failure_then_recovery_heals_end_to_end(monkeypatch):
    """(d) a real monitor script run by the real ``_apply_monitor_gate`` on the owner side, its job
    flags carried back exactly as ``gateway.session_cron`` does (``RUN_JOB_FLAGS``), booked by the
    firing scheduler's tick."""
    from gateway.session_cron import RUN_JOB_FLAGS
    from cron.monitor import check_monitor

    home = J.HERMES_DIR
    flag = home / "fail-now"
    (home / "scripts" / "watch.py").write_text(
        "import pathlib, sys\n"
        f"if pathlib.Path({str(flag)!r}).exists():\n"
        "    print('HTTP Error 403: rate limit exceeded'); sys.exit(1)\n"
        "print('guide v1')\n", encoding="utf-8")
    _bypass_gate(monkeypatch)
    job_id, _slot = _due_agent_job("watcher")
    J.update_job(job_id, {"monitor_script": "watch.py"})
    from cron.monitor_outbox import acknowledge
    seeded = check_monitor(_stored(job_id))
    assert seeded.changed and seeded.change_id  # first run stages the baseline
    assert acknowledge(job_id, seeded.change_id)  # as a successful agent run would

    from contextlib import asynccontextmanager
    from hermes_cli import gateway_client

    class OwnerRunningTheGate:
        async def rpc(self, method, **params):
            if method == "cron.submit":
                return {"session_id": "s", "admission_id": "a"}
            if method == "cron.recover":
                return {"status": "missing"}
            assert method == "cron.status"
            job = dict(_stored(job_id))  # the owner's own copy, as run_job gets it
            early, _extra, _ctx = S._apply_monitor_gate(job, job_id, job["name"], None)
            assert early is not None, "the seeded monitor never wakes the agent here"
            flags = {key: job[key] for key in RUN_JOB_FLAGS if key in job}
            return {"status": "terminal", "result": list(early), "job_flags": flags}

    @asynccontextmanager
    async def connect(*_a, **_k):
        yield OwnerRunningTheGate()

    monkeypatch.setattr(gateway_client, "connect_gateway", connect)

    flag.write_text("1")
    for n in (1, 2, 3):
        _make_due(job_id)
        S.tick(verbose=False)
        assert _stored(job_id)["failure_streak"] == n
    assert "403" in _stored(job_id)["last_error"]

    flag.unlink()
    _make_due(job_id)
    S.tick(verbose=False)

    _assert_healed(job_id)


# --- detection guard: every pre-agent failure return names its kind ------------------------------

# Pre-agent failures deliberately booked as agent failures: a gate tick does not prove them fixed.
# The injection block re-trips on the next real wake (the prompt/skill content is unchanged).
_AGENT_KIND_BY_DESIGN = {("_prepare_job_prompt", "CronPromptInjectionBlocked")}


def _untagged_failure_returns(fn):
    """Every ``return (False, ...)`` (also ``return (False, ...), x``) in *fn* whose branch does not
    set ``_gate_error`` / ``_fire_error`` before it, minus the by-design list."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    missing = []
    for branch in ast.walk(tree):
        bodies = [getattr(branch, "body", None), getattr(branch, "orelse", None)]
        for body in (b for b in bodies if isinstance(b, list)):
            for i, stmt in enumerate(body):
                if not isinstance(stmt, ast.Return) or not isinstance(stmt.value, ast.Tuple):
                    continue
                first = stmt.value.elts[0] if stmt.value.elts else None
                inner = first.elts[0] if isinstance(first, ast.Tuple) and first.elts else first
                if not (isinstance(inner, ast.Constant) and inner.value is False):
                    continue
                if any("_gate_error" in ast.unparse(s) or "_fire_error" in ast.unparse(s)
                       for s in body[:i]):
                    continue
                handler = (ast.unparse(branch.type)
                           if isinstance(branch, ast.ExceptHandler) and branch.type else None)
                if (fn.__name__, handler) not in _AGENT_KIND_BY_DESIGN:
                    missing.append(f"{fn.__name__}:+{stmt.lineno} {ast.unparse(stmt)[:90]}")
    return missing


def _pre_agent_gates():
    import cron.scheduler_authority as A
    return [S._apply_monitor_gate, S._prepare_job_prompt, A.run_canonical_job]


@pytest.mark.parametrize("fn", _pre_agent_gates(), ids=lambda f: f.__name__)
def test_every_pre_agent_failure_return_tags_a_failure_kind(fn):
    """A pre-agent failure that sets neither ``_gate_error`` nor ``_fire_error`` is booked as an
    agent failure, which no later gate tick can clear (the bug above). Each ``return (False, ...)``
    in the pre-agent gates must sit in a branch that flags its kind, or be listed by design."""
    assert not _untagged_failure_returns(fn), _untagged_failure_returns(fn)


def test_guard_sees_an_untagged_failure_return():
    """The guard itself goes red on the pre-fix shape of the monitor source-failure branch."""
    def _apply_monitor_gate(job, ok):
        if not ok:
            return (False, "doc", "alert", "err"), None, None
        return None, None, None
    assert _untagged_failure_returns(_apply_monitor_gate)


def test_gate_error_rides_the_cron_result_across_the_owner_boundary():
    from gateway.session_cron import RUN_JOB_FLAGS
    assert "_gate_error" in RUN_JOB_FLAGS
