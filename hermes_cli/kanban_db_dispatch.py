"""Dispatcher: crash/stale/orphan detection, failure accounting and the respawn circuit breaker, memory-aware concurrency caps, the one-shot ``dispatch_once`` pass, worker spawning (``_default_spawn``), worker-log rotation and the long-lived ``run_daemon`` loop.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Iterable
from typing import Iterator
from typing import Mapping
from typing import Optional
from typing import TYPE_CHECKING

from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER
from hermes_cli import kanban_db_dispatch_guard_notice as _guard_notice
from hermes_cli.kanban_db_stale_claims import _defer_reclaim_for_live_worker
from hermes_cli.kanban_suspend_ledger import awake_seconds as _awake_seconds, record_dispatch_pass as _record_dispatch_pass
from hermes_cli.kanban_db_dispatch_guard_notice import guard_hold_started_at as _guard_hold_started_at
from hermes_cli import kanban_db_dispatch_quota as _kbq, _startup_fast
from hermes_cli.kanban_db_dispatch_exit_code import exit_code_kind as _exit_code_kind
from hermes_cli.kanban_db_dispatch_allowlist import _dispatch_profile_allowlist, dispatch_profile_allowlist_summary
from hermes_cli import kanban_worker_scope as _worker_scope, kanban_aggregate_scope as _aggregate_scope
from hermes_cli import kanban_host_reboot as _host_reboot, kanban_owner_unavailable as _ou  # [daveey]
from hermes_cli import kanban_owner_death as _owner_death, kanban_parent_unclaimed as _parent_unclaimed  # [daveey]
from hermes_cli import kanban_guard_refusal as _guard_refusal, loop_accounting as _la, kanban_requeue_custody as _requeue_custody  # [daveey]
from hermes_cli.kanban_db_dispatch_crash_accounting import _account_crashes  # [daveey]
from hermes_cli.kanban_failure_accounting import _record_task_failure
from hermes_cli.kanban_db_dispatch_dead_worker import _DeadWorker
from hermes_cli.kanban_db_dispatch_queries import skip_reap
from hermes_cli.kanban_db_dispatch_quiet import QuietResultFields
from hermes_cli.kanban_admission_pools import host_remaining, lane_rows, pickup_budget, pool_admits, pool_take
from hermes_cli.kanban_db_dispatch_clock import record_clock_hold as _record_clock_hold
from hermes_cli.kanban_db_dispatch_rerun import reopened_after
from hermes_cli import kanban_db_dispatch_custody as _custody
from hermes_cli.kanban_db_dispatch_pid import _pid_alive  # re-export
from hermes_cli._startup_fast import project_root_str

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task


# After this many consecutive non-success attempts on a task/profile the
# dispatcher parks the task in ``blocked`` with a reason — prevents retry storms.
DEFAULT_FAILURE_LIMIT = 2

# Keep a little wall-clock budget for the worker to observe a terminal timeout
# and make a terminal board call (kanban_block/kanban_complete/kanban_request_review)
# before max_runtime_seconds kills it.
KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS = 30

# A healthy worker is still alive for a while after kanban_complete /
# kanban_request_review returns (final assistant turn, session persistence), so
# a run's retained worker is only reaped once ended_at is at least this old
# (two default dispatch ticks).
TERMINAL_WORKER_REAP_GRACE_SECONDS = 120

# ---------------------------------------------------------------------------
# Respawn guard constants
# ---------------------------------------------------------------------------

# Patterns in last_failure_error that indicate a quota / auth blocker.
# These errors won't resolve by retrying immediately — auto-block instead.
# The auth family is a curated list, not an open `auth\w*` stem: that stem
# also matched ordinary English words like "author"/"authored"/"authoring"/
# "authoritative" in worker progress prose, parking a healthy card forever
# (#117009).
_RESPAWN_BLOCKER_RE = re.compile(
    r"\b(quota|rate[\s_\-]?limit|429|403|"
    r"auth|authenticat(?:e|es|ed|ing|ion)|authoriz(?:e|es|ed|ing|ation)|"
    r"authoris(?:e|es|ed|ing|ation)|authz|"
    r"unauthorized|forbidden|billing|subscription|"
    r"access[\s_]denied|permission[\s_]denied|"
    r"invalid[\s_]api[\s_]key)\b",
    re.IGNORECASE,
)

# ``last_failure_error`` written by the completion gates (PR acceptance, ...) starts with
# this: the worker ran, its completion was refused, and the next run must retry or rebind.
# It is never a worker/provider quota or auth failure, whatever words the gate detail
# contains, so it never matches ``blocker_auth``. [daveey]
_COMPLETION_GATE_ERROR_PREFIXES = ("PR acceptance ",)

# A guard hold that never ends parks a card in ``ready`` forever with nothing on the board
# saying so (2026-10-21: ``blocker_auth`` held t_10de9798 for hours; a held card never
# runs, so ``consecutive_failures`` never grows and the breaker never trips). Past the cap
# the dispatcher blocks the card with the guard reason so it shows as waiting on someone.
# ``blocker_auth`` gets the short cap: a real auth failure never clears without a human. A
# quota wall does clear on its own, so it never reaches ``blocker_auth`` and a quota-flavored
# hold is re-queued at the cap instead of blocked (``kanban_db_dispatch_quota``). Every other guard
# is time-bounded by design and only hits the long cap when something is wrong.
# ``shared_blocker`` holds are exempt: they are a declared wall another card is probing.
# Override via ``HERMES_KANBAN_GUARD_HOLD_CAP_SECONDS`` / ``..._LONG_SECONDS``. [daveey]
DEFAULT_GUARD_HOLD_CAP_SECONDS = 30 * 60
DEFAULT_GUARD_HOLD_CAP_LONG_SECONDS = 6 * 3600
_GUARD_HOLD_SHORT_CAP_REASONS = frozenset({"blocker_auth"})
_GUARD_HOLD_EXEMPT_REASONS = frozenset({"shared_blocker", "model_provider_mismatch", "model_pin_not_in_routing_registry", "subscription_quota_reset"})

def _env_seconds(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


def guard_hold_cap_seconds(reason: str) -> Optional[int]:
    """Max seconds the respawn guard may hold a card for ``reason`` (None = no cap)."""
    if reason in _GUARD_HOLD_EXEMPT_REASONS:
        return None
    if reason in _GUARD_HOLD_SHORT_CAP_REASONS:
        return _env_seconds("HERMES_KANBAN_GUARD_HOLD_CAP_SECONDS", DEFAULT_GUARD_HOLD_CAP_SECONDS)
    return _env_seconds("HERMES_KANBAN_GUARD_HOLD_CAP_LONG_SECONDS", DEFAULT_GUARD_HOLD_CAP_LONG_SECONDS)


def _is_completion_gate_error(err: str) -> bool:
    return err.lstrip().startswith(_COMPLETION_GATE_ERROR_PREFIXES)


# Within this window a completed run counts as "recent proof"; don't re-spawn.
_RESPAWN_GUARD_SUCCESS_WINDOW = 3600  # 1 hour

# Cooldown after a rate-limited (quota-wall) requeue before re-spawning. Without
# it the task would re-spawn on the very next tick and bounce off the same quota
# wall, burning a worker slot every tick for hours. Overridable via
# ``HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS``.
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 300  # 5 minutes
# Spacing after a run lost to its profile owner's restart (``owner_restart_lost``): long enough for
# the drain to end and the next owner to start, so the respawn does not dial the same drain. [daveey]
OWNER_RESTART_COOLDOWN_SECONDS = 120


def _is_owner_restart_run(run) -> bool:
    """A ``task_runs`` row the sweep booked as lost to an owner restart (neutral, never a failure)."""
    return (run["outcome"] or "") == "reclaimed" and bool(_kb._json_dict(run["metadata"]).get("owner_restart"))

# Within this window a GitHub PR URL in a comment blocks re-spawn.
_RESPAWN_GUARD_PR_WINDOW = 86400  # 24 hours

_RESPAWN_GUARD_PR_URL_RE = re.compile(r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+", re.IGNORECASE)


@dataclass
class DispatchResult(QuietResultFields):
    """Outcome of a single ``dispatch`` pass.
    ``kanban.default_assignee`` applied this tick before spawning (#27145). Surfaces the auto-assignment to
    telemetry / CLI / dashboard so the operator can see when the dispatcher is acting on the fallback rule
    ``kanban.max_in_progress_per_profile`` (#21582): ``(task_id, assignee, current_running_count)``.
    NOT an operator-actionable failure — the task will be picked up on a subsequent tick when the assignee
    has capacity. Separate bucket so telemetry / dashboards can show "this profile is busy" vs the board's
    dispatch lock (issue #35240). A losing dispatcher does no DB writes this tick; the holder is progressing.
    """

    scheduling_admitted: bool = False
    """Real tick reached scheduling after global capacity gates; not task delivery proof."""
    skipped_owned: bool = False
    dispatch_owner: Optional[str] = None
    ownership_error: Optional[str] = None
    reclaimed: int = 0
    promoted: int = 0
    reconciled_orphans: list[str] = field(default_factory=list)
    """``running`` cards requeued by :func:`reconcile_orphaned_running` (broken claim bookkeeping, dead/gone worker)."""
    released_leftover_claims: list[str] = field(default_factory=list)  # dead local claims cleared [daveey]
    reaped_terminal_workers: list[str] = field(default_factory=list)
    """Task ids whose worker outlived its closed run and was terminated by :func:`reap_terminal_workers`."""
    stopped_run_scopes: list[str] = field(default_factory=list)  # ended runs' scopes stopped (kanban_worker_scope) [daveey]
    supervisor_requests: list = field(default_factory=list)  # (request id, status) executed this tick [daveey]
    spawned: list[tuple[str, str, str]] = field(default_factory=list)
    """``(task_id, assignee, workspace_path)`` triples."""
    skipped_unassigned: list[str] = field(default_factory=list)
    """Ready task ids with no assignee at all — operator-actionable (usually a misfiled task waiting for routing)."""
    auto_assigned_default: list[str] = field(default_factory=list)
    """Unassigned task ids given ``kanban.default_assignee`` this tick (the fallback rule, not an explicit assignment)."""
    skipped_nonspawnable: list[str] = field(default_factory=list)
    """Ready task ids whose assignee names a control-plane lane (e.g. a Claude
    Code terminal like ``orion-cc``), not a Hermes profile. Expected steady-state
    on multi-lane setups, NOT operator-actionable; tracked apart so health
    telemetry can tell "stuck" from "correctly idle"."""
    skipped_per_profile_capped: list[tuple[str, str, int]] = field(default_factory=list)
    owner_down: list[tuple[str, str, str]] = field(default_factory=list)  # (task, profile, code) held, unclaimed [daveey]
    """``(task_id, assignee, current_running_count)`` deferred because the
    assignee is at ``kanban.max_in_progress_per_profile``. Picked up on a later
    tick; separate bucket so dashboards show "profile busy" vs "stuck"."""
    skipped_ineligible: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, reason)`` ready/review cards this machine cannot lease (``runs_on`` / creator labels exclude
    it, ``pinned_only``): the Board would refuse the claim (``labels_mismatch``), so they are never candidates
    and never hold a slot or the review reservation. [daveey]"""
    claim_deferred: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, reason)`` whose claim the Board lease deferred this tick (``lease_held``, ``labels_mismatch``,
    ``offline``). The tick moves on to the next candidate; the stuck warning names these. [daveey]"""
    admission_deferred: list[str] = field(default_factory=list)  # hub claim screen left unclaimed, unjudged [daveey]
    crashed: list[str] = field(default_factory=list)  # task ids reclaimed because their worker PID disappeared
    capsule_refused: list[tuple[str, str]] = field(default_factory=list)  # (task_id, code): kanban_capsule_admission [daveey]
    pinned_quota_held: list = field(default_factory=list)  # (task_id, model, provider, reset): kanban_dispatch_skip
    llm_paced: list = field(default_factory=list)  # (task_id, detail, until, extra): the quota-pace governor's holds
    remote_archived: list[str] = field(default_factory=list)  # archived on another gateway's stop (kanban_remote_stop)
    auto_blocked: list[str] = field(default_factory=list)
    """Task ids auto-blocked by the spawn-failure circuit breaker."""
    agent_block_escalated: list[str] = field(default_factory=list)
    """Agent-owned blocks older than 2h re-spawned through triage."""
    ask_rechecked: list[str] = field(default_factory=list)
    """Standing human asks found void (relay lint, every named card closed) and moved to agent triage."""
    timed_out: list[str] = field(default_factory=list)
    """Task ids whose workers exceeded ``max_runtime_seconds``."""
    woken_continuations: list[str] = field(default_factory=list)
    """Task ids whose ``kanban_continue(resume_after_seconds=...)`` wait came due this tick."""
    stale: list[str] = field(default_factory=list)
    """Task ids reclaimed for no heartbeat within ``dispatch_stale_timeout_seconds``."""
    respawn_guarded: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, reason)`` skipped by the respawn guard: ``"blocker_auth"``
    (quota/auth error — also auto-blocked), ``"shared_blocker"`` (parked behind
    a declared shared blocker another card is probing), ``"recent_success"`` (completed run
    within guard window), ``"active_pr"`` (GitHub PR URL in a recent comment)."""
    no_progress_held: list[tuple[str, str]] = field(default_factory=list)  # (task_id, action): kanban_no_progress
    clock_held: dict[str, tuple[str, int]] = field(default_factory=dict)
    """``task_id -> (guard reason, epoch the hold ends)`` for guarded cards waiting on a known clock (quota reset,
    cooldowns; ``kanban_db_dispatch_clock``). The stuck warning names the deadline and stays quiet when every
    lane candidate is here. [daveey]"""
    lane_candidates: int = field(default=0, compare=False)
    """Unclaimed ready (+ review, when it dispatches) rows this tick enumerated, before eligibility. A census, not an
    outcome, so it is left out of ``==``: callers compare results to assert what a tick did. [daveey]"""
    shared_blocker_released: list[str] = field(default_factory=list)
    """Blocked task ids unblocked because a probe cleared the shared blocker
    they were parked behind (``kanban_shared_blockers``). [daveey]"""
    shared_blocker_probed: list[str] = field(default_factory=list)
    """Blocked task ids unblocked as the probe of an open shared blocker that had no
    ready/running card behind it (``probe_stranded_blockers``). [daveey]"""
    quota_block_released: list[str] = field(default_factory=list)
    """Guard-hold blocks on a quota wall released once another card completed after them
    (``kanban_db_dispatch_quota.release_recovered_quota_blocks``). [daveey]"""
    rate_limited: list[str] = field(default_factory=list)
    """Task ids whose workers bailed on a provider rate-limit / quota wall
    (EX_TEMPFAIL sentinel exit) and were released to ``ready`` WITHOUT counting
    a failure — a long quota window must never trip the circuit breaker."""
    skipped_locked: bool = False
    """True when another process held the board's dispatch lock: this tick did
    no DB writes; the lock holder is making progress on the same board."""
    at_capacity: Optional[str] = None
    """``"max_in_progress=6/6"`` / ``"max_spawn=3/3"`` when the host or board concurrency cap was full this
    tick, so no worker could start. A ready queue held only by a full cap is correctly idle, not stuck:
    the "dispatcher stuck" warning names it instead of blaming profile health. [daveey]"""
    admission_budget: Any = None  # opted-in hub: this tick's PoolBudget (kanban_admission_pools)
    memory_pressure: Optional[str] = None
    """Memory pressure that restricted this tick: ``"critical"`` (no new
    workers), ``"elevated"`` (at most one), ``None`` (no restriction).
    Reclaim/promotion bookkeeping still ran; deferred tasks stay queued."""
    core_update_drain_hold: Optional[dict] = None  # live hermes-sync hold that refused every claim [daveey]


from hermes_cli.kanban_db_dispatch_stuck import (  # re-exports for the stuck detectors [daveey]
    all_at_capacity, all_clock_held, all_quiet_deferred, describe_suppression, stuck_warning)


# Bounded registry of recently-reaped worker exits, filled by the reap loop in
# ``dispatch_once`` and read by ``detect_crashed_workers`` to classify a dead-pid
# task. Entry: ``pid -> (raw_wait_status, reaped_at_epoch)``; raw status kept so
# both WIFEXITED/WEXITSTATUS and WIFSIGNALED can be consulted. Trimmed by age
# plus a total size cap. Process-local by nature (``waitpid`` only reaps our own
# children): a per-tick ``hermes kanban dispatch`` process finds it empty, so
# ``_classify_dead_worker_exit`` falls back to the exit trailer the worker
# leaves in its own log (``KANBAN_WORKER_EXIT_TRAILER``).
_RECENT_WORKER_EXIT_TTL_SECONDS = 600
_RECENT_WORKER_EXITS_MAX = 4096
_recent_worker_exits: dict[int, tuple[int, float]] = {}

# A child's exit code is only recoverable through a live handle (Windows has no
# ``waitpid(-1)``; on POSIX ``waitpid(-1)`` would steal other children's status), so
# ``_default_spawn`` parks each worker's ``Popen`` here and ``reap_worker_zombies`` polls it. Entry: ``pid -> Popen``.
_live_worker_procs: dict[int, subprocess.Popen] = {}


def _wait_status_from_returncode(returncode: int) -> int:
    """Encode a ``Popen.returncode`` in the wait-status layout the registry stores."""
    return (int(returncode) & 0xFF) << 8


def _record_worker_exit(pid: int, raw_status: int) -> None:
    """Record a reaped child's exit status; duplicate pids overwrite (latest wins)."""
    if not pid or pid <= 0:
        return
    now = time.time()
    _recent_worker_exits[int(pid)] = (int(raw_status), now)
    if len(_recent_worker_exits) > _RECENT_WORKER_EXITS_MAX // 2:
        cutoff = now - _RECENT_WORKER_EXIT_TTL_SECONDS
        for _pid in [p for p, (_s, t) in _recent_worker_exits.items() if t < cutoff]:
            _recent_worker_exits.pop(_pid, None)
    if len(_recent_worker_exits) > _RECENT_WORKER_EXITS_MAX:
        # Drop oldest half.
        ordered = sorted(_recent_worker_exits.items(), key=lambda kv: kv[1][1])
        for _pid, _ in ordered[: len(ordered) // 2]:
            _recent_worker_exits.pop(_pid, None)


def _classify_worker_exit(pid: int) -> tuple[str, Optional[int]]:
    """``(kind, code)`` for a reaped worker PID: ``clean_exit`` (rc 0 while
    still ``running`` = protocol violation), ``rate_limited``
    (``KANBAN_RATE_LIMIT_EXIT_CODE``, never counts as a failure),
    ``nonzero_exit``, ``signaled`` (``code`` is the signal), ``unknown`` (pid
    not in the reap registry; ``code`` None)."""
    entry = _recent_worker_exits.get(int(pid))
    if entry is None:
        return ("unknown", None)
    raw, _ = entry
    # Bit-level POSIX wait-status decode instead of os.WIFEXITED/WEXITSTATUS/
    # WIFSIGNALED/WTERMSIG: those helpers do not exist on Windows, where the
    # registry is fed by reap_worker_zombies' Popen poll. Low 7 bits = signal
    # (0 = normal exit, 0x7F = stopped), bits 8-15 = exit code.
    raw = int(raw)
    signal_number = raw & 0x7F
    if signal_number == 0:
        return _exit_code_kind((raw >> 8) & 0xFF)
    if signal_number != 0x7F:
        return ("signaled", signal_number)
    return ("unknown", None)


_EXIT_TRAILER_RE = re.compile(
    r"^" + re.escape(KANBAN_WORKER_EXIT_TRAILER) + r"(\d+)\s*$", re.MULTILINE,
)


def _worker_log_exit_code(task_id: str, board: Optional[str] = None, *, run_id=None, log: Optional[str] = None) -> Optional[int]:
    """Exit code from the trailer the worker CLI wrote to its own log; None when absent.

    The durable twin of ``_recent_worker_exits``: written by the worker itself
    (``hermes_cli.quiet_single_query.exit_single_query``), so it is there whether
    or not the process running this sweep ever reaped the worker. Last trailer
    wins — the log is append-mode across re-runs. ``log``: the whole log a caller holds (not re-read).
    """
    try:
        raw = log if log is not None else _kb.read_worker_log(task_id, tail_bytes=None if run_id is not None else 4000, board=board)
    except Exception:
        return None
    # A log with no run marker predates it (worker spawned before the deploy): last trailer wins, as before.
    scoped = current_run_log(raw or '', run_id=run_id) if run_id is not None and '[kanban-worker-start]' in (raw or '') else raw or ''
    matches = _EXIT_TRAILER_RE.findall(scoped)
    return int(matches[-1]) if matches else None


def reap_worker_zombies() -> list[int]:
    """Reap registered worker handles. Drop a non-Popen so a missing poll() cannot traceback a tick."""
    reaped: list[int] = []
    for pid, proc in list(_live_worker_procs.items()):
        poll = getattr(proc, "poll", None)
        returncode = poll() if callable(poll) else None
        if not isinstance(returncode, int):
            if not callable(poll):
                _live_worker_procs.pop(pid, None)
                _custody.on_dropped(pid, proc)
            continue
        _record_worker_exit(
            pid, _wait_status_from_returncode(returncode) if returncode >= 0 else -returncode)
        _live_worker_procs.pop(pid, None)
        _custody.on_reaped(pid, proc)
        reaped.append(pid)
    return reaped


# ``worker_started_at`` value for a spawn whose fingerprint could not be captured. Distinct from the
# NULL legacy row (pre-fingerprint spawn): such a worker is held (its claim is never released beside
# the live PID) but NEVER signalled — missing process identity is refusal, not permission (#99558).
UNVERIFIED_WORKER_FINGERPRINT = "unverified"


def _process_fingerprint(pid: int) -> Optional[str]:
    """Restart-stable identity of a live process: ``"<instantiation epoch>|<start time>"``. The start
    time alone (``/proc/<pid>/stat`` field 22 on Linux) is clock ticks since THIS boot, so a row that
    survives a reboot could match an unrelated process with the same PID and the same tick value;
    ``gateway.drain_control.current_instantiation_epoch`` (``boot_id`` + PID-1 start) changes on every
    reboot / container recreate, so the composed value never survives one. ``None`` when unreadable."""
    from gateway.drain_control import current_instantiation_epoch
    from gateway.status import get_process_start_time
    start = get_process_start_time(int(pid))
    if start is None:
        return None
    epoch = current_instantiation_epoch()
    # [daveey] macOS: psutil's start time is ``kern.boottime`` + the process's uptime offset, so it
    # moves whenever the kernel re-anchors boottime (sleep/wake, NTP step). Stamp the boottime read
    # beside it ("<start>@<boottime>") so ``_pid_recycled`` can compare the boot-relative offset, which
    # those adjustments leave unchanged. Linux ticks are already boot-relative (epoch is non-empty).
    boot = None if epoch else _darwin_boottime_centis()
    return f"{epoch}|{start}" if boot is None else f"{epoch}|{start}@{boot}"


def _darwin_boottime_centis() -> Optional[int]:
    """``kern.boottime`` in centiseconds (macOS only), or ``None`` when unavailable."""
    if sys.platform != "darwin":
        return None
    try:
        import ctypes
        import ctypes.util

        class _Timeval(ctypes.Structure):
            _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_int32)]

        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        tv = _Timeval()
        size = ctypes.c_size_t(ctypes.sizeof(tv))
        if libc.sysctlbyname(b"kern.boottime", ctypes.byref(tv), ctypes.byref(size), None, 0) != 0:
            return None
        if tv.tv_sec <= 0:
            return None
        return int(tv.tv_sec) * 100 + int(tv.tv_usec) // 10_000
    except Exception:
        return None


def _start_components(start: str) -> tuple[int, Optional[int]]:
    """``"<start>[@<boottime>]"`` -> ``(start, boottime | None)``; raises ``ValueError`` on junk."""
    abs_part, sep, boot_part = start.partition("@")
    return int(abs_part), (int(boot_part) if sep else None)


def _worker_alive(pid: Optional[int], started_at) -> bool:
    """True when ``pid`` is live AND is still the worker we spawned. ``started_at`` is the fingerprint
    recorded by ``_set_worker_pid``; after a reboot (or any PID recycle) an unrelated process can own
    the number, so bare existence is never enough to extend a claim or to signal. A legacy row without
    a fingerprint keeps the existence answer: killing it is the pre-fingerprint behaviour and the row is
    rewritten with a fingerprint on its next spawn. An UNVERIFIED spawn also keeps the existence answer
    (a claim is never released beside a possibly-live worker) but ``_terminate_reclaimed_worker``
    refuses to signal it."""
    if not _kb._pid_alive(pid):
        return False
    if started_at == UNVERIFIED_WORKER_FINGERPRINT:
        return True
    return not _pid_recycled(pid, started_at)


def _pid_recycled(pid: Optional[int], started_at) -> bool:
    """True when a live ``pid`` is NOT the process fingerprinted at spawn (or the fingerprint can no
    longer be read). Signalling it would hit a stranger. ``None`` fingerprint = legacy row, never
    recycled; the UNVERIFIED marker is always foreign. An integer fingerprint (rows written before the
    boot witness was added) compares the start time only.

    [daveey] The start times are compared within ``START_TIME_DRIFT_TOLERANCE`` (2 s), not exactly. On
    macOS the psutil start time follows ``kern.boottime``, which moves on sleep/wake and clock sync. A
    restarted gateway read every live worker 1.00 s off its spawn stamp, booked all of them crashed in
    one tick, blocked their cards as a "systemic" failure, and respawned duplicates beside them. The
    instantiation epoch is still compared exactly, so a row that survived a reboot stays foreign."""
    if started_at is None or not pid:
        return False
    if started_at == UNVERIFIED_WORKER_FINGERPRINT:
        return True
    from gateway.status import get_process_start_time, start_time_fingerprints_match
    if isinstance(started_at, str) and "|" in started_at:
        current_fp = _process_fingerprint(int(pid))
        if current_fp is None:
            return True
        rec_epoch, _, rec_start = started_at.partition("|")
        cur_epoch, _, cur_start = current_fp.partition("|")
        if rec_epoch != cur_epoch:
            return True
        try:
            rec_abs, rec_boot = _start_components(rec_start)
            cur_abs, cur_boot = _start_components(cur_start)
        except (TypeError, ValueError):
            return current_fp != started_at
        # Same process when the absolute start agrees within drift tolerance (legacy rows, small NTP
        # slews) OR, when both sides carry a boottime stamp, the boot-relative start agrees (any size of
        # boottime re-anchor). A recycled PID started after ours died: both offsets are off by at least
        # our worker's lifetime, so neither branch matches it.
        if start_time_fingerprints_match(rec_abs, cur_abs):
            return False
        if rec_boot is not None and cur_boot is not None:
            return not start_time_fingerprints_match(rec_abs - rec_boot, cur_abs - cur_boot)
        return True
    current = get_process_start_time(int(pid))
    if current is None:
        return True
    try:
        return not start_time_fingerprints_match(started_at, current)
    except (TypeError, ValueError, OverflowError):  # Overflow: inf, e.g. a TEXT fingerprint SQLite coerced
        return True


def _kill_fn(signal_fn) -> Optional[Callable[[int, int], None]]:
    """``signal_fn`` test hook, else ``os.kill`` when the platform has one."""
    if signal_fn is not None:
        return signal_fn
    return os.kill if hasattr(os, "kill") else None


def _poll_worker_exit(pid: int, started_at: Optional[int] = None) -> bool:
    """Poll ~5 s (10 x 0.5 s) for ``pid`` to die; True once it is gone."""
    for _ in range(10):
        if not _worker_alive(pid, started_at):
            return True
        time.sleep(0.5)
    return False


def _terminate_reclaimed_worker(
    pid: Optional[int],
    claim_lock: Optional[str],
    *,
    signal_fn=None,
    started_at=None,
    before_signal=None,
) -> dict[str, Any]:
    """Best-effort host-local worker termination for reclaim paths. ``started_at`` is the spawn-time
    fingerprint: when the live process no longer matches it, the PID was recycled and nothing is
    signalled — the worker is gone, which is what the reclaim wanted (``terminated`` = True). An
    UNVERIFIED spawn (fingerprint capture failed) that is still live is never signalled either, but
    it is reported as surviving (``signal_refused``) so the reclaim holds the claim instead of
    spawning a duplicate beside it. ``before_signal`` adds a strict modern identity fence at each
    signal boundary; callers without it retain legacy drift-tolerant identity policy."""
    info: dict[str, Any] = {
        "prev_pid": int(pid) if pid else None,
        "host_local": False,
        "termination_attempted": False,
        "terminated": False,
        "sigkill": False,
    }
    if not pid or pid <= 0 or not claim_lock:
        return info
    if not _kb._claim_is_local(claim_lock):
        return info
    info["host_local"] = True

    kill = _kill_fn(signal_fn)
    if kill is None:
        return info
    if started_at == UNVERIFIED_WORKER_FINGERPRINT:
        # Never signal by bare number: a dead PID is "gone" (reclaim proceeds), a live one is held.
        info["signal_refused"] = True
        info["terminated"] = not _kb._pid_alive(pid)
        return info
    if _kb._pid_alive(pid) and _pid_recycled(pid, started_at):
        info["terminated"] = True
        info["pid_recycled"] = True
        return info

    if before_signal is not None and not before_signal():
        info['signal_refused'] = True
        return info
    info["termination_attempted"] = True
    try:
        kill(int(pid), signal.SIGTERM)
    except OSError:
        # Already gone = successful termination (Windows reports an absent pid as a bare OSError, not
        # ProcessLookupError). Leaving terminated=False would make the reclaim defer forever.
        info["terminated"] = not _worker_alive(pid, started_at)
        return info

    if _poll_worker_exit(pid, started_at):
        info["terminated"] = True
        return info
    if _worker_alive(pid, started_at):
        if before_signal is not None and not before_signal():
            info['signal_refused'] = True
            return info
        from hermes_cli.kanban_db_worker_termination import sigkill
        if not sigkill(kill, pid):
            return info
        info["sigkill"] = True
    info["terminated"] = not _worker_alive(pid, started_at)
    return info


def reap_terminal_workers(conn: sqlite3.Connection, *, signal_fn=None) -> list[str]:
    """End host-local workers that outlived their run (issue #111791) — a worker
    that called ``kanban_complete`` and then hung keeps its ``state.db`` sidecar
    fds open and no ``running``-only sweep can see it once ``tasks.worker_pid`` is
    cleared. Keys on the closed ``task_runs`` row's retained pid + spawn
    fingerprint: a legacy row (NULL fingerprint) or a recycled PID is never
    signalled; a pid that is simply gone just has its evidence cleared. A run
    that ended less than ``TERMINAL_WORKER_REAP_GRACE_SECONDS`` ago is left
    alone so a worker still finalising after its own transition is not killed.
    One row's failure (signal, /proc probe) is logged and skips only that row.
    Returns the task ids whose worker was terminated."""
    rows = conn.execute(
        "SELECT id, task_id, worker_pid, worker_started_at, claim_lock FROM task_runs "
        "WHERE ended_at IS NOT NULL AND ended_at <= ? "
        "AND worker_pid IS NOT NULL AND worker_started_at IS NOT NULL",
        (int(time.time()) - TERMINAL_WORKER_REAP_GRACE_SECONDS,),
    ).fetchall()
    host_prefix = _kb._host_prefix()
    reaped: list[str] = []
    for row in rows:
        try:
            _reap_terminal_worker_row(conn, row, host_prefix, signal_fn, reaped)
        except Exception:
            _kb._log.debug(
                "kanban dispatch: terminal worker reap failed for run %s (task %s)",
                row["id"], row["task_id"], exc_info=True,
            )
    return reaped


def _reap_terminal_worker_row(conn, row, host_prefix: str, signal_fn, reaped: list[str]) -> None:
    pid, fingerprint = int(row["worker_pid"]), row["worker_started_at"]
    if skip_reap(conn, row, pid, host_prefix):
        return
    if fingerprint == UNVERIFIED_WORKER_FINGERPRINT and _kb._pid_alive(pid):
        return  # unproven identity: never signalled; its evidence is cleared once the pid is gone
    alive = _worker_alive(pid, fingerprint)
    termination = None
    if alive:
        termination = _terminate_reclaimed_worker(
            pid, row["claim_lock"], signal_fn=signal_fn, started_at=fingerprint)
        if not termination["terminated"]:
            return  # still alive: try again next tick
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET worker_pid = NULL, worker_started_at = NULL "
            "WHERE id = ? AND worker_pid = ? AND worker_started_at = ?",
            (row["id"], pid, fingerprint),
        )
        if alive:
            _kb._append_event(
                conn, row["task_id"], "terminal_worker_reaped",
                {"pid": pid, "worker_started_at": fingerprint, **termination}, run_id=row["id"],
            )
    if alive:
        reaped.append(row["task_id"])


