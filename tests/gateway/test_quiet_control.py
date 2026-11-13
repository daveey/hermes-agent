"""Quiet-v3 control verbs: real ``GatewayControlServer`` envelope, real handlers, real registry.

External effects only are replaced: the registry class gains a membership generation (the INGRESS
registry API this verb set consumes), the runner is the real ``GatewayRunner`` class without its heavy
constructor, and the lease/update markers are real files under a scratch root.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import threading
import time

import pytest

from agent import claim_quiet as cq
from gateway import quiet_control
from gateway.control_dispatch import ControlRequestDispatcher, verb_is_work_bearing
from gateway.control_socket import GatewayControlServer
from gateway.run import GatewayRunner
from gateway.runtime_ownership import ProfileOwnership
from gateway.turn_lease import SessionTurnLeaseRegistry
from hermes_cli import admission_fence
from hermes_cli import kanban_deploy_window as window
from hermes_cli import code_attestation
from hermes_cli import local_work
from tests.waiting import wait_until

PEER = "uid:1000"


class Runner(GatewayRunner):
    session_runtime_descriptor = {"instance_id": "inst-1", "state": "ready"}


class CleanLedger:
    """A code-attestation ledger with no unknowns on the direct managed-stub route."""

    def __init__(self, unknowns=(), route="managed-stub"):
        self._unknowns = tuple(unknowns)
        self._route = route

    def unknowns(self):
        return self._unknowns

    def latched_unknowns(self):
        return self._unknowns

    def route_report(self):
        return {"route": self._route}


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(root))
    # The production registry, unseeded exactly as hermes_bootstrap installs it; membership comes from
    # the production seeding path (the tracked process reservation of the launch home).
    registry = local_work.Registry()
    monkeypatch.setattr(local_work, "_registry", registry)
    ownership = ProfileOwnership(track_membership=True)
    ownership.reserve([root])
    monkeypatch.setitem(quiet_control._boot, "registry", None)
    monkeypatch.setitem(quiet_control._boot, "captured", False)
    # The executed-code ledger the bootstrap installs beside the registry: clean on a supported
    # route (route-specific ledgers are exercised from real launches in
    # tests/hermes_cli/test_code_attestation_relaunch.py).
    monkeypatch.setattr(code_attestation, "_installed", CleanLedger())
    quiet_control.capture_bootstrap_registry()  # the registry predates every runner: bootstrap provenance
    runner = object.__new__(Runner)
    runner._turn_leases = SessionTurnLeaseRegistry()
    runner._draining = runner._restart_requested = False
    quiet_control.bind_registry(runner)
    server = GatewayControlServer(root)
    control = quiet_control.register_quiet_control(runner, server)
    write_window(root, "idle")
    owner = cq.acquire(max_s=480, reason="test")
    lease = cq.read_active()
    assert lease is not None
    yield type("Env", (), dict(root=root, registry=registry, runner=runner, server=server,
                               control=control, lease=lease, monkeypatch=monkeypatch))
    owner.close()
    ownership.close()


def write_window(root, state, *, age=0):
    path = window.window_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    path.write_text(json.dumps({"version": 1, "host": window.host_identity(), "observed_at": now - age,
                                "window_at": now + 599, "state": state, "state_since": now - age - 1,
                                "drain_bound_s": 60}))


def gateway(runner=None):
    from gateway.status import _get_code_identity_fields, _get_process_start_time
    return {"pid": os.getpid(), "started_at": _get_process_start_time(os.getpid()), "instance_id": "inst-1",
            "code_sha": _get_code_identity_fields().get("code_sha")}


def start_params(env, **over):
    lease = env.lease
    params = {"schema": 1, "episode_id": lease.episode_id, "producer_pid": lease.producer_pid,
              "producer_started_at": lease.producer_started_at,
              "producer_incarnation": lease.producer_incarnation, "expected_gateway": gateway(),
              "deadline_mono_ns": lease.deadline_ns}
    params.update(over)
    return params


def follow_params(env, token, **over):
    params = {"schema": 1, "episode_id": env.lease.episode_id, "observer_token": token,
              "expected_gateway": gateway(), "deadline_mono_ns": env.lease.deadline_ns}
    params.update(over)
    return params


def call(env, verb, params, peer=PEER):
    raw = json.dumps({"protocol": 1, "verb": verb, "id": 1, "params": params}).encode()
    response = json.loads(env.server.handle_request_line(raw, peer))
    return response


def ok(env, verb, params, peer=PEER):
    response = call(env, verb, params, peer)
    assert response["ok"], response
    return response["result"]


def attest(env):
    assert quiet_control.finalize_attestation(env.runner)


# ---- wire registration ---------------------------------------------------------------------

def test_three_private_handlers_registered_and_authenticated(env):
    assert set(env.server.private_handlers) == {
        "quiet-observe-start", "quiet-observe-snapshot", "quiet-observe-end", "quiet-observe-check"}
    response = call(env, "quiet-observe-start", start_params(env), peer=None)
    assert not response["ok"] and "authenticated private request required" in response["error"]


# ---- start refusal -------------------------------------------------------------------------

def test_start_refused_while_coverage_pending(env):
    result = ok(env, "quiet-observe-start", start_params(env))
    assert result["valid"] is False and result["phase"] == "coverage_pending"
    assert "coverage_pending" in result["reasons"] and "observer_token" not in result


def test_start_refused_without_bootstrap_provenance(env):
    # A registry that was not installed when the bootstrap looked: unattested, registry marked unknown.
    env.monkeypatch.setitem(quiet_control._boot, "registry", None)
    env.monkeypatch.setitem(quiet_control._boot, "captured", True)
    runner = object.__new__(Runner)
    runner._turn_leases = SessionTurnLeaseRegistry()
    quiet_control.bind_registry(runner)
    assert "unattested_bootstrap" in env.registry.snapshot().unknowns
    control = quiet_control.register_quiet_control(runner, GatewayControlServer(env.root))
    assert quiet_control.finalize_attestation(runner) is False
    result = control.start(start_params(env), PEER)
    assert result["valid"] is False and "bootstrap_provenance_missing" in result["reasons"]


def test_start_refused_when_no_registry_installed(env):
    env.monkeypatch.setattr(local_work, "_registry", None)
    env.monkeypatch.setitem(quiet_control._boot, "registry", None)
    env.monkeypatch.setitem(quiet_control._boot, "captured", False)
    runner = object.__new__(Runner)
    quiet_control.bind_registry(runner)
    control = quiet_control.register_quiet_control(runner, GatewayControlServer(env.root))
    result = control.start(start_params(env), PEER)
    assert "registry_missing" in result["reasons"]
    assert runner._quiet_local_unknowns == {"unattested_bootstrap"}


def test_start_refused_with_unknown_registry_state(env):
    attest(env)
    env.registry.mark_unknown("late-detach")
    result = ok(env, "quiet-observe-start", start_params(env))
    assert result["valid"] is False and "unknown:late-detach" in result["reasons"]
    assert "observer_token" not in result


def test_start_refused_when_membership_api_missing(env):
    attest(env)
    env.monkeypatch.setattr(local_work.Registry, "membership_generation", None)
    result = ok(env, "quiet-observe-start", start_params(env))
    assert "membership_unsupported" in result["reasons"]


@pytest.mark.parametrize("mutate, reason", [
    (lambda e, p: p.update(episode_id="someone-else"), "lease_episode_mismatch"),
    (lambda e, p: p.update(producer_pid=p["producer_pid"] + 1), "lease_producer_pid_mismatch"),
    (lambda e, p: p.update(producer_incarnation="x"), "lease_producer_incarnation_mismatch"),
    (lambda e, p: p.update(deadline_mono_ns=p["deadline_mono_ns"] - 1), "deadline_not_lease_deadline"),
    (lambda e, p: p["expected_gateway"].update(pid=os.getpid() + 1), "gateway_pid_mismatch"),
    (lambda e, p: p["expected_gateway"].update(instance_id="other"), "gateway_instance_id_mismatch"),
])
def test_start_refused_on_lease_or_identity_mismatch(env, mutate, reason):
    attest(env)
    params = start_params(env)
    mutate(env, params)
    result = ok(env, "quiet-observe-start", params)
    assert result["valid"] is False and reason in result["reasons"]
    assert "observer_token" not in result


def test_start_refused_without_active_lease(env):
    attest(env)
    env.control._lease_reader = lambda: cq.QuietReading("inactive", None, "none")
    result = ok(env, "quiet-observe-start", start_params(env))
    assert "lease_inactive" in result["reasons"]


def test_start_refused_after_deadline(env):
    attest(env)
    now = env.control._mono()
    env.control._now_ns = lambda: now + (env.lease.deadline_ns - now) + 1
    result = ok(env, "quiet-observe-start", start_params(env))
    assert "lease_deadline_reached" in result["reasons"]


def test_start_refused_on_update_markers(env):
    attest(env)
    write_window(env.root, "draining")
    result = ok(env, "quiet-observe-start", start_params(env))
    assert result["valid"] is False and "update:update_window_draining" in result["reasons"]
    write_window(env.root, "idle", age=700)  # fresh for read_window, beyond quiet's 660 s bound
    assert "update_window_stale" in ok(env, "quiet-observe-start", start_params(env))["reasons"]
    os.unlink(window.window_path())
    assert "update:update_window_unavailable" in ok(env, "quiet-observe-start", start_params(env))["reasons"]


def write_update_hold(root, kind):
    """A real core-update hold file: the updater's admission marker, or a live-pid drain hold."""
    if kind == "admission":
        admission_fence.set_hold(root)
    else:
        path = root / "core-update" / "drain-hold"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": os.getpid(), "started_at": time.time(), "max_s": 600}))


