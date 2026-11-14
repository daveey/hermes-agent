"""Quiet-observation control verbs: ``quiet-observe-start`` / ``-snapshot`` / ``-end``.

Private (peer-authenticated) handlers on the gateway control socket, used by the attended observer
(``scripts/observe_gateway_quiet.py``) that runs under the claim-quiet lease.

They are *measurement bookkeeping*: they never reserve a registry token, never mutate the registry
and never start work. A snapshot reports the process-wide local-work registry (epoch, active count,
per-kind counts, sticky unknowns, membership generation) plus the original no-work predicates
(agents, workers, held turn leases) and update-collision markers. The registry lock is only taken
inside ``Registry.snapshot()``; every other read happens outside it, bracketed by an epoch check.

Refusals are structured (``valid: False`` + ``reasons``) so the observer can never read an empty
answer as a clean baseline; malformed or unauthenticated calls raise (``ok: false`` on the wire).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import json
import math
import os
import secrets
import threading
import time
from typing import Any

from gateway.control_dispatch import QUIET_BOOKKEEPING_VERBS
from agent.claim_quiet_records import MAX_LEASE_S

logger = logging.getLogger(__name__)

SCHEMA = 1
START, SNAPSHOT, END, CHECK = QUIET_BOOKKEEPING_VERBS
# Schema 1 has no refresh cadence. Both launchd hermes-sync and the worker
# core-update timer publish every 600 s; allow 60 s scheduling jitter.
UPDATE_WINDOW_PUBLISH_INTERVAL_S = 600
UPDATE_WINDOW_PUBLISH_JITTER_S = 60
UPDATE_WINDOW_MAX_AGE_S = UPDATE_WINDOW_PUBLISH_INTERVAL_S + UPDATE_WINDOW_PUBLISH_JITTER_S
UPDATE_WINDOW_SAFETY_MARGIN_S = 120
# quiet-hold's preflight wants this much freshness left for the observer's spawn + handshake.
START_PREFLIGHT_MARGIN_S = 30
_MAX_TEXT = 256
_ENDED_KEPT = 8

_GATEWAY_KEYS = {"pid", "started_at", "instance_id", "code_sha"}
_START_KEYS = {"schema", "episode_id", "producer_pid", "producer_started_at", "producer_incarnation",
               "expected_gateway", "deadline_mono_ns"}
_FOLLOW_KEYS = {"schema", "episode_id", "observer_token", "expected_gateway", "deadline_mono_ns"}

# Process-wide bootstrap capture: ``capture_bootstrap_registry`` runs at the earliest gateway launch
# boundary; the runner (and its constructor) read the same instance from here.
_boot: dict[str, Any] = {"registry": None, "captured": False, "code": None}


# ---- bootstrap binding ---------------------------------------------------------------------

def capture_bootstrap_registry():
    """The registry installed by the bootstrap, or None. Called before config/plugin initialization.

    Presence at this boundary is the provenance: the registry already existed before any gateway
    code could start work. Never installs one (a late install would have no provenance).
    """
    from hermes_cli import local_work

    try:
        registry = local_work.get_registry()
    except RuntimeError:
        registry = None
    _boot["registry"], _boot["captured"] = registry, True
    _boot["code"] = _installed_code_ledger()
    return registry


def _installed_code_ledger():
    """The bootstrap-installed executed-code ledger (``hermes_cli.code_attestation``), or None.

    Its unknowns (an unsupported launch route included) refuse ``quiet-observe-start``; a missing
    ledger is pending coverage. Read here, never installed here (a late install has no provenance).
    """
    from hermes_cli import code_attestation

    return code_attestation._installed


def bootstrap_registry():
    return _boot["registry"]


def bind_registry(runner, registry=None) -> None:
    """Attach the bootstrap-installed registry to ``runner``; absence => unknown ``unattested_bootstrap``.

    A registry that only appears after the bootstrap capture (or when capture never ran: constructor or
    direct entry) is bound without provenance and marked unknown.
    """
    from hermes_cli import local_work

    if registry is None:
        registry = _boot["registry"]
    provenance = registry is not None and _boot["captured"]
    if registry is None:
        try:
            registry = local_work.get_registry()
        except RuntimeError:
            registry = None
    runner._quiet_registry = registry
    runner._quiet_code = _boot["code"] if _boot["captured"] else _installed_code_ledger()
    runner._quiet_bootstrap_provenance = provenance
    runner._quiet_attested = False
    runner._quiet_local_unknowns = set()
    if not provenance:
        if registry is not None:
            registry.mark_unknown("unattested_bootstrap")
        runner._quiet_local_unknowns.add("unattested_bootstrap")


def finalize_attestation(runner) -> bool:
    """Mark startup attestation complete; called with ``publish_gateway_runtime_ready``.

    Only a runner whose registry carries bootstrap provenance can be attested; anything else stays
    ``coverage_pending`` for the whole process lifetime.
    """
    ok = bool(getattr(runner, "_quiet_bootstrap_provenance", False)
              and getattr(runner, "_quiet_registry", None) is not None)
    runner._quiet_attested = ok
    return ok


def register_quiet_control(runner, server) -> QuietControl:
    """Install the three private handlers on ``server`` (before its ``start()``)."""
    if not hasattr(runner, "_quiet_registry"):
        bind_registry(runner)  # constructor/direct entry: no early bind happened
    control = QuietControl(runner)
    runner._quiet_control = control
    server.private_handlers[START] = control.start
    server.private_handlers[SNAPSHOT] = control.snapshot
    server.private_handlers[END] = control.end
    server.private_handlers[CHECK] = control.check
    return control


# ---- validation ----------------------------------------------------------------------------

def _is_int(value) -> bool:
    return type(value) is int


def _text(value, name) -> str:
    if type(value) is not str or not value or len(value) > _MAX_TEXT or not value.isprintable():
        raise ValueError(f"{name} must be bounded printable text")
    return value


def _check_params(params, keys: set[str]) -> dict:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    if set(params) != keys:
        raise ValueError("params fields must be exactly: " + ", ".join(sorted(keys)))
    if not _is_int(params["schema"]) or params["schema"] != SCHEMA:
        raise ValueError("unsupported schema")
    _text(params["episode_id"], "episode_id")
    if not _is_int(params["deadline_mono_ns"]) or params["deadline_mono_ns"] <= 0:
        raise ValueError("deadline_mono_ns must be a positive integer")
    gateway = params["expected_gateway"]
    if not isinstance(gateway, dict) or set(gateway) != _GATEWAY_KEYS:
        raise ValueError("expected_gateway must have exactly: " + ", ".join(sorted(_GATEWAY_KEYS)))
    if not _is_int(gateway["pid"]) or gateway["pid"] <= 0:
        raise ValueError("expected_gateway.pid must be a positive integer")
    started = gateway["started_at"]
    if type(started) not in (int, float, str) or (type(started) is float and not math.isfinite(started)):
        raise ValueError("expected_gateway.started_at must be a number or text")
    _text(gateway["instance_id"], "expected_gateway.instance_id")
    if gateway["code_sha"] is not None:
        _text(gateway["code_sha"], "expected_gateway.code_sha")
    if "producer_pid" in params:
        if not _is_int(params["producer_pid"]) or params["producer_pid"] <= 0:
            raise ValueError("producer_pid must be a positive integer")
        started = params["producer_started_at"]
        if type(started) not in (int, float) or not math.isfinite(started) or started < 0:
            raise ValueError("producer_started_at must be a finite non-negative number")
        _text(params["producer_incarnation"], "producer_incarnation")
    if "observer_token" in params:
        _text(params["observer_token"], "observer_token")
    return params


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


# ---- host-file update markers (shared with the lease-free preflight) -------------------------

def _hold_reasons() -> list[str]:
    try:
        from gateway.session_notifier_drain import held
        return ["update_hold"] if held() else []
    except Exception:  # health: allow BLE001 -- unreadable marker is unknown, never idle
        return ["update_hold_unreadable"]


def _window_status(now: int, *, remaining_s: int = MAX_LEASE_S) -> tuple[dict[str, Any], list[str]]:
    status: dict[str, Any] = {"collision": False, "window_state": None, "window_age_s": None}
    try:
        from hermes_cli import kanban_deploy_window as window
        record = window.read_window(now)
    except Exception:  # health: allow BLE001 -- unreadable deploy window is reported as update_window_unavailable (not idle)
        record = None
    if record is None:
        return status, ["update_window_unavailable"]
    status["window_state"] = record["state"]
    status["window_age_s"] = now - record["observed_at"]
    safe = record["state"] == "idle" or (
        record["state"] == "batching"
        and record["window_at"] - now >= remaining_s + UPDATE_WINDOW_SAFETY_MARGIN_S)
    return status, ([] if safe else [f"update_window_{record['state']}"])


def start_preflight_reasons(*, now: int | None = None, margin_s: int = START_PREFLIGHT_MARGIN_S) -> list[str]:
    """``quiet-observe-start`` refusals readable from host files alone, before any lease exists.

    The same readers and spellings as :meth:`QuietControl.start` (``update:<reason>``, then
    ``update_window_stale``), with the freshness bound tightened by ``margin_s`` so a window that
    would go stale while the observer starts is refused here. Runner-only state (attestation,
    coverage, drain/restart flags, registry unknowns) is the gateway's: the read-only bookkeeping
    verb ``quiet-observe-check`` (:meth:`QuietControl.check`) reports it with start's own 660 s
    window and no margin, and ``quiet-hold`` asks it after this host-file step, before the lease.
    Advisory: the gateway's start remains authoritative.
    """
    now = int(time.time()) if now is None else now
    status, window_reasons = _window_status(now)
    reasons = [f"update:{r}" for r in _hold_reasons() + window_reasons]
    if reasons:
        return reasons
    if status["window_age_s"] is None or status["window_age_s"] > UPDATE_WINDOW_MAX_AGE_S - margin_s:
        return ["update_window_stale"]
    return []


# ---- the handlers --------------------------------------------------------------------------

class QuietControl:
    def __init__(self, runner, *, lease_reader=None, now_ns=None) -> None:
        self._runner = runner
        self._lease_reader = lease_reader
        self._now_ns = now_ns
        self._lock = threading.Lock()  # episode bookkeeping only; never held across a read
        self._episode: dict[str, Any] | None = None
        self._ended: dict[str, tuple[bytes, str]] = {}
        self._sequence = 0
        self._incarnation = secrets.token_hex(8)

    # -- clocks / identity -------------------------------------------------------------------

    def _mono(self) -> int | None:
        if self._now_ns is not None:
            return self._now_ns()
        from hermes_platform import quiet_clock
        return quiet_clock.suspend_inclusive_ns()

    def _gateway_identity(self) -> dict[str, Any]:
        from gateway.status import _get_code_identity_fields, _get_process_start_time

        descriptor = getattr(self._runner, "session_runtime_descriptor", None) or {}
        return {"pid": os.getpid(), "started_at": _get_process_start_time(os.getpid()),
                "instance_id": descriptor.get("instance_id"),
                "code_sha": _get_code_identity_fields().get("code_sha"),
                "incarnation": self._incarnation}

    def _identity_reasons(self, expected: dict, identity: dict) -> list[str]:
        reasons = []
        for key in ("pid", "started_at", "instance_id", "code_sha"):
            if identity.get(key) is None and key != "code_sha":
                reasons.append(f"gateway_{key}_unknown")
            elif expected[key] != identity.get(key):
                reasons.append(f"gateway_{key}_mismatch")
        return reasons

    # -- registry / predicates ---------------------------------------------------------------

    def _registry(self):
        return getattr(self._runner, "_quiet_registry", None)

    def _pending(self, registry) -> list[str]:
        """Reasons the boot/coverage attestation is not complete (start refuses on any)."""
        runner = self._runner
        reasons = []
        if registry is None:
            reasons.append("registry_missing")
        elif not getattr(runner, "_quiet_bootstrap_provenance", False):
            reasons.append("bootstrap_provenance_missing")
        if not getattr(runner, "_quiet_attested", False):
            reasons.append("coverage_pending")
        if registry is not None and callable(getattr(registry, "coverage_pending", None)):
            try:
                reasons.extend(str(r) for r in registry.coverage_pending())
            except Exception:  # health: allow BLE001 -- unreadable coverage is pending coverage
                reasons.append("coverage_unreadable")
        if registry is not None and not callable(getattr(registry, "membership_generation", None)):
            reasons.append("membership_unsupported")
        if getattr(runner, "_quiet_code", None) is None:
            reasons.append("code_attestation_missing")
        return reasons

    def _code_unknowns(self, *, inventory: bool) -> tuple[set[str], dict]:
        """The executed-code ledger's unknowns as ``code:<reason>``. Snapshots read only the latched
        set (no module inventory while the gateway is being measured); start and check inventory
        first. An unreadable ledger is itself an unknown."""
        ledger = getattr(self._runner, "_quiet_code", None)
        extension = []
        empty = {"count": 0, "sha256": hashlib.sha256(b"[]").hexdigest(), "entries": []}
        if ledger is None:
            return set(), empty
        try:
            partition = getattr(ledger, "quiet_unknown_partition", None)
            if callable(partition):
                found, extension = partition(inventory=inventory)
            else:
                found = ledger.unknowns() if inventory else ledger.latched_unknowns()
        except Exception:  # health: allow BLE001 -- an unreadable ledger is unknown, never clean
            return {"code:unreadable"}, empty
        entries = sorted(set(extension))
        report = {"count": len(entries),
                  "sha256": hashlib.sha256(json.dumps(entries, ensure_ascii=True,
                                                       separators=(",", ":")).encode("utf-8")).hexdigest(),
                  "entries": entries[:20]}
        return {f"code:{reason}" for reason in found}, report

    def _registry_view(self, registry, *, inventory: bool = False) -> tuple[dict[str, Any], list[str]]:
        snap = registry.snapshot()
        gen = None
        if callable(getattr(registry, "membership_generation", None)):
            gen = registry.membership_generation()
        code_unknowns, unattested = self._code_unknowns(inventory=inventory)
        unknowns = sorted(set(snap.unknowns) | set(getattr(self._runner, "_quiet_local_unknowns", ()))
                          | code_unknowns)
        view = {"activity_epoch": snap.epoch, "active_count": snap.active,
                "unattested_code": unattested,
                "per_kind": dict(snap.per_kind), "unknowns": unknowns,
                "membership_generation": gen,
                "pending_transitions": getattr(snap, "pending_transitions", 0)}
        membership = getattr(registry, "membership_snapshot", None)
        if callable(membership):
            view["membership"] = membership()
        return view, []

    def _predicates(self) -> tuple[dict[str, Any], list[str]]:
        """Original no-work predicates from their real sources; None + reason when unreadable."""
        runner = self._runner
        reasons: list[str] = []
        out: dict[str, Any] = {}

        def read(name, fn):
            try:
                out[name] = int(fn())
            except Exception:  # health: allow BLE001 -- an unreadable predicate must refuse, not read as zero
                out[name] = None
                reasons.append(f"predicate_unreadable:{name}")

        read("active_agents", runner._active_work_count)  # agents + managed + cron + api + deferred

        def workers():
            from gateway.session_runtime_workers import uncounted_runtime_work
            from tools.async_delegation import _LIVE_STATES, list_async_delegations
            live = sum(1 for r in list_async_delegations() if r.get("status") in _LIVE_STATES)
            return uncounted_runtime_work(runner) + live

        read("workers", workers)

        def leases():
            registry = getattr(runner, "_turn_leases", None)
            if registry is None:
                raise RuntimeError("no turn lease registry")
            return sum(1 for lease in list(registry._leases.values()) if not lease.idle)

        read("held_turn_leases", leases)
        return out, reasons

    def _update_status(self, *, remaining_s: int = MAX_LEASE_S) -> tuple[dict[str, Any], list[str]]:
        """Collision with a core update, from the existing typed markers (never English parsing)."""
        runner = self._runner
        reasons = _hold_reasons()
        if getattr(runner, "_draining", False):
            reasons.append("runner_draining")
        if getattr(runner, "_restart_requested", False):
            reasons.append("restart_requested")
        status, window_reasons = _window_status(int(time.time()), remaining_s=remaining_s)
        reasons += window_reasons
        status["collision"] = bool(reasons)
        status["reasons"] = reasons
        return status, reasons

    # -- measurement -------------------------------------------------------------------------

    def _measure(self, episode_id: str, phase: str, *, full: bool = True,
                 deadline_ns: int | None = None) -> dict[str, Any]:
        """One snapshot: reads outside the registry lock, epoch/generation checked before and after."""
        registry = self._registry()
        reasons: list[str] = []
        if registry is None:
            return {"schema": SCHEMA, "episode_id": episode_id, "phase": phase, "valid": False,
                    "reasons": ["registry_missing"]}
        view = predicates = update = None
        for attempt in (0, 1):
            before = registry.snapshot().epoch
            gen_before = (registry.membership_generation()
                          if callable(getattr(registry, "membership_generation", None)) else None)
            view, _ = self._registry_view(registry)
            predicates, pred_reasons = self._predicates() if full else ({}, [])
            mono = self._mono()
            # Admission reserves the full horizon; capture needs only the unexpired lease.
            remaining = (max(0, math.ceil((deadline_ns - mono) / 1_000_000_000))
                         if deadline_ns is not None and mono is not None else MAX_LEASE_S)
            update, update_reasons = self._update_status(remaining_s=remaining) if full else ({}, [])
            after = registry.snapshot().epoch
            gen_after = (registry.membership_generation()
                         if callable(getattr(registry, "membership_generation", None)) else None)
            if before == after and gen_before == gen_after:
                break
            reasons = []
        else:
            reasons = ["epoch_moved_during_read"]
        if full:
            reasons += pred_reasons
            reasons += [f"update:{r}" for r in update_reasons]
        if view["unknowns"]:
            reasons += [f"unknown:{u}" for u in view["unknowns"]]
        if view["pending_transitions"]:
            reasons.append("membership_transition_pending")  # a served home is joining or leaving
        if full:
            for name in ("active_agents", "workers", "held_turn_leases"):
                if predicates.get(name):
                    reasons.append(f"predicate_nonzero:{name}")
        if view["active_count"]:
            reasons.append("active_work")
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        mono = self._mono()
        return {
            "schema": SCHEMA, "episode_id": episode_id, "phase": phase, "sequence": sequence,
            "server_mono_ns": mono, "server_utc": time.time(),
            "gateway": self._gateway_identity(),
            "registry_provenance": "bootstrap" if getattr(self._runner, "_quiet_bootstrap_provenance", False)
            else "missing",
            "coverage": "attested" if getattr(self._runner, "_quiet_attested", False) else "pending",
            **view, "predicates": predicates, "update": update,
            "valid": not reasons, "reasons": reasons,
        }

    def _refuse(self, episode_id: str, phase: str, reasons: list[str]) -> dict[str, Any]:
        return {"schema": SCHEMA, "episode_id": episode_id, "phase": phase, "valid": False,
                "reasons": reasons}

    # -- lease -------------------------------------------------------------------------------

    def _lease(self):
        if self._lease_reader is not None:
            return self._lease_reader()
        from agent import claim_quiet
        return claim_quiet.inspect()

    def _lease_reasons(self, params: dict, now_ns: int | None) -> list[str]:
        reading = self._lease()
        lease = getattr(reading, "lease", None)
        if getattr(reading, "state", None) != "active" or lease is None:
            return [f"lease_{getattr(reading, 'state', 'unknown')}"]
        reasons = []
        if lease.episode_id != params["episode_id"]:
            reasons.append("lease_episode_mismatch")
        for key, field in (("producer_pid", "producer_pid"), ("producer_started_at", "producer_started_at"),
                           ("producer_incarnation", "producer_incarnation")):
            if key in params and params[key] != getattr(lease, field):
                reasons.append(f"lease_{key}_mismatch")
        if params["deadline_mono_ns"] != lease.deadline_ns:
            reasons.append("deadline_not_lease_deadline")
        if now_ns is None or now_ns >= lease.deadline_ns:
            reasons.append("lease_deadline_reached")
        return reasons

    # -- authentication ----------------------------------------------------------------------

    def _authenticate(self, params: dict, peer: str) -> dict[str, Any] | None:
        """The live episode a request addresses, or None when it addresses an ended one; else raise."""
        with self._lock:
            episode = self._episode
            ended = self._ended.get(params["episode_id"])
        token = _digest(params["observer_token"])
        if episode is not None and episode["episode_id"] == params["episode_id"]:
            if (not hmac.compare_digest(episode["token"], token) or episode["peer"] != peer
                    or episode["deadline_ns"] != params["deadline_mono_ns"]):
                raise PermissionError("observer token does not match this episode")
            return episode
        if ended is not None and hmac.compare_digest(ended[0], token) and ended[1] == peer:
            return None
        raise PermissionError("unknown or foreign episode")

    # -- verbs -------------------------------------------------------------------------------

    def start(self, params, peer):
        _check_params(params, _START_KEYS)
        episode_id = params["episode_id"]
        registry = self._registry()
        reasons = self._pending(registry)
        if reasons:
            return self._refuse(episode_id, "coverage_pending", reasons)
        identity = self._gateway_identity()
        reasons = self._identity_reasons(params["expected_gateway"], identity)
        now = self._mono()
        reasons += self._lease_reasons(params, now)
        if reasons:
            return self._refuse(episode_id, "refused", reasons)
        for reasons in self._state_gates(registry):
            if reasons:
                return self._refuse(episode_id, "refused", reasons)
        now = self._mono()  # deadline re-checked immediately before the episode is armed
        if now is None or now >= params["deadline_mono_ns"]:
            return self._refuse(episode_id, "refused", ["deadline_reached"])
        with self._lock:
            current = self._episode
            if current is not None and current["deadline_ns"] > now and current["episode_id"] != episode_id:
                return self._refuse(episode_id, "refused", ["episode_conflict"])
            if current is not None and current["episode_id"] == episode_id:
                if current["peer"] != peer:
                    return self._refuse(episode_id, "refused", ["episode_conflict"])
                token = current["token_plain"]  # idempotent for the same authenticated episode
            else:
                token = secrets.token_urlsafe(32)
                self._episode = {"episode_id": episode_id, "peer": peer, "token": _digest(token),
                                 "token_plain": token, "deadline_ns": params["deadline_mono_ns"],
                                 "incarnation": self._incarnation}
        result = self._measure(episode_id, "started", deadline_ns=params["deadline_mono_ns"])
        result["observer_token"] = token
        return result

    def _state_gates(self, registry):
        """Start's lease-independent gates after coverage, in start's order (one list each)."""
        view, _ = self._registry_view(registry, inventory=True)
        yield [f"unknown:{u}" for u in view["unknowns"]]
        update, update_reasons = self._update_status()
        yield [f"update:{r}" for r in update_reasons]
        stale = update["window_age_s"] is None or update["window_age_s"] > UPDATE_WINDOW_MAX_AGE_S
        yield ["update_window_stale"] if stale else []

    def check(self, params, peer):
        """Read-only preflight (``hermes kanban quiet-hold --check``): what ``start`` would refuse
        apart from the lease and the observer's identity, without arming an episode, reading or
        taking a lease, or reserving work (a bookkeeping verb: the registry epoch does not move)."""
        if not isinstance(params, dict) or set(params) != {"schema"} or params["schema"] != SCHEMA:
            raise ValueError(f"params fields must be exactly: schema (= {SCHEMA})")
        registry = self._registry()
        reasons = self._pending(registry)
        unknowns: list[str] = []
        if registry is not None:
            for gate in self._state_gates(registry):
                reasons += gate
            unknowns = [r[len("unknown:"):] for r in reasons if r.startswith("unknown:")]
        ledger = getattr(self._runner, "_quiet_code", None)
        try:
            route = ledger.route_report().get("route") if ledger is not None else None
        except Exception:  # health: allow BLE001 -- an unreadable route is reported as none, never invented
            route = None
        _, unattested = self._code_unknowns(inventory=False)
        predicates, predicate_reasons = self._predicates()
        return {"schema": SCHEMA, "phase": "check", "admitted": not reasons, "reasons": reasons,
                "unattested_code": unattested, "predicates": predicates,
                "predicate_diagnostics": predicate_reasons,
                "registry_unknowns": unknowns, "route": route,
                "activity_epoch": registry.snapshot().epoch if registry is not None else None}

    def _follow(self, params, peer, phase):
        _check_params(params, _FOLLOW_KEYS)
        return self._authenticate(params, peer)

    def snapshot(self, params, peer):
        episode = self._follow(params, peer, "snapshot")
        episode_id = params["episode_id"]
        if episode is None:
            return self._refuse(episode_id, "ended", ["episode_ended"])
        reasons = self._identity_reasons(params["expected_gateway"], self._gateway_identity())
        now = self._mono()
        reasons += self._lease_reasons({"episode_id": episode_id, "deadline_mono_ns": params["deadline_mono_ns"]},
                                       now)
        if reasons:
            with self._lock:  # token authority expires with the lease
                if any(r.startswith("lease_") for r in reasons) and self._episode is episode:
                    self._episode = None
                    # Remember it as ended so the observer's best-effort end gets "ended", not a
                    # foreign-episode error.
                    self._ended[episode_id] = (episode["token"], episode["peer"])
                    while len(self._ended) > _ENDED_KEPT:
                        self._ended.pop(next(iter(self._ended)))
            return self._refuse(episode_id, "refused", reasons)
        return self._measure(episode_id, "snapshot", deadline_ns=episode["deadline_ns"])

    def end(self, params, peer):
        episode = self._follow(params, peer, "end")
        episode_id = params["episode_id"]
        if episode is not None:
            with self._lock:
                if self._episode is episode:
                    self._episode = None
                self._ended[episode_id] = (episode["token"], episode["peer"])
                while len(self._ended) > _ENDED_KEPT:
                    self._ended.pop(next(iter(self._ended)))
        result = self._measure(episode_id, "ended", full=False)
        result["valid"] = False  # an ended episode is never evidence
        result["reasons"] = ["episode_ended"]
        return result