def _worker_survived_termination(termination: dict) -> bool:
    """True when we tried to kill our own host-local worker and it is still alive.

    Reclaiming then would release the claim and spawn a second worker while the
    first still runs — the duplication loop. Only host-local workers we actually
    signalled count; a non-local lock or no-op attempt (no ``os.kill``) must fall
    through to the normal release path since we cannot manage that worker anyway.
    """
    return bool(
        termination.get("host_local")
        and (termination.get("termination_attempted") or termination.get("signal_refused"))
        and not termination.get("terminated")
    )


def heartbeat_worker(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    note: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Record a ``heartbeat`` event + touch ``last_heartbeat_at``.

    Liveness signal orthogonal to the PID check: a worker whose forked child
    (train loop, crawl) is stuck can still have a live Python process.
    Returns False if the task is not running or its claim expired.
    """
    now = int(time.time())
    with _kb.write_txn(conn):
        # Healthy progress supersedes the prior attempt's dispatch diagnostic.
        # Keep failure counters/history intact; stale workers cannot clear it.
        sql = ("UPDATE tasks SET last_heartbeat_at = ?, last_failure_error = NULL "
               "WHERE id = ? AND status = 'running'")
        params: tuple = (now, task_id)
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params += (int(expected_run_id),)
        cur = conn.execute(sql, params)
        if cur.rowcount != 1:
            return False
        run_id = (
            int(expected_run_id)
            if expected_run_id is not None
            else _kb._current_run_id(conn, task_id)
        )
        if run_id is not None:
            conn.execute("UPDATE task_runs SET last_heartbeat_at = ? WHERE id = ?", (now, run_id))
        _kb._append_event(
            conn, task_id, "heartbeat",
            {"note": note} if note else None,
            run_id=run_id,
        )
    return True


def enforce_max_runtime(conn: sqlite3.Connection, *, signal_fn=None) -> list[str]:
    """Terminate workers whose per-task ``max_runtime_seconds`` has elapsed.

    SIGTERM, short grace, then SIGKILL. Emits ``timed_out`` and restores the
    task's source phase so the next tick re-spawns the same kind of worker —
    unless the circuit breaker already gave up, leaving it blocked. Host-local
    only (same reasoning as ``detect_crashed_workers``). ``signal_fn`` is a test hook.
    """
    timed_out: list[str] = []
    now = int(time.time())
    host_prefix = _kb._host_prefix()

    rows = conn.execute(
        "SELECT t.id, t.current_run_id, t.worker_pid, t.worker_started_at, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at, "
        "       t.max_runtime_seconds, t.claim_lock "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running' AND t.max_runtime_seconds IS NOT NULL "
        "  AND COALESCE(r.started_at, t.started_at) IS NOT NULL "
        "  AND t.worker_pid IS NOT NULL"
    ).fetchall()
    from hermes_cli.kanban_owner_recovery import owner_reclaim_blocked, owner_reclaim_blocked_after_kill, terminate_modern_bindings
    from hermes_cli.kanban_failure_release import queued
    for row in rows:
        if queued(conn, row["current_run_id"]):
            continue  # Await authoritative feed; never signal an already-released attempt again.
        if owner_reclaim_blocked(conn, row["id"], for_termination=True, selected=row):
            continue
        lock = row["claim_lock"] or ""
        if not _kb._claim_is_local(lock, host_prefix):
            continue
        # Runtime is per attempt: ``tasks.started_at`` records the FIRST start,
        # so retries must be measured from the active task_runs row.
        # [daveey] Awake time only: a host suspend (``kanban_suspend_ledger``) is not runtime.
        elapsed = _awake_seconds(conn, _kb._to_epoch(row["active_started_at"]) or now, now)  # unreadable: never kill
        limit = int(row["max_runtime_seconds"])
        if elapsed < limit or terminate_modern_bindings(conn, row, signal_fn) is not None:
            continue

        pid = int(row["worker_pid"])
        tid = row["id"]
        started_at = _kb._row_get(row, "worker_started_at")
        if started_at == UNVERIFIED_WORKER_FINGERPRINT and _kb._pid_alive(pid):
            # Fingerprint capture failed at spawn: we cannot prove this live PID is our worker, so
            # it is neither signalled nor released beside (duplicate). It is reclaimed once it exits.
            _kb._log.warning("kanban: task %s worker pid %s exceeded max runtime but has no verified "
                             "identity; not signalled", tid, pid)
            continue
        # SIGTERM, then SIGKILL after 5 s; a recycled PID is never signalled (already gone).
        killed = False
        kill = _kill_fn(signal_fn)
        if kill is not None and not (_kb._pid_alive(pid) and _pid_recycled(pid, started_at)):
            with contextlib.suppress(ProcessLookupError, OSError):
                kill(pid, signal.SIGTERM)
            # Short polling wait — no time.sleep on the write txn.
            _poll_worker_exit(pid, started_at)
            if _worker_alive(pid, started_at):
                from hermes_cli.kanban_db_worker_termination import sigkill
                killed = sigkill(kill, pid)

        error = f"elapsed {int(elapsed)}s > limit {limit}s"
        with _kb.write_txn(conn):
            # Again under the write lock, after the kill wait: an interpreter that bound
            # meanwhile is invisible to the pid/claim CAS below. Host-local by the filter above.
            if owner_reclaim_blocked_after_kill(conn, tid, signalled=True):
                continue
            retry_status = _kb._retry_status_for_run(conn, tid)
            # [daveey] Fence the captured attempt after the kill wait. A new run
            # can reuse the dispatcher lock (and even PID), but not its run id.
            current = conn.execute(
                "SELECT 1 FROM tasks WHERE id=? AND status='running' AND worker_pid=? "
                "AND claim_lock IS ? AND current_run_id IS ?",
                (tid, pid, row["claim_lock"], row["current_run_id"]),
            ).fetchone()
            if current is None:
                continue
            from hermes_cli.kanban_failure_release import queue
            if queue(conn, tid, row["current_run_id"], outcome="timed_out", error=error,
                     failure_limit=DEFAULT_FAILURE_LIMIT,
                     extra={"pid": pid, "sigkill": killed, "retry_status": retry_status}):
                timed_out.append(tid)
                continue
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND worker_pid = ? AND claim_lock IS ?",
                (retry_status, tid, pid, row["claim_lock"]),
            )
            if cur.rowcount == 1:
                payload = {
                    "pid": pid,
                    "elapsed_seconds": int(elapsed),
                    "limit_seconds": limit,
                    "sigkill": killed,
                    "retry_status": retry_status,
                }
                run_id = _kb._end_run(
                    conn, tid, outcome="timed_out", status="timed_out",
                    error=error, metadata=payload,
                )
                payload["failure_notice"] = "canonical"
                _kb._append_event(conn, tid, "timed_out", payload, run_id=run_id)
                _record_task_failure(
                    conn, tid, error=error, run_id=run_id, outcome="timed_out", allow_nested=True,
                    event_payload_extra={"pid": pid, "sigkill": killed, "retry_status": retry_status},
                )
                timed_out.append(tid)
    return timed_out


# A running task with no heartbeat for this long is inactive regardless of
# ``dispatch_stale_timeout_seconds`` (spec: ">4h started + no commits in 1h").
_STALE_HEARTBEAT_GAP_SECONDS = 3600


def detect_stale_running(
    conn: sqlite3.Connection,
    *,
    stale_timeout_seconds: int = 0,
    signal_fn=None,
) -> list[str]:
    """Reclaim ``running`` tasks with no heartbeat progress; returns their ids.

    Stale = running longer than ``stale_timeout_seconds`` (active run's
    ``started_at``, else ``tasks.started_at``) AND ``last_heartbeat_at`` NULL or
    older than ``_STALE_HEARTBEAT_GAP_SECONDS``. Task returns to its source
    phase, run closes ``outcome='stale'``, a live host-local worker is killed.
    ``0`` disables the check; ``signal_fn`` is a test hook. Deliberately NOT
    counted via ``_record_task_failure``: an absent heartbeat is not a worker
    failure, and counting it would let long-running tasks trip the breaker.
    """
    if stale_timeout_seconds <= 0:
        return []

    now = int(time.time())
    reclaimed: list[str] = []

    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.worker_started_at, t.last_heartbeat_at, t.claim_lock, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running'" + _kb._sync.held_elsewhere_sql(conn, "t")
    ).fetchall()

    from hermes_cli.kanban_owner_recovery import owner_reclaim_blocked, owner_reclaim_blocked_after_kill
    for row in rows:
        if owner_reclaim_blocked(conn, row["id"]):
            continue
        if row["active_started_at"] is None:
            continue
        # [daveey] Awake time only (``kanban_suspend_ledger``): a host suspend is neither runtime
        # nor heartbeat silence, else the first pass after a long lid-close kills every live worker.
        active_started = _kb._to_epoch(row["active_started_at"]) or now  # unreadable clock: never reclaim
        elapsed = _awake_seconds(conn, active_started, now)
        if elapsed < stale_timeout_seconds:
            continue

        last_hb = _kb._to_epoch(row["last_heartbeat_at"])
        hb_age = _awake_seconds(conn, max(last_hb, active_started), now) if last_hb is not None else None
        if hb_age is not None and hb_age < _STALE_HEARTBEAT_GAP_SECONDS:
            continue

        pid = row["worker_pid"]
        tid = row["id"]
        lock = row["claim_lock"] or ""

        termination = _kb._terminate_reclaimed_worker(
            pid, lock, signal_fn=signal_fn, started_at=_kb._row_get(row, "worker_started_at"))

        # Never release a claim while our own worker is still alive: that would
        # spawn a duplicate beside it. Hold the claim and retry next tick.
        if _worker_survived_termination(termination):
            _defer_reclaim_for_live_worker(
                conn, tid, lock, now, termination,
                reason="heartbeat_stale_worker_alive",
            )
            continue

        with _kb.write_txn(conn):
            # Again under the write lock, after the termination wait (see enforce_max_runtime).
            if owner_reclaim_blocked_after_kill(conn, tid, signalled=bool(termination.get("termination_attempted"))):
                continue
            retry_status = _kb._retry_status_for_run(conn, tid)
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND claim_lock IS ?",
                (retry_status, tid, row["claim_lock"]),
            )
            if cur.rowcount != 1:
                continue

            payload = {
                "elapsed_seconds": int(elapsed),
                "last_heartbeat_at": _kb._opt_int(last_hb),
                "heartbeat_age_seconds": _kb._opt_int(hb_age),
                "timeout_seconds": stale_timeout_seconds,
                "pid": int(pid) if pid else None,
                "retry_status": retry_status,
            }
            payload.update(termination)

            run_id = _kb._end_run(
                conn, tid,
                outcome="stale", status="stale",
                error=(
                    f"no heartbeat for {int(hb_age)}s "
                    if hb_age is not None
                    else "no heartbeat ever"
                ) + f" after {int(elapsed)}s running",
                metadata=payload,
            )
            _kb._append_event(conn, tid, "stale", payload, run_id=run_id)
            reclaimed.append(tid)

    return reclaimed


from hermes_cli.kanban_db_dispatch_claims import (  # moved out; re-exported [daveey]
    ORPHAN_CLAIM_GRACE_SECONDS, _claim_in_flight, release_leftover_local_claims,
)


def reconcile_orphaned_running(conn: sqlite3.Connection) -> list[str]:
    """Requeue ``running`` cards with broken claim bookkeeping; returns their ids.

    A task ``running`` with NULL ``claim_lock``/``claim_expires`` (crash
    mid-claim, manual SQL, DB restore) is a zombie forever: ``release_stale_claims``
    needs ``claim_expires``, ``detect_crashed_workers`` needs a host-local lock +
    pid, ``detect_stale_running`` is off by default. Orphans go back to their
    claimed source phase (``review`` for a reviewer run, else ``ready``) with a
    comment, leaked run closed, ``reconciled`` event; a row with a live
    host-local PID, or with a claim still in flight (:func:`_claim_in_flight`),
    is deferred so no duplicate spawns beside it.
    """
    now = int(time.time())
    reconciled: list[str] = []
    rows = conn.execute(
        "SELECT id, claim_lock, claim_expires, worker_pid, worker_started_at FROM tasks "
        "WHERE status = 'running' "
        "  AND (claim_lock IS NULL OR claim_expires IS NULL)" + _kb._sync.held_elsewhere_sql(conn)
    ).fetchall()
    from hermes_cli.kanban_owner_recovery import owner_reclaim_blocked
    for row in rows:
        if owner_reclaim_blocked(conn, row["id"]):
            continue
        tid = row["id"]
        pid = row["worker_pid"]
        if pid and _worker_alive(pid, _kb._row_get(row, "worker_started_at")):
            # Never requeue beside a live process. Retry next tick.
            _kb._log.debug(
                "kanban reconcile: task %s has broken claim bookkeeping but "
                "pid %s is alive on this host — deferring", tid, pid,
            )
            continue
        if _claim_in_flight(conn, tid, now):
            _kb._log.debug("kanban reconcile: task %s has a claim in flight — deferring", tid)
            continue
        with _kb.write_txn(conn):
            # Restore the claimed source phase, as every other release path does:
            # a reviewer run must go back to review, not to an implementation run.
            retry_status = _kb._retry_status_for_run(conn, tid)
            if owner_reclaim_blocked(conn, tid):
                continue
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND claim_lock IS ? AND claim_expires IS ?",
                (retry_status, tid, row["claim_lock"], row["claim_expires"]),
            )
            if cur.rowcount != 1:
                continue
            payload = {
                "reason": "orphaned_running",
                "claim_lock": row["claim_lock"],
                "claim_expires": _kb._to_epoch(row["claim_expires"]),
                "worker_pid": int(pid) if pid else None,
                "now": now,
                "retry_status": retry_status,
            }
            run_id = _kb._end_run(
                conn, tid,
                outcome="reclaimed", status="reclaimed",
                error="orphaned running card (broken claim bookkeeping)",
                metadata=payload,
            )
            _kb._insert_comment(
                conn, tid, "dispatcher",
                "reconciliation: card was 'running' with no valid claim "
                f"(dead/gone worker) — requeued to {retry_status}",
                now,
            )
            _kb._append_event(conn, tid, "reconciled", payload, run_id=run_id)
            reconciled.append(tid)
        _kb._log.info(
            "kanban reconcile: requeued orphaned running task %s "
            "(claim_lock=%r, worker_pid=%r)", tid, row["claim_lock"], pid,
        )
    return reconciled


# A run younger than this may still be settling (a claim and its run row land in one txn, but a synced board's
# feed can briefly carry a run ahead of its card's row), so the sweep leaves it alone.
DANGLING_RUN_MIN_AGE_SECONDS = 600
_DANGLING_RUN_TERMINAL_EVENTS = {
    "completed": ("done", "completed"), "blocked": ("blocked", "blocked"), "crashed": ("crashed", "crashed"),
}


def close_dangling_runs(conn: sqlite3.Connection, *, min_age_seconds: int = DANGLING_RUN_MIN_AGE_SECONDS) -> list[int]:
    """Close runs still ``running`` that are not their card's current run; returns their ids. [daveey]

    Every claim path ends its run through ``current_run_id``. Once that pointer has moved on (cleared by a
    terminal transition, or pointing at a newer run) nothing can reach the old row again, so a run that a path
    abandoned, or that a sync race reopened after it ended, stays ``running`` forever and reads as live work.
    The row is closed ``reclaimed``; on a synced board the capture trigger sends that to the Board."""
    cutoff = int(time.time()) - min_age_seconds
    closed: list[int] = []
    with _kb.write_txn(conn):
        rows = conn.execute(
            "SELECT r.id, r.task_id FROM task_runs r JOIN tasks t ON t.id = r.task_id "
            "WHERE r.status = 'running' AND r.ended_at IS NULL AND r.started_at < ? "
            "  AND (t.current_run_id IS NULL OR t.current_run_id != r.id)", (cutoff,),
        ).fetchall()
        now = int(time.time())
        for row in rows:
            # A run the card already ended (a sync race or a recovery reopened its row) keeps that real end: the
            # terminal event is the proof, and rewriting it to ``reclaimed`` hides the completion from the owner.
            ended = conn.execute(
                "SELECT kind, created_at, payload FROM task_events WHERE run_id = ? AND kind IN "
                "('completed', 'blocked', 'crashed') ORDER BY id DESC LIMIT 1", (row["id"],),
            ).fetchone()
            if ended is not None:
                status, outcome = _DANGLING_RUN_TERMINAL_EVENTS[ended["kind"]]
                try:
                    summary = (json.loads(ended["payload"] or "{}") or {}).get("summary")
                except (ValueError, AttributeError):
                    summary = None
                cur = conn.execute(
                    "UPDATE task_runs SET status = ?, outcome = ?, ended_at = ?, claim_expires = NULL, "
                    "summary = COALESCE(summary, ?) WHERE id = ? AND ended_at IS NULL",
                    (status, outcome, int(ended["created_at"]), summary if isinstance(summary, str) else None, row["id"]),
                )
                if cur.rowcount == 1:
                    closed.append(int(row["id"]))
                    _kb._log.info("kanban reconcile: restored run %s of task %s to %s from its %s event",
                                  row["id"], row["task_id"], outcome, ended["kind"])
                continue
            cur = conn.execute(
                "UPDATE task_runs SET status = 'reclaimed', outcome = 'reclaimed', ended_at = ?, "
                "claim_expires = NULL, error = COALESCE(error, ?) WHERE id = ? AND ended_at IS NULL",
                (now, "dangling run: no longer the card's current run", row["id"]),
            )
            if cur.rowcount == 1:
                closed.append(int(row["id"]))
                _kb._log.info("kanban reconcile: closed dangling run %s of task %s", row["id"], row["task_id"])
    return closed


from hermes_cli.kanban_db_values import error_fingerprint as _error_fingerprint  # re-export (crash accounting)


# ~96% of "clean exit without a terminal tool call" tasks complete on a later
# run, so a protocol violation gets a bounded retry before the breaker trips.
# The budget is a violation-only STREAK (``_protocol_violation_streak``),
# independent of ``consecutive_failures``: other failure kinds neither consume
# nor extend it. Per-task ``max_retries`` overrides it.
_PROTOCOL_VIOLATION_FAILURE_LIMIT = 3

# Closed runs to walk when counting the streak; it trips at a handful anyway.
_PROTOCOL_VIOLATION_SCAN_LIMIT = 50


def _protocol_violation_streak(conn: sqlite3.Connection, task_id: str) -> int:
    """Count the task's trailing run of clean-exit protocol violations.

    Walks closed runs newest-first (including the one ``detect_crashed_workers``
    just closed). ``rate_limited`` runs are neutral and skipped (a quota wall
    says nothing about the task); any other closed run breaks the streak, so
    the budget counts ONLY protocol violations. Violations are recognized by the
    ``protocol_violation`` run-metadata marker, with the error text as fallback
    for runs recorded before the marker existed.
    """
    streak = 0
    rows = conn.execute(
        "SELECT outcome, error, metadata FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY id DESC LIMIT ?",
        (task_id, _PROTOCOL_VIOLATION_SCAN_LIMIT),
    ).fetchall()
    for row in rows:
        outcome = row["outcome"] or ""
        if outcome == "rate_limited" or _is_owner_restart_run(row):
            continue
        head = (row["error"] or "").partition(WORKER_OUTPUT_SEP)[0]  # never the worker's own words
        if outcome == "crashed" and (
                _kb._json_dict(row["metadata"]).get("protocol_violation") or "protocol violation" in head):
            streak += 1
            continue
        break
    return streak


WORKER_OUTPUT_SEP = " Worker's last output: "  # dead-worker error text: our words, then the worker's
_PROTOCOL_VIOLATION_ERROR = (
    # Worker subprocess returned 0 but its task is still ``running`` in the DB — it exited without calling
    # ``kanban_complete`` / ``kanban_block`` / ``kanban_request_review``. Overwhelmingly the work itself succeeded and only the
    # paperwork was skipped, so a retry usually completes; the corrective sentence below is surfaced to the
    # retry worker via the prior-attempt error in ``build_worker_context`` (guidance approach from #61817).
    # Keep this short: ``_record_task_failure`` caps the stored error at 500 chars and the worker's own
    # last output (``_worker_final_output``, up to 400 chars) is appended after it — a longer preamble
    # truncates away the worker's explanation, which is the part the board and the retry worker need.
    "worker exited cleanly (rc=0) without kanban_complete, kanban_block "
    "or kanban_request_review — protocol violation. "
    "If the prior run already did the work, verify it and "
    "report it via kanban_complete (or kanban_request_review); "
    "a run without a terminal kanban call counts as failed no "
    "matter what it did."
)


# Rich panel/rule chrome around the rendered response, and the CLI's own preamble lines.
_LOG_CHROME = re.compile(r"[─━═╭╮╰╯│┃┌┐└┘]+|☤\s*Hermes")


from hermes_cli.kanban_worker_log import (
    current_run_log, exit_summary_marker as _exit_summary_marker, log_noise_prefixes as _log_noise_prefixes,
    _worker_final_output, crash_worker_output)


def _classify_dead_worker(
    pid: int, claimer: Optional[str], exit_code=None, *, task_id: Optional[str] = None,
    board: Optional[str] = None, run_id: Optional[int] = None, conn: Optional[sqlite3.Connection] = None,
) -> _DeadWorker:
    """Map a dead worker's reaped exit status to its reclaim bookkeeping.

    A clean exit or a crash carries the worker's own last output (``worker_output``
    in the event payload, appended to the error text) so the board and the retry
    worker see WHY instead of a bare label; a rate-limited requeue does not need it.
    """
    log = (_kb.read_worker_log(task_id, tail_bytes=None, board=board) or "") if task_id and run_id else None
    exited = _classify_dead_worker_exit(pid, claimer, exit_code, task_id=task_id, board=board, run_id=run_id, log=log)
    failure = _worker_scope.read_run_failure(conn, task_id, run_id, log or "", pid=pid,
                                         exit_code=0 if exited.kind == "clean_exit" else None) if task_id else None  # rc 0 skips the kernel log
    if failure:
        kind = "oom_killed" if failure["scope_result"] == "oom-kill" else "signaled"
        if kind == "oom_killed" and task_id:
            # On the reclaim connection. A second connect here re-runs the
            # legacy-inflight backfill and deadlocks on this write txn.
            _worker_scope.note_oom_kill(task_id, board, conn=conn)
        dead = _DeadWorker(kind, None, _worker_scope.describe(failure), kind,
                           {"pid": pid, "claimer": claimer, **failure})
    else:
        dead = _ou.noted(exited, conn, task_id, run_id, log)
    if task_id and not dead.rate_limited:
        worker_output = crash_worker_output(conn, task_id, run_id, pid, claimer, board=board)
        if worker_output:
            dead.error_text += f"{WORKER_OUTPUT_SEP}{worker_output!r}"
            dead.event_payload["worker_output"] = worker_output
            dead.unknown_skill = dead.kind == "nonzero_exit" and "Unknown skill(s):" in worker_output
    if task_id and dead.kind == "nonzero_exit":  # name a workspace <stdlib>.py [daveey]
        from hermes_cli.kanban_workspace_shadow import note_dead_worker
        note_dead_worker(dead, conn, task_id, run_id, board)
    return dead


def _classify_dead_worker_exit(
    pid: int, claimer: Optional[str], exit_code=None, *, task_id: Optional[str] = None,
    board: Optional[str] = None, run_id: Optional[int] = None, log: Optional[str] = None,
) -> _DeadWorker:
    """Exit status -> reclaim bookkeeping, before the worker's own words are folded in.

    ``exit_code`` is the authority's exact-run result for a managed worker (a child of the
    profile owner, never of this dispatcher, so it cannot be reaped here). Otherwise the reap
    registry only knows children of THIS process; a per-tick dispatcher reads the exit trailer
    the worker left in its log instead, so the same death gets the same booking (protocol
    violation / rate-limit requeue / crash) as under the gateway-embedded dispatcher. A worker
    that never reached its exit epilogue (killed, OOM) leaves no trailer and stays a plain crash. ``log``: as held by the caller.
    """
    kind, code = _classify_worker_exit(pid)
    if exit_code is not None:
        kind, code = _exit_code_kind(int(exit_code))
    elif kind == "unknown" and task_id:
        logged = _worker_log_exit_code(task_id, board=board, run_id=run_id, log=log)
        if logged is not None:
            kind, code = _exit_code_kind(logged)
    if kind == "clean_exit":
        # rc=0 while still ``running``: usually the work succeeded and only the
        # paperwork was skipped; the corrective sentence reaches the retry
        # worker via ``build_worker_context``.
        return _DeadWorker(
            kind, code, _PROTOCOL_VIOLATION_ERROR, "protocol_violation",
            # ``protocol_violation`` is the durable marker for
            # _protocol_violation_streak: _end_run copies this payload into the
            # run metadata.
            {"pid": pid, "claimer": claimer, "exit_code": code, "protocol_violation": True},
            protocol_violation=True,
        )
    if kind == "rate_limited":
        # Quota wall — NOT a task failure. Release to the source phase and do
        # NOT count a failure so a long quota window can't trip the breaker.
        return _DeadWorker(
            kind, code,
            f"pid {pid} exited rate-limited (quota wall) — requeued without counting a failure",
            "rate_limited",
            {"pid": pid, "claimer": claimer, "exit_code": code},
            rate_limited=True,
        )
    if kind == "owner_restart":
        # The profile owner restarted under the attempt (``KANBAN_OWNER_RESTART_EXIT_CODE``): the
        # host's event, not the card's, so it never charges the breaker, however many die at once. [daveey]
        return _DeadWorker(
            kind, code,
            f"pid {pid} lost its turn to a gateway restart — requeued without counting a failure",
            "owner_restart_lost",
            {"pid": pid, "claimer": claimer, "exit_code": code, "owner_restart": True},
            owner_restart=True,
        )
    if kind == "terminal_provider":
        # The worker classified its own provider failure as unhealable (credential
        # revoked, model gone): every further spawn would hit the same wall, so
        # ``_account_crashes`` trips the breaker now instead of after ``failure_limit``.
        return _DeadWorker(
            kind, code,
            f"pid {pid} exited on a terminal provider error (exit {code}): the provider rejected "
            "this profile's credential or model — fix the configuration, then unblock.",
            "crashed",
            {"pid": pid, "claimer": claimer, "exit_kind": kind, "exit_code": code, "terminal_provider": True},
            terminal_provider=True,
        )
    if kind in ("nonzero_exit", "owner_unavailable", "owner_down"):  # owner_*: kanban_owner_unavailable.note rebooks it
        error_text = f"pid {pid} exited with code {code}"
    elif kind == "signaled":
        error_text = f"pid {pid} killed by signal {code}"
    else:
        error_text = f"pid {pid} not alive"
    event_payload = {"pid": pid, "claimer": claimer}
    if code is not None and kind != "unknown":
        event_payload["exit_kind"] = kind
        event_payload["exit_code"] = code
    return _DeadWorker(kind, code, error_text, "crashed", event_payload)


@dataclass
class _CrashSweep:
    """Everything ``detect_crashed_workers`` collects inside its reclaim txn."""

    crashed: list[str] = field(default_factory=list)
    rate_limited: list[str] = field(default_factory=list)
    host_rebooted: list[str] = field(default_factory=list)  # reboot bystanders, requeued uncounted [daveey]
    owner_unavailable: list[str] = field(default_factory=list)  # owner never answered, requeued uncounted [daveey]
    owner_restarted: list[str] = field(default_factory=list)  # lost to an owner restart, requeued uncounted [daveey]
    # ``(task_id, pid, claimer, dead_worker)``: accounted in the reclaim txn.
    crash_details: list[tuple[str, int, str, _DeadWorker]] = field(default_factory=list)
    auto_blocked: list[str] = field(default_factory=list)
    # Worker-exit observer payloads, fired only after every reclaim/accounting
    # txn has committed.
    exited_hook_payloads: list[dict] = field(default_factory=list)


def _reclaim_dead_workers(conn: sqlite3.Connection, board: Optional[str] = None,
                          *, failed_results_only: bool = False) -> _CrashSweep:
    """Release every host-local ``running`` task whose worker PID is dead."""
    sweep = _CrashSweep()
    from hermes_cli.kanban_update_reclaim_intent import recover_and_drain
    sweep.exited_hook_payloads.extend(recover_and_drain(conn))
    for hook in sweep.exited_hook_payloads:
        target = {"rate_limited": sweep.rate_limited, "reclaimed": sweep.owner_restarted, "host_reboot": sweep.host_rebooted,
                  "owner_down": sweep.owner_unavailable, "owner_unavailable": sweep.owner_unavailable}
        target[hook["outcome"]].append(hook["task_id"])
    with _kb.write_txn(conn):
        rows = conn.execute(
            "SELECT id, worker_pid, worker_started_at, claim_lock, started_at, assignee, current_run_id, "
            "workspace_path FROM tasks "
            "WHERE status = 'running' AND worker_pid IS NOT NULL"
        ).fetchall()
        host_prefix = _kb._host_prefix()
        now_boot, reboot_victims = _host_reboot.victims(rows, lambda r: _kb._claim_is_local(r["claim_lock"] or "",
                                                                                          host_prefix))
        from hermes_cli.kanban_owner_recovery import apply_attempt_note, owner_reclaim_blocked
        for row in rows:
            lock = row["claim_lock"] or ""
            if not _kb._claim_is_local(lock, host_prefix):
                continue
            from hermes_cli.kanban_update_reclaim_intent import observer_held
            if observer_held(conn, row):
                continue
            # Launch-window grace so a freshly-spawned worker isn't reclaimed
            # before its PID is visible on /proc.
            started_at = _kb._to_epoch(_kb._row_get(row, "started_at"))
            if not failed_results_only and started_at is not None and time.time() - started_at < _kb._resolve_crash_grace_seconds():
                continue
            if _worker_alive(row["worker_pid"], _kb._row_get(row, "worker_started_at")):
                continue
            from hermes_cli.kanban_failed_result import reclaim_proof, result_exit_code
            proof, hold = reclaim_proof(conn, row, failed_results_only=failed_results_only)
            if hold:
                continue
            if proof is None and owner_reclaim_blocked(conn, row["id"], crash_sweep=True):
                continue

            pid = int(row["worker_pid"])
            # A managed run's authority-recorded exit (its bound interpreter's, under this claim).
            exit_code = result_exit_code(conn, row, proof)
            dead = _classify_dead_worker(pid, row["claim_lock"], exit_code, task_id=row["id"], board=board,
                                         run_id=row["current_run_id"], conn=conn)
            _host_reboot.note(dead, row, now_boot, reboot_victims)  # [daveey]
            if proof is None:
                apply_attempt_note(conn, row["id"], pid, dead)
            else:
                from hermes_cli.kanban_lost_result import annotate_loss
                annotate_loss(dead, proof, int(time.time()), failed_results_only)
            _owner_death.note(dead, conn, row["id"], row["current_run_id"])  # owner died under it [daveey]
            _guard_refusal.note(dead, conn, row["id"], row["current_run_id"])  # host refused its kanban calls [daveey]
            dead.run_id = row["current_run_id"]
            dead.event_payload["retry_status"] = _kb._retry_status_for_run(conn, row["id"])
            from hermes_cli.kanban_failure_release import route_dead_worker
            if route_dead_worker(conn, row, dead, sweep, pid):
                continue
            from hermes_cli.kanban_run_finalizer import close_dead_run
            closed = close_dead_run(conn, row, dead)
            if closed is None:
                continue
            run_id, retry_status = closed
            sweep.exited_hook_payloads.append({
                "task_id": row["id"],
                "assignee": row["assignee"],
                "run_id": run_id,
                "worker_pid": pid,
                "exit_kind": dead.kind,
                "exit_code": dead.code,
                "outcome": dead.run_outcome,
                "retry_status": retry_status,
            })
            if dead.rate_limited or dead.protocol_violation:
                # Stamp last_failure_error WITHOUT touching ``consecutive_failures``:
                # a rate-limited requeue must show ``check_respawn_guard`` a quota
                # blocker; a below-budget protocol violation never reaches
                # ``_record_task_failure`` (which stamps this column), yet the
                # board UI and retry worker need the corrective message.
                conn.execute(
                    "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                    (dead.error_text[:500], row["id"]),
                )
            if (dead.host_reboot and not dead.reboot_culprit) or dead.owner_unavailable:
                # A reboot bystander, or an owner that never answered (no turn ran): no failure counted. [daveey]
                conn.execute("UPDATE tasks SET last_failure_error = ? WHERE id = ?", (dead.error_text[:500], row["id"]))
                (sweep.owner_unavailable if dead.owner_unavailable else sweep.host_rebooted).append(row["id"])
            elif dead.owner_restart:  # lost to its owner's restart: requeued, no failure counted [daveey]
                conn.execute("UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                             (dead.error_text[:500], row["id"]))
                sweep.owner_restarted.append(row["id"])
            elif dead.rate_limited:
                try:
                    from hermes_cli.kanban_quota_episode import record_episode
                    record_episode(conn, row["id"], run_id)
                except sqlite3.Error:
                    raise
                except Exception:  # health: allow BLE001 -- episode bookkeeping never strands the sweep; traceback logged [daveey]
                    _kb._log.warning("kanban: recording quota episode failed for %s", row["id"], exc_info=True)
                sweep.rate_limited.append(row["id"])
                try:
                    from hermes_cli import kanban_provider_walls as _walls
                    _walls.record_wall_for_run(conn, row["id"], row["current_run_id"],
                                               _kb.worker_log_path(row["id"], board=board))
                except Exception:
                    _kb._log.warning("kanban: recording provider wall failed", exc_info=True)
            else:
                sweep.crashed.append(row["id"])
                sweep.crash_details.append((row["id"], pid, row["claim_lock"], dead))
        if sweep.crash_details:
            sweep.auto_blocked = _account_crashes(conn, sweep.crash_details, allow_nested=True)
    return sweep


def detect_crashed_workers(conn: sqlite3.Connection, board: Optional[str] = None,
                           *, failed_results_only: bool = False) -> list[str]:
    """Reclaim ``running`` tasks whose worker PID is no longer alive.

    Restores the source phase immediately (no waiting for the claim TTL), for
    tasks claimed by *this host* only — other hosts' PIDs are meaningless.
    Clean exit while ``running`` is a protocol violation with a bounded
    violation-only retry budget; ``KANBAN_RATE_LIMIT_EXIT_CODE`` is a quota
    wall, released WITHOUT counting a failure and surfaced via the
    ``_last_rate_limited`` attribute (the return stays crashed-only).
    """
    sweep = _reclaim_dead_workers(conn, board=board, failed_results_only=failed_results_only)
    # Closure and accounting committed together; only external observers remain.
    for _, _, _, dead in sweep.crash_details:
        _worker_scope.release_failure(dead.event_payload)
    # Side-channel attributes keep the public ``list[str]`` return stable;
    # ``dispatch_once`` reads them to populate ``DispatchResult``. Rate-limited
    # requeues did NOT count a failure and are NOT crashes.
    detect_crashed_workers._last_auto_blocked = sweep.auto_blocked  # type: ignore[attr-defined]
    detect_crashed_workers._last_rate_limited = sweep.rate_limited  # type: ignore[attr-defined]
    detect_crashed_workers._last_host_rebooted = sweep.host_rebooted  # type: ignore[attr-defined]
    detect_crashed_workers._last_owner_restarted = sweep.owner_restarted  # type: ignore[attr-defined]
    # Fired only now, after the reclaim txn AND breaker accounting have
    # committed, so subscribers always observe fully durable board state.
    if sweep.exited_hook_payloads and _kb._kanban_observer_consumed("on_kanban_worker_exited"):
        _board = _kb.get_current_board()
        for hook_fields in sweep.exited_hook_payloads:
            hook_fields = dict(hook_fields)
            _kb._fire_kanban_lifecycle_hook(
                # Best-effort observer in the dispatcher, after durable accounting.
                "on_kanban_worker_exited",
                hook_fields.pop("task_id"),
                board=_board,
                **hook_fields,
            )
    return sweep.crashed


def _set_worker_pid(conn: sqlite3.Connection, task_id: str, pid: int, *, run_id=None, claim_lock=None) -> None:
    """Publish the launcher only while this claim has no executing worker yet, together with its
    restart-stable fingerprint (``_process_fingerprint``); the ``spawned`` event carries both. The
    fingerprint is what lets every later liveness/kill decision tell OUR worker from a process that
    recycled the PID after a reboot. A failed capture is persisted as ``UNVERIFIED_WORKER_FINGERPRINT``,
    never NULL: NULL is the legacy pre-fingerprint row whose bare-PID kill authority a new spawn must
    not inherit.

    [daveey] ``run_id`` is the claimed run (HERMES_KANBAN_RUN_ID): its run row always records the
    worker it launched, and a ``current_run_id`` a sync pull blanked mid-spawn (t_01a189343dd2f6ac)
    still counts as this claim's for the task row. A card that moved on to a newer run keeps that
    run's task fields untouched."""
    started_at = _process_fingerprint(int(pid)) or UNVERIFIED_WORKER_FINGERPRINT
    with _kb.write_txn(conn):
        if run_id is None:
            run_id = _kb._current_run_id(conn, task_id)
        conn.execute(
            "UPDATE tasks SET worker_pid = ?, worker_started_at = ? WHERE id = ? AND status = 'running' "
            "AND (current_run_id IS ? OR current_run_id IS NULL) AND worker_pid IS NULL "
            "AND (? IS NULL OR claim_lock = ?)",
            (int(pid), started_at, task_id, run_id, claim_lock, claim_lock))
        if run_id is not None:
            conn.execute("UPDATE task_runs SET worker_pid = ?, worker_started_at = ? WHERE id = ? "
                         "AND worker_pid IS NULL",
                         (int(pid), started_at, run_id))
        _kb._append_event(conn, task_id, "spawned", {"pid": int(pid), "started_at": started_at}, run_id=run_id)