@pytest.mark.parametrize("state, age, hold", [
    ("draining", 0, None), ("batching", 0, None), ("idle", 700, None), ("idle", 0, None), (None, 0, None),
    # #2591 M2c: a hold alone (fresh idle window) must refuse here too, or quiet-hold spends its cooldown
    # on a start the gateway then refuses; with a draining window the hold reason still comes first.
    ("idle", 0, "admission"), ("idle", 0, "drain"), ("draining", 0, "admission"),
])
def test_start_preflight_matches_the_start_refusal_from_host_files(env, state, age, hold):
    """quiet-hold's lease-free preflight reads the same files, with the same spellings, as start."""
    attest(env)
    if state is None:
        os.unlink(window.window_path())
    else:
        write_window(env.root, state, age=age)
    if hold is not None:
        write_update_hold(env.root, hold)
    result = ok(env, "quiet-observe-start", start_params(env))
    refused = result["reasons"] if result["phase"] != "started" else []
    assert quiet_control.start_preflight_reasons(margin_s=0) == refused
    assert (result["phase"] == "started") == (state == "idle" and age == 0 and hold is None)
    if hold is not None:
        assert refused[0] == "update:update_hold", refused


def test_start_preflight_margin_refuses_a_window_about_to_go_stale(env):
    write_window(env.root, "idle", age=quiet_control.UPDATE_WINDOW_MAX_AGE_S - 5)
    assert quiet_control.start_preflight_reasons(margin_s=0) == []
    assert quiet_control.start_preflight_reasons() == ["update_window_stale"]


# ---- schema / authentication ---------------------------------------------------------------

@pytest.mark.parametrize("mutate", [
    lambda p: p.update(schema=2),
    lambda p: p.update(schema=True),
    lambda p: p.update(extra=1),
    lambda p: p.pop("episode_id"),
    lambda p: p.update(producer_pid=True),
    lambda p: p.update(producer_pid=0),
    lambda p: p.update(deadline_mono_ns="1"),
    lambda p: p.update(deadline_mono_ns=True),
    lambda p: p.update(producer_started_at=float("inf")),
    lambda p: p["expected_gateway"].update(extra=1),
    lambda p: p.update(expected_gateway=[]),
    lambda p: p.update(episode_id="x" * 300),
])
def test_start_rejects_malformed_params(env, mutate):
    attest(env)
    params = start_params(env)
    mutate(params)
    response = call(env, "quiet-observe-start", params)
    assert response["ok"] is False and "ValueError" in response["error"]


def test_follow_verbs_reject_schema_and_wrong_credentials(env):
    attest(env)
    started = ok(env, "quiet-observe-start", start_params(env))
    token = started["observer_token"]
    for verb in ("quiet-observe-snapshot", "quiet-observe-end"):
        assert "ValueError" in call(env, verb, follow_params(env, token, schema=3))["error"]
        assert "ValueError" in call(env, verb, follow_params(env, token, extra=1))["error"]
        assert "PermissionError" in call(env, verb, follow_params(env, "forged-token"))["error"]
        assert "PermissionError" in call(env, verb, follow_params(env, token), peer="uid:other")["error"]
        assert "PermissionError" in call(env, verb, follow_params(env, token, episode_id="other"))["error"]
        assert "PermissionError" in call(
            env, verb, follow_params(env, token, deadline_mono_ns=env.lease.deadline_ns - 5))["error"]
    # The refusals above left the live episode untouched.
    assert ok(env, "quiet-observe-snapshot", follow_params(env, token))["valid"] is True