def adopt_worker_pid(conn: sqlite3.Connection, task_id: str, run_id: int, pid: int) -> bool:
    """Worker-side half of ``_set_worker_pid``, run by the worker before its first model call.

    A dispatcher killed between spawning the worker and ``_set_worker_pid`` leaves the run with no
    pid: no liveness check can see the worker, so a TTL expiry reclaims the card and spawns a second
    worker beside it. The worker fills the missing pid itself (``worker_registered``). False when
    ``run_id`` is no longer the card's live run: the card was reclaimed before this worker got here,
    and it must exit without working it."""
    started_at = _process_fingerprint(int(pid)) or UNVERIFIED_WORKER_FINGERPRINT
    with _kb.write_txn(conn):
        row = conn.execute("SELECT status, current_run_id, worker_pid, claim_lock FROM tasks WHERE id = ?",
                           (task_id,)).fetchone()
        if row is None or row["status"] != "running" or row["current_run_id"] != int(run_id):
            return False
        # Liveness checks are host-local: a pid from another host (or pid namespace) proves nothing here.
        if row["worker_pid"] is None and _kb._claim_is_local(row["claim_lock"]):
            conn.execute("UPDATE tasks SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
                         (int(pid), started_at, task_id))
            conn.execute("UPDATE task_runs SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
                         (int(pid), started_at, int(run_id)))
            _kb._append_event(conn, task_id, "worker_registered", {"pid": int(pid), "started_at": started_at},
                              run_id=int(run_id))
    return True