# ---- snapshot content ----------------------------------------------------------------------

def test_clean_cycle_reports_registry_predicates_and_update_status(env):
    attest(env)
    started = ok(env, "quiet-observe-start", start_params(env))
    assert started["valid"] is True and started["phase"] == "started"
    token = started["observer_token"]
    first = ok(env, "quiet-observe-snapshot", follow_params(env, token))
    second = ok(env, "quiet-observe-snapshot", follow_params(env, token))
    assert second["sequence"] == first["sequence"] + 1
    for snap in (first, second):
        assert snap["valid"] is True and snap["reasons"] == []
        assert snap["active_count"] == 0 and snap["unknowns"] == []
        assert snap["activity_epoch"] == env.registry.snapshot().epoch
        assert snap["membership_generation"] == env.registry.membership_generation() > 0
        assert snap["pending_transitions"] == 0
        assert snap["membership"] == {local_work.canonical_home(env.root): ["ready", ["owned"]]}
        assert snap["predicates"] == {"active_agents": 0, "workers": 0, "held_turn_leases": 0}
        assert snap["update"]["collision"] is False and snap["update"]["window_state"] == "idle"
        assert snap["gateway"]["pid"] == os.getpid() and snap["registry_provenance"] == "bootstrap"
        assert snap["coverage"] == "attested"
    assert "observer_token" not in first and token not in json.dumps(first)
    # Bookkeeping does not move the epoch: measuring must never invalidate its own experiment.
    assert first["activity_epoch"] == second["activity_epoch"]


def test_registry_work_is_visible_and_epoch_moves(env):
    attest(env)
    token = ok(env, "quiet-observe-start", start_params(env))["observer_token"]
    before = ok(env, "quiet-observe-snapshot", follow_params(env, token))
    reservation = env.registry.reserve("agent", home=str(env.root), source="test")
    busy = ok(env, "quiet-observe-snapshot", follow_params(env, token))
    assert busy["valid"] is False and busy["active_count"] == 1 and busy["per_kind"] == {"agent": 1}
    assert "active_work" in busy["reasons"] and busy["activity_epoch"] > before["activity_epoch"]
    env.registry.finish(reservation)
    after = ok(env, "quiet-observe-snapshot", follow_params(env, token))
    assert after["valid"] is True and after["active_count"] == 0
    assert after["activity_epoch"] > before["activity_epoch"]  # zero again, but the epoch remembers


def test_epoch_moving_during_read_is_not_clean(env):
    attest(env)
    token = ok(env, "quiet-observe-start", start_params(env))["observer_token"]
    real = env.runner._active_work_count

    def churn():
        env.registry.finish(env.registry.reserve("agent", home=str(env.root), source="churn"))
        return real()

    env.monkeypatch.setattr(env.runner, "_active_work_count", churn)
    snap = ok(env, "quiet-observe-snapshot", follow_params(env, token))
    assert snap["valid"] is False and "epoch_moved_during_read" in snap["reasons"]


def test_membership_generation_change_during_read_is_not_clean(env):
    attest(env)
    token = ok(env, "quiet-observe-start", start_params(env))["observer_token"]
    real = env.runner._active_work_count

    def churn():  # a served profile joins mid-read through the real membership API
        env.registry.membership_commit(env.registry.membership_begin(
            str(env.root / "profiles" / "late"), role="owned", transition="add"), state="ready")
        return real()

    env.monkeypatch.setattr(env.runner, "_active_work_count", churn)
    assert "epoch_moved_during_read" in ok(env, "quiet-observe-snapshot", follow_params(env, token))["reasons"]


def test_open_membership_transition_is_not_clean(env):
    attest(env)
    token = ok(env, "quiet-observe-start", start_params(env))["observer_token"]
    joining = env.registry.membership_begin(str(env.root / "profiles" / "joining"), role="owned",
                                            transition="add")
    snap = ok(env, "quiet-observe-snapshot", follow_params(env, token))
    assert snap["valid"] is False and snap["pending_transitions"] == 1
    assert "membership_transition_pending" in snap["reasons"]
    env.registry.membership_commit(joining, state="ready")
    assert ok(env, "quiet-observe-snapshot", follow_params(env, token))["valid"] is True


def test_production_registry_start_is_not_refused_for_membership(env):
    """RED on 95089d00fb: the production Registry had no membership_generation (every start refused
    ``membership_unsupported``) and nothing seeded membership (first reservation latched unknown-home)."""
    attest(env)
    assert type(env.registry) is local_work.Registry
    held = env.registry.reserve("agent", home=str(env.root), source="test")
    env.registry.finish(held)
    started = ok(env, "quiet-observe-start", start_params(env))
    assert "membership_unsupported" not in started["reasons"]
    assert started["valid"] is True and started["unknowns"] == []


def test_original_predicates_are_read_from_their_real_sources(env):
    attest(env)
    token = ok(env, "quiet-observe-start", start_params(env))["observer_token"]
    env.runner._running_agents["session"] = object()
    snap = ok(env, "quiet-observe-snapshot", follow_params(env, token))
    assert snap["predicates"]["active_agents"] == 1 and "predicate_nonzero:active_agents" in snap["reasons"]
    env.runner._running_agents.clear()

    async def hold_lease():
        lease = await env.runner._turn_leases.acquire("sid", owner_key="k", generation=1)
        try:
            return ok(env, "quiet-observe-snapshot", follow_params(env, token))
        finally:
            env.runner._turn_leases.release(lease)

    held = asyncio.run(hold_lease())
    assert held["predicates"]["held_turn_leases"] == 1
    assert "predicate_nonzero:held_turn_leases" in held["reasons"]
    assert ok(env, "quiet-observe-snapshot", follow_params(env, token))["valid"] is True


def test_unreadable_predicate_is_never_zero(env):
    attest(env)
    token = ok(env, "quiet-observe-start", start_params(env))["observer_token"]
    env.runner._turn_leases = None
    snap = ok(env, "quiet-observe-snapshot", follow_params(env, token))
    assert snap["predicates"]["held_turn_leases"] is None
    assert "predicate_unreadable:held_turn_leases" in snap["reasons"] and snap["valid"] is False


def test_update_collision_appears_mid_episode(env):
    attest(env)
    token = ok(env, "quiet-observe-start", start_params(env))["observer_token"]
    write_window(env.root, "batching")
    snap = ok(env, "quiet-observe-snapshot", follow_params(env, token))
    assert snap["update"]["collision"] is True and "update:update_window_batching" in snap["reasons"]
    write_window(env.root, "idle")
    env.runner._draining = True
    snap = ok(env, "quiet-observe-snapshot", follow_params(env, token))
    assert "update:runner_draining" in snap["reasons"]


def test_gateway_identity_mismatch_refuses_snapshot(env):
    attest(env)
    token = ok(env, "quiet-observe-start", start_params(env))["observer_token"]
    params = follow_params(env, token)
    params["expected_gateway"]["instance_id"] = "replaced"
    snap = ok(env, "quiet-observe-snapshot", params)
    assert snap["valid"] is False and "gateway_instance_id_mismatch" in snap["reasons"]


# ---- episode lifecycle ---------------------------------------------------------------------