def _clear_failure_counter(conn: sqlite3.Connection, task_id: str) -> None:
    """Reset the unified consecutive-failures counter.

    Called from ``complete_task`` on success. NOT called on spawn success: a
    spawn proves the worker could start, not that the run will succeed, so
    timeouts and crashes must accumulate across spawn boundaries.
    """
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 0, "
            "last_failure_error = NULL WHERE id = ?",
            (task_id,),
        )


def _block_if_guard_hold_capped(conn: sqlite3.Connection, task_id: str, reason: str) -> bool:
    from hermes_cli.kanban_block_routing import block_if_guard_hold_capped
    return block_if_guard_hold_capped(conn, task_id, reason)


def check_respawn_guard(
    conn: sqlite3.Connection, task_id: str, *, lane: str = "ready",
) -> Optional[str]:
    """Return a guard reason if ``task_id`` should NOT be re-spawned, else None.

    Called per ready/review row before any claim attempt. Priority order:
    ``"infrastructure_cooldown"`` (latest run is a ``spawn_failed`` the host
    refused — no restart-safe scope — within the cooldown; never counted),
    ``"rate_limit_cooldown"`` (latest run ``rate_limited`` within the cooldown;
    checked BEFORE ``blocker_auth`` because the requeue stamps a quota-flavored
    ``last_failure_error`` that would otherwise park the task forever — that
    path never increments ``consecutive_failures``), ``"blocker_auth"``
    (quota/auth pattern from a worker/provider failure, never a completion-gate
    refusal; a held card never runs, so the breaker can NOT trip — the
    dispatcher's hold cap, :func:`guard_hold_cap_seconds`, blocks it instead), then for the
    ready lane only ``"recent_success"`` (completed run within the window, unless
    a re-queue event arrived after it — a deliberate re-run) and ``"active_pr"``
    (PR URL in a recent comment; re-spawning risks a duplicate PR — unless a
    handoff event followed the comment: the named profile must work on that
    PR). The review lane skips the last two: they are the *inputs* to a review
    handoff. Stale / dead claim locks are NOT a guard reason — the reclaim
    passes own those.
    """
    row = conn.execute(
        "SELECT last_failure_error FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None

    from hermes_cli.kanban_quota_release import pending_task
    if pending_task(conn, task_id):
        return "rate_limit_cooldown"
    now = int(time.time())
    from hermes_cli.kanban_quota_episode import decision, episode
    if episode(conn, task_id):
        guard = decision(conn, _kb.get_task(conn, task_id), lane=lane, now=now)[0]
        stamp = _kb._lossy_text(row['last_failure_error'])
        if guard is None and _kbq.is_quota_error(stamp):
            _kbq.clear_quota_error(conn, task_id, stamp)
        return guard
    # Only the latest run gates admission; quota and infrastructure failures are neutral.
    # A core-update requeue or an unused Board grant ran nothing and is not that run. [daveey]
    from hermes_cli.kanban_core_update_requeue import latest_ended_run_skipping_requeues
    rl_cooldown = _kb._resolve_rate_limit_cooldown_seconds()
    latest_run = latest_ended_run_skipping_requeues(conn, task_id, by_ended_at=True)
    if latest_run is not None and latest_run["outcome"] == "spawn_failed":
        if rl_cooldown > 0 and _kb._json_dict(latest_run["metadata"]).get("infrastructure"):
            ended_at = _kb._to_epoch(latest_run["ended_at"])
            if ended_at is not None and (now - ended_at) < rl_cooldown:
                return "infrastructure_cooldown"
    if latest_run is not None and _is_owner_restart_run(latest_run):
        # Lost to its owner's restart: spaced so it does not respawn into the same drain. [daveey]
        ended_at = _kb._to_epoch(latest_run["ended_at"])
        cooldown = _kb._env_int("HERMES_KANBAN_OWNER_RESTART_COOLDOWN_SECONDS", OWNER_RESTART_COOLDOWN_SECONDS)
        if ended_at is not None and (now - ended_at) < cooldown:
            return "owner_restart_cooldown"
        return None

    err = _kb._lossy_text(row["last_failure_error"])
    latest_outcome = latest_run["outcome"] if latest_run is not None else None
    # 1b. Quota stamp that outlived its ``rate_limited`` run (a later ``blocked`` /
    #     ``changes_requested`` run is now the latest): still a quota wall, never
    #     ``blocker_auth``. Same cooldown, measured from the latest run's end; once
    #     it has elapsed the stamp is dropped and the card retries. (2026-10-23:
    #     six cards blocked ``capability`` for 13 h on a cleared quota wall.) [daveey]
    if err and latest_outcome not in {"crashed", "oom_killed", "signaled"} and not _is_completion_gate_error(err) and _kbq.is_quota_error(err):
        ended_at = _kb._to_epoch(latest_run["ended_at"]) if latest_run is not None else None
        if rl_cooldown > 0 and ended_at is not None and (now - ended_at) < rl_cooldown:
            return "rate_limit_cooldown"
        _kbq.clear_quota_error(conn, task_id, err)
        err = ""

    # 2. Auth blocker: retrying immediately will not help.  A plain
    # crash is different: its persisted error includes the worker's last
    # captured output, which is context rather than a diagnosis and may contain
    # benign commands such as ``claude auth status`` (#117097).
    if (err and latest_outcome not in {"crashed", "oom_killed", "signaled"} and not _is_completion_gate_error(err)
            and _RESPAWN_BLOCKER_RE.search(err)):
        return "blocker_auth"

    # 2b. Shared external blocker (``kanban_shared_blockers``): while another
    #     card probes the same declared wall, or the last probe was recent,
    #     hold this one instead of letting every card rediscover it. [daveey]
    from hermes_cli.kanban_shared_blockers import shared_blocker_guard
    try:
        shared = shared_blocker_guard(conn, task_id, now=now)
    except Exception:
        _kb._log.debug("kanban: shared blocker guard failed for %s", task_id, exc_info=True)
        shared = None
    if shared is not None:
        return shared

    # Review-lane spawns stop here: a recent completed run and a fresh PR URL
    # are the canonical *inputs* to a review handoff, not duplicate-work signals.
    if lane == "review":
        return None

    # 3. Completed run within guard window. Exception: an explicit re-queue
    #    AFTER that success (done→ready drag, re-promotion, unblock, reclaim) is
    #    a deliberate "run it again" — otherwise a manual done→ready would sit
    #    silently held until the window elapses. An operator reopen counts only strictly after it. [daveey]
    success = max((t for t, _ in _rows_since(
        conn, "task_runs", task_id, now - _RESPAWN_GUARD_SUCCESS_WINDOW, "AND outcome = 'completed'")), default=None)
    if success is not None and not any(_rows_since(
            conn, "task_events", task_id, success, "AND kind IN ('status', 'promoted', 'unblocked', 'reclaimed')")) \
            and not reopened_after(conn, task_id, success):
        return "recent_success"

    # 4. GitHub PR URL in a recent comment — prior worker already opened a PR
    #    (see :func:`kanban_db_dispatch_pr_state.active_pr_reason` for the exceptions).
    from hermes_cli.kanban_db_dispatch_pr_state import active_pr_reason
    return active_pr_reason(conn, task_id, now)


_GUARD_TIME_COLS = {"task_runs": "ended_at", "task_events": "created_at", "task_comments": "created_at"}


def _rows_since(conn: sqlite3.Connection, table: str, task_id: str, since: int, where: str = "", *,
                cols: str = "", strict: bool = False, unreadable: Optional[int] = None) -> Iterator[tuple[int, Any]]:
    """``(epoch, row)`` for ``task_id``'s ``table`` rows timed at/after ``since`` (after, if ``strict``).
    SQLite sorts TEXT above every INTEGER, so the indexed range also returns every text stamp (written
    outside the mutators, #97455); those are decided on their parsed epoch. An unreadable time is
    dropped (never guards or lifts) unless the caller substitutes ``unreadable``."""
    col = _GUARD_TIME_COLS[table]
    sql = f"SELECT {col}{cols} FROM {table} WHERE task_id = ? {where} AND {col} {'>' if strict else '>='} ?"
    for row in conn.execute(sql, (task_id, since)):
        t = _kb._to_epoch(row[0])
        t = unreadable if t is None else t
        if t is not None and (t > since if strict else t >= since):
            yield t, row


def _profile_exists_fn() -> Optional[Callable[[str], bool]]:
    """``profile_exists`` from ``hermes_cli.profiles``; ``None`` when it cannot be
    imported and no allowlist is set (callers then trust the assignee).

    With ``kanban.dispatch_profiles`` set (#110995) the predicate also requires the
    assignee to be listed, fail-closed: a ``default`` card is only claimable by homes that
    opted in, and a set allowlist whose gate cannot be built claims nothing (#147531)."""
    try:
        from hermes_cli.profiles import normalize_profile_name, profile_exists
    except Exception:
        if _dispatch_profile_allowlist(str) is None:
            return None
        _kb._log.warning("kanban: hermes_cli.profiles unimportable — claiming no cards", exc_info=True)
        return lambda _name: False
    allowlist = _dispatch_profile_allowlist(normalize_profile_name)
    if allowlist is None:
        return profile_exists

    def _gated(name: str) -> bool:
        try:
            canon = normalize_profile_name(name)
        except ValueError:
            return False
        return canon in allowlist and bool(profile_exists(name))

    return _gated


def _has_spawnable(conn: sqlite3.Connection, status: str) -> bool:
    from hermes_cli.kanban_worker_node import dispatch_refusals
    refused = dispatch_refusals(conn, status)
    rows = [row for row in conn.execute(
        "SELECT id, assignee FROM tasks "
        "WHERE status = ? AND assignee IS NOT NULL AND claim_lock IS NULL",
        (status,),
    ).fetchall() if row["id"] not in refused]
    if not rows:
        return False
    profile_exists = _profile_exists_fn()
    if profile_exists is None:
        # Can't introspect — assume spawnable, preserve legacy behavior.
        return True
    return any(profile_exists(row["assignee"]) for row in rows)


def has_spawnable_ready(conn: sqlite3.Connection) -> bool:
    """True iff a ready+assigned+unclaimed task maps to a real Hermes profile.

    Lets health telemetry tell "stuck" (``0 spawned`` with spawnable work) from
    "correctly idle" (only control-plane lanes waiting on ``claim_task``). Falls
    back to "any assigned" when profiles cannot import and no allowlist is set.
    """
    return _has_spawnable(conn, "ready")


def has_spawnable_review(conn: sqlite3.Connection) -> bool:
    """:func:`has_spawnable_ready` for the review column."""
    return _has_spawnable(conn, "review")


def review_dispatch_enabled() -> bool:
    """Whether review tasks dispatch automatically. Default true (Hermes ships
    ``sdlc-review``); operators disable it for human-only review boards.
    """
    try:
        from hermes_cli.config import load_config
        return bool((load_config() or {}).get("kanban", {}).get("review_dispatch", True))
    except Exception:
        return True


# Memory-aware dispatch guard: an uncapped board once OOM'd a 1 GiB host. Two
# safeguards — a memory-DERIVED default cap when none is configured
# (``resolve_max_in_progress``) and a live memory-PRESSURE guard inside the
# tick (``_memory_pressure_level``) because a static cap can't see other
# tenants. Both fail open: non-Linux / read error → no cap / "unknown".

# Assumed per-worker footprint for the derived cap; deliberately conservative
# so the cap errs toward fewer workers on small VMs.
MEMORY_GUARD_MB_PER_WORKER = 512

# Derived default bounds: never below 2 (smallest VM must still progress),
# never above 8 (more fan-out must be explicit in config).
DERIVED_MAX_IN_PROGRESS_FLOOR = 2
DERIVED_MAX_IN_PROGRESS_CEILING = 8


def _system_memory_sample() -> dict:
    """Best-effort system memory snapshot (KiB values), ``{}`` when unknown.

    Local import keeps ``kanban_db`` importable without the gateway package.
    Module-level indirection is also the test seam — conftest patches this to
    ``{}`` so results don't depend on the CI runner's live memory.
    """
    try:
        from gateway.lifecycle_ledger import sample_memory
        return sample_memory() or {}
    except Exception:
        return {}


def derive_default_max_in_progress(sample: Optional[Mapping[str, Any]] = None) -> Optional[int]:
    """Memory-derived default for ``kanban.max_in_progress`` when unset:
    ``clamp(MemTotal / MEMORY_GUARD_MB_PER_WORKER, FLOOR, CEILING)``. Returns
    ``None`` (no cap) when total memory is unknown, so macOS/Windows dev
    machines are unaffected.
    """
    if sample is None:
        sample = _system_memory_sample()
    total_kib = sample.get("mem_total_kib")
    if isinstance(total_kib, bool) or not isinstance(total_kib, int) or total_kib <= 0:
        return None
    workers = (total_kib // 1024) // MEMORY_GUARD_MB_PER_WORKER
    return max(DERIVED_MAX_IN_PROGRESS_FLOOR, min(workers, DERIVED_MAX_IN_PROGRESS_CEILING))


def resolve_max_in_progress(configured: Optional[int]) -> Optional[int]:
    """Effective global concurrency cap: explicit config wins, else the
    memory-derived default. All config-parsing callers route through this so
    both paths agree.
    """
    if configured is not None:
        return configured
    return derive_default_max_in_progress()


def configured_max_in_progress() -> Optional[int]:
    """Read ``kanban.max_in_progress`` from config, or None when unset/invalid.

    Shared so every dispatch entry point agrees on "explicitly configured": a
    positive integer wins, anything else falls through to the derived default.
    """
    try:
        from hermes_cli.config import load_config_readonly
        raw = (load_config_readonly() or {}).get("kanban", {}).get("max_in_progress")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        ival = int(raw)
    except (TypeError, ValueError):
        return None
    return ival if ival >= 1 else None


def count_running_tasks(conn: sqlite3.Connection) -> int:
    """Number of tasks in ``status='running'``.

    Used by the multi-board sweep to count OTHER boards' workers against the
    host-level budget — the memory-derived cap bounds the machine, not the
    board. Fails open to 0 so a broken board doesn't brick dispatch on healthy ones.
    """
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'running'" + _kb._sync.held_elsewhere_sql(conn)
            ).fetchone()[0]
        )
    except Exception:
        return 0