def test_start_is_idempotent_for_same_peer_and_conflicts_otherwise(env):
    attest(env)
    first = ok(env, "quiet-observe-start", start_params(env))
    again = ok(env, "quiet-observe-start", start_params(env))
    assert again["observer_token"] == first["observer_token"]
    other_peer = ok(env, "quiet-observe-start", start_params(env), peer="uid:other")
    assert other_peer["valid"] is False and "episode_conflict" in other_peer["reasons"]
    successor = dataclasses.replace(env.lease, episode_id="successor")
    env.control._lease_reader = lambda: cq.QuietReading("active", successor, "verified")
    conflict = ok(env, "quiet-observe-start", start_params(env, episode_id="successor"))
    assert "episode_conflict" in conflict["reasons"]


def test_end_is_idempotent_and_cannot_touch_a_successor(env):
    attest(env)
    token = ok(env, "quiet-observe-start", start_params(env))["observer_token"]
    ended = ok(env, "quiet-observe-end", follow_params(env, token))
    assert ended["phase"] == "ended" and ended["valid"] is False
    assert ok(env, "quiet-observe-end", follow_params(env, token))["phase"] == "ended"  # idempotent
    assert ok(env, "quiet-observe-snapshot", follow_params(env, token))["reasons"] == ["episode_ended"]
    # A successor episode: a stale token/end for the old one must not clear it.
    successor = dataclasses.replace(env.lease, episode_id="successor")
    env.control._lease_reader = lambda: cq.QuietReading("active", successor, "verified")
    new_token = ok(env, "quiet-observe-start", start_params(env, episode_id="successor"))["observer_token"]
    assert new_token != token
    ok(env, "quiet-observe-end", follow_params(env, token))  # stale end for the old episode
    live = ok(env, "quiet-observe-snapshot", follow_params(env, new_token, episode_id="successor"))
    assert live["valid"] is True
    forged = call(env, "quiet-observe-end", follow_params(env, token, episode_id="successor"))
    assert "PermissionError" in forged["error"]
    assert ok(env, "quiet-observe-snapshot",
              follow_params(env, new_token, episode_id="successor"))["valid"] is True


def test_token_authority_expires_with_the_lease(env):
    attest(env)
    token = ok(env, "quiet-observe-start", start_params(env))["observer_token"]
    env.control._lease_reader = lambda: cq.QuietReading("inactive", None, "released")
    snap = ok(env, "quiet-observe-snapshot", follow_params(env, token))
    assert snap["valid"] is False and "lease_inactive" in snap["reasons"]
    env.control._lease_reader = None  # the lease is back, but the episode died with it
    dead = ok(env, "quiet-observe-snapshot", follow_params(env, token))
    assert dead["valid"] is False and dead["phase"] == "ended" and dead["reasons"] == ["episode_ended"]
    # The observer's best-effort end on the expired episode is answered, not a foreign-episode error.
    ended = ok(env, "quiet-observe-end", follow_params(env, token))
    assert ended["valid"] is False and ended["reasons"] == ["episode_ended"]
    # A token that never belonged to the episode is still refused.
    assert "PermissionError" in call(env, "quiet-observe-snapshot", follow_params(env, "forged"))["error"]


def test_bookkeeping_handlers_do_not_reserve(env):
    attest(env)
    before = env.registry.snapshot()
    token = ok(env, "quiet-observe-start", start_params(env))["observer_token"]
    ok(env, "quiet-observe-snapshot", follow_params(env, token))
    ok(env, "quiet-observe-end", follow_params(env, token))
    after = env.registry.snapshot()
    assert (after.epoch, after.active) == (before.epoch, before.active)


# ---- dispatcher classification -------------------------------------------------------------

def test_verb_classification_is_default_deny():
    for verb in ("identify", "status", "profile", "resolve-profile", "quiet-observe-start",
                 "quiet-observe-snapshot", "quiet-observe-end"):
        assert not verb_is_work_bearing(verb)
    for verb in ("hosted-producer", "reload-plugins", "serve-profile", "rescan-profiles",
                 "brand-new-verb", None, 7):
        assert verb_is_work_bearing(verb)


def _dispatch_env(env):
    entered, release = threading.Event(), threading.Event()

    def handler(raw, peer):
        entered.set()
        release.wait(10)
        return b"{}\n"

    return ControlRequestDispatcher(registry=env.registry, home=str(env.root)), handler, entered, release


def test_work_bearing_control_reserves_until_the_handler_physically_ends(env):
    async def run():
        dispatcher, handler, entered, release = _dispatch_env(env)
        base = env.registry.snapshot()
        task = asyncio.ensure_future(dispatcher.dispatch(
            handler, json.dumps({"verb": "hosted-producer", "protocol": 1}).encode(), None))
        await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)
        assert env.registry.snapshot().active == base.active + 1
        assert env.registry.snapshot().per_kind["control:hosted-producer"] == 1
        task.cancel()  # the client closed / timed out: custody does not move
        with pytest.raises(asyncio.CancelledError):
            await task
        assert env.registry.snapshot().active == base.active + 1
        release.set()
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: wait_until(lambda: env.registry.snapshot().active == base.active))
        dispatcher.close()

    asyncio.run(run())


@pytest.mark.parametrize("verb", ["identify", "status", "profile", "quiet-observe-snapshot"])
def test_passive_and_bookkeeping_verbs_do_not_reserve(env, verb):
    async def run():
        dispatcher, handler, entered, release = _dispatch_env(env)
        before = env.registry.snapshot()
        task = asyncio.ensure_future(dispatcher.dispatch(
            handler, json.dumps({"verb": verb, "protocol": 1}).encode(), None))
        await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)
        during = env.registry.snapshot()
        release.set()
        await task
        dispatcher.close()
        return before, during

    before, during = asyncio.run(run())
    assert (during.epoch, during.active) == (before.epoch, before.active)


def test_unknown_verb_and_unparseable_request(env):
    async def run():
        dispatcher, handler, entered, release = _dispatch_env(env)
        base = env.registry.snapshot().active
        task = asyncio.ensure_future(dispatcher.dispatch(
            handler, json.dumps({"verb": "future-verb", "protocol": 1}).encode(), None))
        await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)
        counted = env.registry.snapshot().active
        release.set()
        await task
        # Garbage carries no verb and no work: nothing to reserve.
        before_garbage = env.registry.snapshot().epoch
        release_garbage = ControlRequestDispatcher(registry=env.registry, home=str(env.root))
        await release_garbage.dispatch(lambda raw, peer: b"{}\n", b"not json", None)
        assert env.registry.snapshot().epoch == before_garbage
        dispatcher.close()
        release_garbage.close()
        return base, counted

    base, counted = asyncio.run(run())
    assert counted == base + 1


def test_dispatch_without_a_registry_still_dispatches(env):
    env.monkeypatch.setattr(local_work, "_registry", None)

    async def run():
        dispatcher = ControlRequestDispatcher()
        result = await dispatcher.dispatch(lambda raw, peer: b"ok\n", b'{"verb":"hosted-producer"}', None)
        dispatcher.close()
        return result

    assert asyncio.run(run()) == b"ok\n"


# ---- real socket ---------------------------------------------------------------------------

@pytest.mark.platforms("posix")
def test_real_listener_round_trip_through_query_gateway_control(env):
    from gateway.control_socket import query_gateway_control
    attest(env)

    async def run():
        assert await env.server.start()
        try:
            loop = asyncio.get_running_loop()
            started = await loop.run_in_executor(
                None, lambda: query_gateway_control(env.root, "quiet-observe-start",
                                                    params=start_params(env), timeout=5.0))
            assert started is not None and started["valid"] is True
            snap = await loop.run_in_executor(
                None, lambda: query_gateway_control(
                    env.root, "quiet-observe-snapshot",
                    params=follow_params(env, started["observer_token"]), timeout=5.0))
            assert snap["valid"] is True and snap["active_count"] == 0
            # Authentication failure and malformed params are None on this client: INVALID for the observer.
            bad = await loop.run_in_executor(
                None, lambda: query_gateway_control(env.root, "quiet-observe-snapshot",
                                                    params=follow_params(env, "forged"), timeout=5.0))
            assert bad is None
        finally:
            await env.server.stop()

    asyncio.run(run())