from hermes_cli.kanban_board_share import count_running_tasks_other_boards  # moved; test seam stays here [daveey]


def _memory_pressure_level(sample: Optional[Mapping[str, Any]] = None) -> str:
    """Classify system memory pressure: ok/elevated/critical/unknown.

    Reuses :func:`gateway.memory_status.classify_pressure` so "critical" matches
    the dashboard banner and lifecycle-ledger OOM heuristics. ``unknown``
    (non-Linux, read failure) imposes no restriction — never brick dispatch
    where /proc is unavailable.
    """
    if sample is None:
        sample = _system_memory_sample()
    if not sample:
        return "unknown"
    try:
        from gateway.memory_status import classify_pressure
        return classify_pressure(sample.get("mem_available_kib"), sample.get("mem_total_kib"))
    except Exception:
        return "unknown"


def dispatch_once(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    max_spawn: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    board: Optional[str] = None,
    default_assignee: Optional[str] = None,
    max_in_progress_per_profile: Optional[int] = None,
    reconcile_orphans: bool = True,
    dispatcher_id: Optional[str] = None,
    observation_update_drain: bool = False,
    spawn_quota: Optional[int] = None,
) -> DispatchResult:
    """Run one dispatcher tick under the board's single-writer lock.

    Wraps :func:`_dispatch_once_locked` in the non-blocking :func:`_dispatch_tick_lock`
    so two dispatchers on one ``kanban.db`` never race a write tick on WAL
    frames. The loser returns an empty ``DispatchResult`` with
    ``skipped_locked=True`` and writes nothing; the lock is keyed on the
    resolved DB path so unrelated boards tick in parallel.
    """
    def _locked_tick() -> DispatchResult:
        result = _dispatch_once_locked(
            conn,
            spawn_fn=spawn_fn,
            ttl_seconds=ttl_seconds,
            dry_run=dry_run,
            max_spawn=max_spawn,
            max_in_progress=max_in_progress,
            failure_limit=failure_limit,
            stale_timeout_seconds=stale_timeout_seconds,
            board=board,
            default_assignee=default_assignee,
            max_in_progress_per_profile=max_in_progress_per_profile,
            reconcile_orphans=reconcile_orphans,
            spawn_quota=spawn_quota,
        )
        from hermes_cli.kanban_capacity_evidence import observe_dispatch
        observe_dispatch(conn, result, board=board, dry_run=dry_run,
                         update_drain=observation_update_drain)
        return result

    from hermes_cli.kanban_dispatch_owner import run_owned_tick
    return run_owned_tick(conn, _locked_tick, dispatcher_id, board, dry_run)


from hermes_cli.kanban_db_dispatch_spawn_call import call_spawn_fn as _call_spawn_fn  # moved out [daveey]


def _dispatch_lane_task(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    assignee: str,
    result: DispatchResult,
    *,
    lane: str,
    dry_run: bool,
    ttl_seconds: Optional[int],
    board: Optional[str],
    failure_limit: int,
    spawn_fn,
    per_profile_cap: Optional[int],
    per_profile_running: dict[str, int],
) -> bool:
    """Guard, claim, resolve the workspace and spawn one ready/review row.
    Returns True when a spawn slot was consumed (real or ``dry_run``); every
    skip is recorded on ``result``.
    """
    task_id = row["id"]
    from hermes_cli.kanban_db_dispatch_admission import lane_admission_refused  # nonspawnable, capsule [daveey]
    if lane_admission_refused(conn, task_id, assignee, result, profile_exists=_profile_exists_fn(),
                              spawn_fn=spawn_fn, dry_run=dry_run, lane=lane):
        return False
    # Per-profile cap: a fan-out must not swamp one profile's local model / API quota / browser pool.
    if per_profile_cap is not None:
        current = per_profile_running.get(assignee, 0)
        if current >= per_profile_cap:
            result.skipped_per_profile_capped.append((task_id, assignee, current))
            return False
    from hermes_cli.kanban_model_assignment import dispatch_guard
    guard_reason = dispatch_guard(conn, task_id, lane=lane, dry_run=dry_run)
    if guard_reason is not None:
        result.respawn_guarded.append((task_id, guard_reason))
        _record_clock_hold(conn, result, task_id, guard_reason)
        if not dry_run:  # maintenance is home-owned; foreign executors still record the guard
            if _block_if_guard_hold_capped(conn, task_id, guard_reason):
                result.auto_blocked.append(task_id)
                return False
            _guard_notice.record_guard_hold(conn, task_id, guard_reason)
        return False
    from hermes_cli.kanban_llm_pacing_dispatch import hold as _pacing_hold  # quota-pace governor [daveey]
    if _pacing_hold(conn, row, lane, result, dry_run=dry_run):  # pool ahead of pace: wait, unclaimed
        return False
    from hermes_cli.kanban_start_gate import hold as _start_gate_hold  # plugin start gate (token throttle) [daveey]
    gate_reason = _start_gate_hold(task_id=task_id, lane=lane, assignee=assignee, board=board)
    if gate_reason is not None:  # a plugin holds this start: wait, unclaimed, asked again next tick
        result.respawn_guarded.append((task_id, gate_reason))
        _record_clock_hold(conn, result, task_id, gate_reason)
        return False

    def _count_spawn(name: str) -> None:
        # Later rows in this tick respect the per-profile cap; subsequent ticks re-query from the DB.
        if per_profile_cap is not None and name:
            per_profile_running[name] = per_profile_running.get(name, 0) + 1

    if dry_run:
        result.spawned.append((task_id, assignee, ""))
        _count_spawn(assignee)
        return True
    # Assignee's gateway service failed/masked/disabled: hold unclaimed, file ``kanban_owner_down``'s card. [daveey]
    from hermes_cli.kanban_owner_down import preflight as _owner_down_preflight
    down = _owner_down_preflight(conn, task_id, assignee)
    if down is not None:
        result.owner_down.append((task_id, assignee, down.code))
        return False
    from hermes_cli.kanban_db_dispatch_admission import admitted_claim  # + capsule recheck in the claim txn [daveey]
    _kb._sync.pop_claim_deferral(task_id)
    claimed = admitted_claim(conn, task_id, assignee, lane, ttl_seconds, spawn_fn, result)
    if claimed is None:
        deferral = _kb._sync.pop_claim_deferral(task_id)
        if deferral is not None:  # the lane decides whether this refusal permits later candidates
            result.claim_deferred.append((task_id, deferral))
        return False
    try:  # R4b: a grant snapshot with custody or a manifest routes to the source resolver before any kind branch
        workspace, resolved_branch_name, source = _source_dispatch.resolve(conn, claimed, board=board, kbw=_kbw)
    except Exception as exc:
        if _source_dispatch.refused(conn, claimed, exc, result):
            return False
        # A misconfigured worktree (no repo to anchor it, relative path, board
        # without default_workdir) fails identically on every retry: block on the
        # first occurrence (sticky) with the reason instead of spending the
        # retry budget. [daveey]
        config_error = isinstance(exc, _kbw.WorktreeWorkspaceConfigError)
        if _record_task_failure(
            conn, claimed.id, f"workspace: {exc}",
            outcome="spawn_failed", failure_limit=failure_limit, release_claim=True, end_run=True,
            force_trip=config_error,
            event_payload_extra={"workspace_config_error": True} if config_error else None,
        ):
            result.auto_blocked.append(claimed.id)
        return False
    from hermes_cli.kanban_source_launch import launch  # record, attach (source cards), spawn, pre-spawn abort
    return launch(conn, claimed, workspace, resolved_branch_name, source, lane=lane, board=board, failure_limit=failure_limit,
                  spawn_fn=spawn_fn if spawn_fn is not None else _default_spawn, result=result, count_spawn=_count_spawn)


def _run_reclaim_phase(
    conn: sqlite3.Connection,
    result: DispatchResult,
    *,
    stale_timeout_seconds: int,
    failure_limit: int,
    reconcile_orphans: bool,
    board: Optional[str] = None, dry_run: bool = False,
) -> None:
    """Reclaim stale/orphaned/crashed/timed-out running tasks, then promote. Each sweep is a ``dispatch.<sweep>`` pass."""
    _la.timed("dispatch.reap_worker_zombies", reap_worker_zombies)
    result.reaped_terminal_workers = _la.timed("dispatch.reap_terminal_workers", reap_terminal_workers, conn)
    result.stopped_run_scopes = _la.timed("dispatch.stopped_scope_units", _worker_scope.stopped_scope_units, conn, board=board)  # orphans of ended runs [daveey]
    _la.timed("dispatch.requeue_custody", _requeue_custody.sweep, conn)  # requeued runs' persisted jobs: release or bound
    # Stop requests and owner card archives other gateways sent (kanban_owner_archive_relay), before the reclaim sweeps. [daveey]
    from hermes_cli.kanban_owner_archive_relay import apply_remote_archives
    result.remote_archived = _la.timed("dispatch.apply_remote_archives", apply_remote_archives, conn)
    # [daveey] Before any stale check: a long gap since the last pass is a host suspend, which the
    # heartbeat backstop, the stale sweep and max_runtime must not count (``kanban_suspend_ledger``).
    _record_dispatch_pass(conn, dry_run=dry_run)
    # Process death and timeout expiry do not advance a content revision. Only
    # a proven empty running set may skip these sweeps, never a prior no-op.
    has_running = conn.execute("SELECT 1 FROM tasks WHERE status = 'running' LIMIT 1").fetchone() is not None
    if has_running:
        result.reclaimed = _la.timed("dispatch.release_stale_claims", _kb.release_stale_claims, conn, failure_limit=failure_limit)
        if reconcile_orphans:
            result.reconciled_orphans = _la.timed("dispatch.reconcile_orphaned_running", reconcile_orphaned_running, conn)
    if reconcile_orphans:
        _la.timed("dispatch.close_dangling_runs", close_dangling_runs, conn)  # ended/non-running tasks can still retain a running run
        result.released_leftover_claims = _la.timed("dispatch.release_leftover_local_claims", release_leftover_local_claims, conn)  # not gated: not running [daveey]
    if has_running:
        result.stale = _la.timed("dispatch.detect_stale_running", detect_stale_running, conn, stale_timeout_seconds=stale_timeout_seconds)
    from hermes_cli.kanban_db_dispatch_claims import crash_sweep_due  # G2: runs while a local pid may have died
    if crash_sweep_due(conn, has_running):
        result.crashed = _la.timed("dispatch.detect_crashed_workers", detect_crashed_workers, conn, board=board)
        result.auto_blocked.extend(getattr(detect_crashed_workers, "_last_auto_blocked", []))
        result.rate_limited.extend(getattr(detect_crashed_workers, "_last_rate_limited", []))
        result.reclaimed += len(getattr(detect_crashed_workers, "_last_owner_restarted", []))
    if has_running:
        result.timed_out = _la.timed("dispatch.enforce_max_runtime", enforce_max_runtime, conn)
    from hermes_cli.kanban_supervisor_requests import dispatcher_drain  # [daveey] non-worker executor
    result.supervisor_requests = [] if dry_run else _la.timed("dispatch.dispatcher_drain", dispatcher_drain, conn, board=board)
    from hermes_cli.kanban_continuation import wake_due_continuations
    result.woken_continuations = _la.timed("dispatch.wake_due_continuations", wake_due_continuations, conn)
    result.promoted = _la.timed("dispatch.recompute_ready", _kb.recompute_ready, conn, failure_limit=failure_limit)
    from hermes_cli.kanban_idle_candidates import sweep_shared_blockers
    _la.timed("dispatch.sweep_shared_blockers", sweep_shared_blockers, conn, result)
    from hermes_cli.kanban_provider_walls import tick_upkeep
    _la.timed("dispatch.tick_upkeep", tick_upkeep, conn, board=board, dry_run=dry_run)
    try:
        result.quota_block_released = _la.timed("dispatch.release_recovered_quota_blocks", _kbq.release_recovered_quota_blocks, conn)
    except Exception:
        _kb._log.warning("kanban: releasing recovered quota-wall blocks failed", exc_info=True)


def _tick_spawn_budget(
    conn: sqlite3.Connection,
    result: DispatchResult,
    *,
    max_spawn: Optional[int],
    max_in_progress: Optional[int],
    board: Optional[str],
    spawn_quota: Optional[int] = None,
) -> tuple[bool, Optional[int]]:
    """``(may_spawn, spawn_budget)`` for this tick; ``budget None`` = uncapped.

    ``max_spawn`` is a live per-board concurrency cap (running + this tick's
    spawns), not a per-tick budget — a per-tick reading would grow concurrency
    by N every tick. ``max_in_progress`` is a HOST-level cap: running workers on
    every other board count against the same budget, else N boards multiply the
    cap by N — exactly the fan-out the memory-derived default exists to prevent.
    """
    # Count already-running tasks so max_spawn enforces concurrency, not a
    # per-tick budget: "running" tasks stay running until the worker makes a terminal
    # board call (kanban_complete/kanban_block/kanban_request_review) or the TTL reclaims them.
    running_count = 0
    spawn_budget: Optional[int] = None
    if max_spawn is not None or max_in_progress is not None:
        running_count = count_running_tasks(conn)

    # Both ready and review loops consume from the same budget.
    if max_spawn is not None:
        if running_count >= max_spawn:
            result.at_capacity = f"max_spawn={running_count}/{max_spawn}"
            return False, None
        spawn_budget = max_spawn - running_count

    if max_in_progress is not None:
        remaining = host_remaining(max_in_progress, running_count, board, result)
        if remaining <= 0:
            result.at_capacity = f"max_in_progress={max_in_progress - remaining}/{max_in_progress}"
            return False, None
        if spawn_budget is None or spawn_budget > remaining:
            spawn_budget = remaining
    if spawn_quota is not None:  # this board's share of the host slots, ranked across boards (kanban_board_share)
        if spawn_quota <= 0:
            from hermes_cli.kanban_board_share import OTHER_BOARDS
            result.at_capacity = f"board_share=0 (free slots go to higher-ranked cards on {OTHER_BOARDS})"
            return False, None
        spawn_budget = spawn_quota if spawn_budget is None else min(spawn_budget, spawn_quota)

    # Memory-pressure guard: a static cap can't see the host's actual state.
    # critical -> spawn nothing this tick; elevated -> at most one new worker.
    # Reclaim/promotion already ran, so bookkeeping stays live; deferred tasks
    # wait for a later tick. "unknown" imposes no restriction.
    pressure = _memory_pressure_level()
    if pressure == "critical":
        result.memory_pressure = pressure
        _kb._log.warning(
            "kanban dispatch: system memory pressure is critical; "
            "spawning no new workers this tick (deferred, not dropped)"
        )
        return False, None
    if pressure == "elevated":
        result.memory_pressure = pressure
        if spawn_budget is None or spawn_budget > 1:
            _kb._log.warning(
                "kanban dispatch: system memory pressure is elevated; "
                "limiting to at most 1 new worker this tick"
            )
            spawn_budget = 1
    return True, spawn_budget


def _any_spawnable_review(
    conn: sqlite3.Connection,
    review_rows: list[sqlite3.Row],
    *,
    per_profile_cap: Optional[int] = None,
    per_profile_running: Optional[dict[str, int]] = None,
) -> bool:
    """Mirror review dispatch gates before reserving ready-lane capacity.

    Unimportable profiles fail open only when no allowlist is set. A
    review row that :func:`_dispatch_lane_task` would refuse this tick — its
    assignee already at the per-profile cap, or respawn-guarded — cannot
    consume the reservation, so it must not withhold capacity from an
    otherwise ready task (one such row would pin ``ready_budget`` to 0).
    """
    if not review_rows:
        return False
    profile_exists = _profile_exists_fn()
    running = per_profile_running or {}
    for row in review_rows:
        assignee = row["assignee"]
        if not assignee:
            continue
        if profile_exists is not None and not profile_exists(assignee):
            continue
        if per_profile_cap is not None and running.get(assignee, 0) >= per_profile_cap:
            continue
        if check_respawn_guard(conn, row["id"], lane="review") is None:
            return True
    return False


def _resolve_default_assignee(default_assignee: Optional[str]) -> Optional[str]:
    """``kanban.default_assignee`` when it names a real profile this home may
    claim (``kanban.dispatch_profiles`` gated, same predicate as the spawn
    gate). Otherwise ``None`` so an unassigned shared-board card is never
    written to. Profiles unimportable: trust the config if no allowlist is set
    (the downstream check still buckets a missing profile as nonspawnable),
    else ``None`` — the fail-closed gate claims nothing (#147531)."""
    name = (default_assignee or "").strip() or None
    if name:
        profile_exists = _profile_exists_fn()
        if profile_exists is not None and not profile_exists(name):
            return None
    return name


# The dispatch lock has been released here. Fire the tick observer strictly OUTSIDE the single-writer
# critical section (#56066 sweeper finding / #64231 disposition): a slow subscriber must never extend the
# lock hold and stall a sibling dispatcher's tick.
TRIAGE_STALE_SECONDS = 3600


def sweep_stale_triage(
    conn: sqlite3.Connection, *, default_assignee: Optional[str] = None,
    older_than: int = TRIAGE_STALE_SECONDS, now: Optional[int] = None,
) -> list[str]:
    """Release stale triage cards once per triage entry (``hermes_cli.kanban_triage_sweep``). [daveey]"""
    from hermes_cli.kanban_triage_sweep import sweep_stale_triage as _sweep
    return _sweep(conn, default_assignee=default_assignee, older_than=older_than, now=now)


def _dispatch_once_locked(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    max_spawn: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    board: Optional[str] = None,
    default_assignee: Optional[str] = None,
    max_in_progress_per_profile: Optional[int] = None,
    reconcile_orphans: bool = True,
    spawn_quota: Optional[int] = None,
) -> DispatchResult:
    """One dispatcher tick: reclaim stale/crashed running tasks, promote
    todo -> ready, then atomically claim each spawnable ready/review row and
    call ``spawn_fn(task, workspace_path, board) -> Optional[int]``, recording
    the PID so later ticks catch crashes before the TTL. Cap semantics:
    :func:`_tick_spawn_budget`."""
    result = DispatchResult()
    _run_reclaim_phase(
        conn, result, stale_timeout_seconds=stale_timeout_seconds,
        failure_limit=failure_limit, reconcile_orphans=reconcile_orphans, board=board, dry_run=dry_run,
    )
    if not dry_run:
        from hermes_cli.kanban_dispatch_maintenance import sweep
        _la.timed("dispatch.maintenance", sweep, conn, result, _guard_notice, _parent_unclaimed)
    from hermes_cli.kanban_gateway_drain import dispatch_paused
    if dispatch_paused():
        _kb._log.info('kanban dispatcher: scheduling paused — gateway draining or restart requested')
        return result
    if not dry_run:
        from hermes_cli.kanban_worker_placement import dispatch_tick
        try:
            _la.timed("dispatch.worker_placement", dispatch_tick, conn)
        except Exception:
            logger = _kb._log
            logger.warning("kanban worker placement failed; continuing local dispatch", exc_info=True)
    from hermes_cli.kanban_gateway_drain import core_update_hold
    result.core_update_drain_hold = core_update_hold()
    if result.core_update_drain_hold is not None:  # every caller (CLI, dashboard, gateway) claims nothing
        return result
    may_spawn, spawn_budget = _tick_spawn_budget(conn, result, max_spawn=max_spawn, max_in_progress=max_in_progress,
                                                 board=board, spawn_quota=spawn_quota)
    if not may_spawn:
        return _kb._sync.worker_pickup_capped(conn, result, dry_run, _profile_exists_fn)  # record why cards wait

    result.scheduling_admitted = not dry_run
    if not dry_run:
        try:
            sweep_stale_triage(conn, default_assignee=_resolve_default_assignee(default_assignee))
        except Exception:
            _kb._log.debug("kanban dispatch: triage sweep failed", exc_info=True)
    unjudged = _kb._sync.worker_pickup(conn, pickup_budget(result, spawn_budget), dry_run, _profile_exists_fn) or ()
    # Cards this machine cannot lease (runs_on for another machine, ...) are never candidates: before 10-28 a
    # review card pinned to the hub sat first in the MacBook's review lane, took the review reservation every
    # tick, and the Board refused its lease every tick, so the eligible ready queue never started.
    from hermes_cli.kanban_hub_admission_claim import screened_lanes
    all_ready, all_review, unjudged = screened_lanes(conn, dry_run, review_dispatch_enabled(), unjudged)
    result.admission_deferred = sorted(unjudged)
    from hermes_cli.kanban_db_dispatch_preclaim import eligible_rows as _eligible_rows
    ready_rows = _eligible_rows(conn, all_ready, "ready", result)
    review_rows = _eligible_rows(conn, all_review, "review", result) if all_review else []
    result.lane_candidates = len(all_ready) + len(all_review)
    # Per-profile cap. Deferred tasks go to skipped_per_profile_capped, not
    # skipped_unassigned — "busy, retry later" differs from "needs routing".
    # Resolved BEFORE the review reservation so the reservation can see which
    # review rows the lane loop would refuse this tick.
    per_profile_cap = max_in_progress_per_profile if (
        # Per-profile concurrency cap (#21582): when set, track how many workers each assignee already has
        # in flight, and refuse to spawn when this would push that assignee past the cap. Prevents fan-out
        # workloads from melting a single profile's local model / API quota / browser pool while leaving
        # other profiles idle.
        isinstance(max_in_progress_per_profile, int)
        and max_in_progress_per_profile > 0
    ) else None
    per_profile_running: dict[str, int] = {}
    if per_profile_cap is not None:
        for prow in conn.execute(
            "SELECT assignee, COUNT(*) AS n FROM tasks WHERE status = 'running' AND assignee IS NOT NULL"
            + _kb._sync.held_elsewhere_sql(conn) + " GROUP BY assignee"
        ):
            per_profile_running[prow["assignee"]] = int(prow["n"])
    # One scoring order across both lanes: reserving the only slot for review
    # indefinitely starves ready work. Aging protects either lane without an
    # unconditional partition; pool and per-profile gates remain authoritative.
    from hermes_cli.kanban_dispatch_priority import order_with_remote_waiters

    lane_by_id = {row["id"]: "review" for row in review_rows}
    rows = order_with_remote_waiters(conn, ready_rows + review_rows)
    lane_kwargs: dict[str, Any] = dict(
        dry_run=dry_run, ttl_seconds=ttl_seconds, board=board,
        failure_limit=failure_limit, spawn_fn=spawn_fn,
        per_profile_cap=per_profile_cap, per_profile_running=per_profile_running,
    )
    default_assignee = _resolve_default_assignee(default_assignee)
    spawned = 0
    for row in rows:
        if spawn_budget is not None and spawned >= spawn_budget:
            break
        if not pool_admits(result, row):
            continue
        lane = lane_by_id.get(row["id"], "ready")
        row_assignee = row["assignee"]
        if not row_assignee:
            if lane != "ready":
                result.skipped_unassigned.append(row["id"])
                continue
            row_assignee = _kbda.fill_default_assignee(conn, row["id"], default_assignee, result, dry_run=dry_run)
            if not row_assignee:
                continue
        if _dispatch_lane_task(conn, row, row_assignee, result, lane=lane, **lane_kwargs):
            spawned += pool_take(result, row)
        elif (row["id"], "capacity") in result.claim_deferred:
            break  # a mid-pass slot release must not reward a lower-ranked row
    return result


from hermes_cli.kanban_db_dispatch_limits import _positive_int  # re-export for ops and worker logs


def _module_hermes_argv() -> list[str]:
    """Interpreter-bound Hermes CLI invocation (``hermes_cli.main`` is the
    console-script target — there is no top-level ``hermes`` package). ``-P``: the cwd is
    the task workspace, which must never shadow the install (see ``hermes_module_argv``)."""
    from cron.scheduler_worker_env import hermes_module_argv
    return hermes_module_argv(sys.executable)


def _propagate_module_import_root(cmd: list[str], env: dict[str, str]) -> None:
    """Put the running install's package root on a module-form worker's path.

    ``_resolve_hermes_argv`` proves ``hermes_cli`` importable in THIS process,
    where a store-python shim has the repo root on ``sys.path`` in-process;
    the spawned child runs the bare ``sys.executable`` from the task workspace
    with a scrubbed ``PYTHONPATH`` and cannot import the package the parent
    just proved importable — it dies before any work and the board
    auto-blocks (#122299, #122487, #122500). Same-interpreter child, so the
    root is version-safe to propagate; ``hermes_cli.main``'s own bootstrap
    then owns dependency activation as usual. A resolved shim path owns its
    imports and is left alone. Same pin cron's external worker uses (#112729)."""
    from cron.scheduler_worker_env import is_hermes_module_argv, pin_hermes_tree_on_pythonpath
    if not is_hermes_module_argv(cmd):
        return
    pin_hermes_tree_on_pythonpath(env, Path(_startup_fast.project_root_str()))


def _absolute_hermes_path(path: str) -> str:
    """Return an absolute filesystem path for a resolved Hermes shim."""
    expanded = os.path.expanduser(path)
    return expanded if os.path.isabs(expanded) else os.path.abspath(expanded)


from hermes_cli.kanban_dispatch_paths import _looks_like_path, _is_windows_batch_shim


def _path_search_names(command: str) -> list[str]:
    """Return executable names to try for an unqualified command."""
    if not _kb._IS_WINDOWS or os.path.splitext(command)[1]:
        return [command]
    raw = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
    return [command + ext for ext in raw.split(";") if ext]


def _safe_which_no_cwd(command: str) -> Optional[str]:
    """Resolve a bare command from PATH without implicit current-dir search.

    On Windows ``shutil.which`` may search the current directory before PATH
    for bare names — unsafe for a dispatcher. Only explicit PATH entries are
    considered; empty / ``.`` entries are skipped.
    """
    for raw_dir in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_dir or raw_dir == ".":
            continue
        directory = os.path.expanduser(raw_dir)
        for name in _path_search_names(command):
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and (_kb._IS_WINDOWS or os.access(candidate, os.X_OK)):
                return candidate
    return None


def _hermes_path_argv(path: str) -> list[str]:
    """argv for a resolved Hermes executable path. Windows batch shims
    (``.cmd``/``.bat``) are unsafe as argv[0] because the argument vector
    includes task-derived values; prefer the module form."""
    if _kb._IS_WINDOWS and _is_windows_batch_shim(path):
        return _module_hermes_argv()
    return [_absolute_hermes_path(path)]


def _resolve_hermes_argv() -> list[str]:
    """Resolve the ``hermes`` invocation as argv for ``Popen``: ``$HERMES_BIN``
    (path-like -> absolute; bare names keep PATH semantics, never a
    same-directory file), then the running interpreter's ``sys.executable -m
    hermes_cli.main`` (exactly this install; also covers shim-less cron,
    systemd ``User=``, launchd), then ``which("hermes")`` (Windows: safe PATH
    search, batch shims fall back to the module form) only when ``hermes_cli``
    is not importable. The module argv must win over PATH: a PATH-first lookup
    lets an attacker-planted ``hermes`` shadow the running install (#111569).
    Mirrors ``gateway.run._resolve_hermes_bin``; local because ``hermes_cli``
    sits below ``gateway`` in the dependency order.
    """
    import importlib.util
    import shutil

    env_bin = os.environ.get("HERMES_BIN", "").strip()
    if env_bin:
        if _looks_like_path(env_bin):
            return _hermes_path_argv(env_bin)
        resolved_env_bin = _safe_which_no_cwd(env_bin)
        if resolved_env_bin:
            return _hermes_path_argv(resolved_env_bin)
        return _module_hermes_argv()

    try:
        if importlib.util.find_spec("hermes_cli") is not None:
            return _module_hermes_argv()
    except Exception:
        pass

    hermes_bin = _safe_which_no_cwd("hermes") if _kb._IS_WINDOWS else shutil.which("hermes")
    if hermes_bin:
        return _hermes_path_argv(hermes_bin)
    return _module_hermes_argv()


def _worker_terminal_timeout_env(
    max_runtime_seconds: Optional[int],
    current_timeout: Optional[str],
) -> Optional[str]:
    """Return a worker-scoped TERMINAL_TIMEOUT override, if needed.

    When ``max_runtime_seconds`` exceeds the terminal tool's default timeout,
    raise only the child's default so a long command isn't killed by the
    generic terminal default first.
    """
    if max_runtime_seconds is None:
        return None
    try:
        runtime = int(max_runtime_seconds)
    except (TypeError, ValueError):
        return None
    if runtime <= 0:
        return None

    desired = max(1, runtime - KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS)
    try:
        existing = int(str(current_timeout).strip()) if current_timeout else 0
    except (TypeError, ValueError):
        existing = 0
    if existing >= desired:
        return None
    return str(desired)


@contextlib.contextmanager
def _worker_profile_scope(hermes_home: str):
    """Bind an assigned profile's whole runtime scope (home, secrets, terminal policy) for one
    dispatch-side read or spawn-env build.

    The dispatcher runs detached from any turn, so nothing binds a profile for it: ``load_config``,
    the toolset probes' ``get_secret`` reads and ``build_subprocess_env``'s passthrough resolution
    all fall back to the LAUNCH profile's ambient ``os.environ`` / ``TERMINAL_*``. Binding was
    previously conditional on ``is_multiplex_active()``, so on a single-profile host a worker for
    profile B was built entirely from the dispatcher's own environment.

    Home, secrets and terminal policy always move together: the spawned child only submits the
    claim to the assignee's own gateway, which runs the turn (and, if the child starts it, gets
    the assignee's own secrets from ``served_profile_child_env``).

    The secret mapping is never widened: a profile that is not this process's own home gets its own
    ``.env`` + external sources ONLY, while the launch home keeps its established
    env-over-``.env`` precedence (``launch_secret_scope``) so systemd / ``op run`` injection still
    resolves for a standalone dispatcher.
    """
    from agent.runtime_scope import bind_profile_scope
    with bind_profile_scope(hermes_home, hydrate_secrets=False):
        yield


def _resolve_worker_cli_toolsets(hermes_home: Optional[str]) -> Optional[list[str]]:
    """Return the assigned profile's effective CLI toolsets for a worker.

    Resolved at dispatch time and passed as an explicit ``--toolsets`` pin so
    worker startup cannot fall back to a stale root/active-profile config or a
    profile whose top-level ``toolsets`` is only the kanban orchestrator
    surface. ``model_tools`` still appends the task-scoped kanban lifecycle
    tools when ``HERMES_KANBAN_TASK`` is set.
    """
    if not hermes_home:
        return None
    try:
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools

        with _worker_profile_scope(hermes_home):
            cfg = load_config()
            toolsets = sorted(_get_platform_tools(cfg, "cli"))
        return toolsets or None
    except Exception as exc:
        _kb._log.debug(
            "kanban worker: could not resolve CLI toolsets for HERMES_HOME=%r (%s)",
            hermes_home,
            exc,
        )
        return None


_retagged_workspace_roots: set[str] = set()

def _retag_legacy_worker_sessions(workspaces_root_path: str) -> None:
    """Reclaim pre-tag worker rows in state.db so they leave the session lists.

    Best-effort: the durable gate is ``state_meta`` in
    ``retag_kanban_worker_sessions``; the in-process set avoids reopening
    state.db on every spawn. A tick must never fail because a session DB was
    busy or missing.
    """
    if workspaces_root_path in _retagged_workspace_roots:
        return
    try:
        from hermes_state_registry import acquire, release_or_close

        # Inside the gateway the dispatcher shares the process's registry handle; a bare
        # SessionDB() here was one more writer connection on the same state.db (#100896).
        db = acquire()
        try:
            db.retag_kanban_worker_sessions(workspaces_root_path)
        finally:
            release_or_close(db)
        _retagged_workspace_roots.add(workspaces_root_path)
    except Exception as exc:
        _kb._log.debug("kanban worker: legacy session retag skipped (%s)", exc)


def _worker_argv(task: Task, profile_arg: str, hermes_home: Optional[str]) -> list[str]:
    """The profile owner executes; this subprocess only waits for its receipt.

    [daveey] ``-P``: the worker runs with cwd = the task workspace, which for a hermes-agent worktree
    card is another Hermes checkout; plain ``-m`` put that cwd first on ``sys.path`` and the worker
    imported the worktree's ``hermes_cli`` (see ``hermes_module_argv``)."""
    import sys
    from cron.scheduler_worker_env import hermes_module_argv
    return hermes_module_argv(sys.executable, "hermes_cli.kanban_worker_client")


def _open_worker_log(task: Task, board: Optional[str]):
    """Append-mode per-task log (a re-run on unblock appends, never overwrites),
    rotated first. Anchored at the board root (not the shared kanban root) so
    `hermes kanban log` reads its own file and boards sharing task ids don't
    collide."""
    log_dir = _kb.worker_logs_dir(board=board)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task.id}.log"
    from hermes_cli.kanban_worker_log import open_worker_log
    log = open_worker_log(log_path)
    log.write(f'\n[kanban-worker-start] run_id={getattr(task, "current_run_id", None)}\n'.encode())
    log.flush()  # must reach disk before a child can fail (even before importing its client)
    return log


def _restart_safe_worker_argv(task: Task, command: list[str], board: Optional[str] = None) -> list[str]:
    """Wrap a systemd-hosted dispatcher's worker in the shared restart-safe scope.

    Kanban workers are long-lived agentic runs that outlive the dispatcher
    tick, so they never take cron's degraded mode under the managed gateway:
    ``require_restart_safe_scope=True`` makes the helper raise
    ``RestartSafeScopeUnavailable`` there (an infrastructure spawn failure the
    dispatcher does not charge to the card). Under any other systemd unit
    (``Type=oneshot`` dispatch timers, #113612) ``outlives_parent=True`` gets the
    worker its own scope so the unit's cgroup teardown cannot kill it.
    """
    from tools.process_registry import restart_safe_gateway_child_argv

    if task.current_run_id is None:
        # Outside managed systemd this is harmless, but a managed dispatch must
        # never mint an untraceable worker.  Check topology through the shared
        # helper first, using a placeholder suffix that cannot be launched.
        dispatch = restart_safe_gateway_child_argv(
            command,
            unit_suffix=f"kanban-{task.id}-run-missing",
            require_restart_safe_scope=True,
            outlives_parent=True,
        )
        if dispatch.mode != "in_process":
            raise RuntimeError(
                "cannot create restart-safe systemd scope for Kanban worker: "
                "the claimed task has no current run id"
            )
        return command

    argv = _worker_scope.retain_failed_scope(restart_safe_gateway_child_argv(
        command,
        unit_suffix=f"kanban-{task.id}-run-{task.current_run_id}",
        require_restart_safe_scope=True,
        outlives_parent=True,
    ).argv)
    argv = _worker_scope.oom_policy_continue(_worker_scope.apply_oom_recovery(argv, task.id, board))  # before bounded_scope: its `-- nice -n 15` must follow OOMPolicy, which apply_toolchain_env reads before the first `--`
    return _aggregate_scope.bounded_scope(argv, task_id=task.id, board=str(_kb.kanban_db_path(board=board).resolve()))  # pool + per-card slice


def _default_spawn(task: Task, workspace: str, *, board: Optional[str] = None, observation=None) -> Optional[int]:
    """Fire-and-forget worker client (``-m hermes_cli.kanban_worker_client``) for the claim.

    Returns the child's PID so the dispatcher can detect crashes before the
    claim TTL expires; completion is still observed via the worker's own
    ``complete`` / ``block`` transitions. ``board`` pins the child's
    ``HERMES_KANBAN_DB`` / ``HERMES_KANBAN_BOARD`` / workspaces_root to the
    board the task was claimed from, so workers cannot see other boards.
    """
    if observation is not None:
        from native_attestations import host_only
        host_only()
    if not task.assignee:
        raise ValueError(f"task {task.id} has no assignee")

    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env

    profile_arg = normalize_profile_name(task.assignee)

    from agent.secret_scope import is_multiplex_active
    from hermes_cli.kanban_worker_env import worker_client_env
    from tools.environments.local import _is_routed_home, build_subprocess_env

    try:
        profile_home = resolve_profile_env(profile_arg)
    except FileNotFoundError:
        # No profile dir (isolated test fixtures) — the CLI resolves it from
        # HERMES_PROFILE (set below) instead.
        profile_home = None

    # Scrub for a ROUTED home, not only under multiplex: the authority test is "does this worker act
    # for another profile", exactly as served_profile_child_env decides it (tools/environments/local.py).
    # Gating on the gateway-wide flag left B's worker inheriting the dispatcher's own OPENAI_API_KEY and
    # systemd-injected tokens on every single-profile host.
    routed = bool(profile_home) and _is_routed_home(profile_home)
    # build_subprocess_env's secret scrub resolves terminal.env_passthrough vars through get_secret(),
    # which without a bound scope reads the LAUNCH profile's ambient environment for a worker spawned
    # on B's behalf (and raises under multiplex) — so bind B's whole scope around the build.
    with (_worker_profile_scope(profile_home) if profile_home
          else contextlib.nullcontext()):
        env = build_subprocess_env(
            scrub_secrets=is_multiplex_active() or routed,
            inherit_profile_home=True,
        )
    # For another profile, host plumbing only: a gateway the client cold-starts gets this env plus the
    # assignee's secrets, which replace values but never remove one (kanban_worker_env).
    if routed:
        env = worker_client_env(env, profile_home)
    # The dispatcher is detached from every conversation; its worker must never inherit routing mirrored
    # by a previous gateway turn (nor its session id in the ssh agent marker its tools re-stamp). [daveey]
    from gateway.session_context import _VAR_MAP
    env = {k: v for k, v in env.items() if k not in _VAR_MAP} | {"LC_HERMES_AGENT": "1"}

    # Inject HERMES_HOME so the worker reads the profile-scoped config.yaml:
    # without it the child's get_hermes_home() falls back to the DEFAULT
    # profile root because `hermes -p` applies its override before
    # hermes_constants is imported.
    if profile_home:
        env["HERMES_HOME"] = profile_home
    if task.tenant:
        env["HERMES_TENANT"] = task.tenant
    env["HERMES_KANBAN_TASK"] = task.id
    env["HERMES_KANBAN_WORKSPACE"] = workspace
    # Tag the session `kanban` so session-browsing surfaces filter it out by
    # source instead of rendering one sidebar row per attempt.
    env["HERMES_SESSION_SOURCE"] = "kanban"
    # TERMINAL_CWD takes precedence over process cwd in file_tools and
    # build_context_files_prompt; without it relative writes land in the gateway
    # user's home and workers load the gateway's AGENTS.md. file_tools rejects
    # relative / sentinel values, so only set a real absolute directory.
    # Pin TERMINAL_CWD to the task's workspace so the worker's file tools and context-file loader anchor on
    # the workspace, not whatever cwd the dispatching gateway happened to export. The worker subprocess is
    # already launched with cwd=workspace, but TERMINAL_CWD takes precedence over the process cwd in both
    # file_tools._resolve_base_dir (#41312 — relative write_file paths were landing in the gateway user's
    # home) and build_context_files_prompt (#34619 — workers loaded the dispatching gateway's AGENTS.md
    # instead of the task's). Setting it to the workspace fixes both: the workspace is where the task's work
    # actually happens.
    if workspace and os.path.isabs(workspace) and os.path.isdir(workspace):
        env["TERMINAL_CWD"] = workspace
    env.update({"HERMES_KANBAN_BRANCH": task.branch_name} if task.branch_name else {},
               **(getattr(task, "source_env", None) or {}))  # workspace_source: HERMES_KANBAN_SOURCE_{COMMIT,PATH,BRANCH}
    if task.current_run_id is not None:
        env["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)
    if task.claim_lock:
        env["HERMES_KANBAN_CLAIM_LOCK"] = task.claim_lock
    # Goal-loop mode (Ralph-style /goal judge loop in cli.py quiet-mode path).
    # Only set when enabled so non-goal tasks keep a clean env.
    if task.goal_mode:
        env["HERMES_KANBAN_GOAL_MODE"] = "1"
        if task.goal_max_turns is not None:
            env["HERMES_KANBAN_GOAL_MAX_TURNS"] = str(int(task.goal_max_turns))
    for var in ("TERMINAL_TIMEOUT", "TERMINAL_MAX_FOREGROUND_TIMEOUT"):
        override = _worker_terminal_timeout_env(task.max_runtime_seconds, env.get(var))
        if override is not None:
            env[var] = override
    # Pin the board DB + workspaces root so the worker's kanban paths still
    # match after `hermes -p` rewrites HERMES_HOME (symlink / Docker layouts).
    env["HERMES_KANBAN_DB"] = str(_kb.kanban_db_path(board=board))
    env["HERMES_KANBAN_WORKSPACES_ROOT"] = str(_kb.workspaces_root(board=board))
    # Board slug — defense-in-depth pin if a path is resolved without the
    # DB / workspaces env vars.
    env["HERMES_KANBAN_BOARD"] = _kb._normalize_board_slug(board) or _kb.get_current_board()
    # kanban_comment reads HERMES_PROFILE for its default author; `-p` alone
    # doesn't set the env var.
    env["HERMES_PROFILE"] = profile_arg
    # This is the grant boundary: the dispatcher assigned this new worker's task. HERMES_TUI: older
    # hermes builds on PATH predate `--cli`'s precedence.
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER
    env.pop(DELEGATED_CHILD_ENV_MARKER, None)
    env.pop("HERMES_TUI", None)

    cmd = _worker_argv(task, profile_arg, env.get("HERMES_HOME"))
    # The worker is `-m hermes_cli.kanban_worker_client` with no console-script bootstrap, and the scrub
    # above strips our PYTHONPATH entries, so a PYTHONPATH-launched dispatcher's child died on
    # `No module named 'hermes_cli'` (#112729). Pinned last, after every other env decision.
    from cron.scheduler_worker_env import pin_hermes_tree_on_pythonpath
    env = pin_hermes_tree_on_pythonpath(env, Path(project_root_str()))
    # [daveey] Deferred placement: a card with parents that nothing pins here runs on the routed gateway.
    # Its forwarder (seconds, no LLM) replaces the worker; on any failure it execs ``cmd`` itself.
    from hermes_cli.kanban_placement_forward import forward_argv
    forward = forward_argv(task, board, cmd)
    if forward:
        cmd = forward
    # A worker spawned by a managed systemd gateway must leave the gateway's
    # cgroup before startup; otherwise restarting the service kills the worker
    # that is performing the handoff.
    cmd = _restart_safe_worker_argv(task, cmd, board)
    _worker_scope.apply_toolchain_env(env, cmd)
    from tools.process_registry import systemd_user_bus_env
    env = systemd_user_bus_env(env)
    held = _custody.reserve()  # BEFORE the child exists; released by the reap that proves its exit
    try:
        proc = _popen_worker(task, board, cmd, workspace, env, observation)
    except BaseException:
        _custody.abort(held)  # no child was created
        raise
    _live_worker_procs[proc.pid] = proc
    _custody.adopt_spawned(proc, held, _process_fingerprint)
    return proc.pid


def _popen_worker(task: Task, board: Optional[str], cmd, workspace: str, env, observation):
    """Open the worker log and start the detached child; a raise means no child exists."""
    log_f = _open_worker_log(task, board)
    try:
        proc = (observation or subprocess.Popen)(
            cmd,
            cwd=workspace if os.path.isdir(workspace) else None,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,  # detached-spawn: ok — restart-safe kanban worker, must survive the dispatcher
            creationflags=subprocess.CREATE_NO_WINDOW if _kb._IS_WINDOWS else 0,
        )
    except FileNotFoundError:
        log_f.close()
        raise RuntimeError(
            "`hermes` executable not found on PATH. "
            "Install Hermes Agent or activate its venv before running the kanban dispatcher."
        )
    except BaseException:
        log_f.close()
        raise
    # Intentionally NOT closing log_f: the child keeps writing after return;
    # the OS-level FD stays open in the child until it exits.
    return proc


# ---------------------------------------------------------------------------
# Long-lived dispatcher daemon
# ---------------------------------------------------------------------------

def run_daemon(
    *,
    interval: float = 60.0,
    max_spawn: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stop_event=None,
    on_tick=None,
) -> None:
    """Run the dispatcher in a loop until interrupted.

    Calls :func:`dispatch_once` every ``interval`` seconds; exits cleanly on
    SIGINT / SIGTERM so it is systemd-friendly. ``stop_event`` and ``on_tick``
    are test hooks. Each tick resolves ``kanban.max_in_progress`` exactly like
    the gateway dispatcher and ``hermes kanban dispatch`` — the standalone
    daemon must not be the one uncapped entry point.
    """
    import threading

    if stop_event is None:
        stop_event = threading.Event()

    def _handle(_signum, _frame):
        stop_event.set()

    # Install handlers only on the main thread — tests call this inline from
    # worker threads and signal() would raise there.
    if threading.current_thread() is threading.main_thread():
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, _handle)

    while not stop_event.is_set():
        try:
            # Re-resolved every tick (config load is mtime-cached) so operator
            # edits apply without a restart.
            max_in_progress = resolve_max_in_progress(configured_max_in_progress())
            with contextlib.closing(_kbc.connect()) as conn:
                res = dispatch_once(
                    conn,
                    max_spawn=max_spawn,
                    max_in_progress=max_in_progress,
                    failure_limit=failure_limit,
                )
            if on_tick is not None:
                with contextlib.suppress(Exception):
                    on_tick(res)
        except Exception:
            # Don't let any single tick kill the daemon.
            import traceback
            traceback.print_exc()
        stop_event.wait(timeout=interval)


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb
from hermes_cli import kanban_db_connect as _kbc
from hermes_cli import kanban_db_default_assignee as _kbda
from hermes_cli import kanban_db_workspace as _kbw
from hermes_cli import kanban_source_dispatch as _source_dispatch
