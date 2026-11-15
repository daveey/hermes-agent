"""Dispatcher and maintenance verbs for ``hermes kanban``: ``dispatch``,
``daemon`` (deprecated standalone loop), ``tail``/``watch`` event streaming,
``gc`` and ``repair``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
import time
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_hub_admission_claim import print_deferred
from hermes_cli.kanban_no_progress import held_json, print_held
from hermes_cli.kanban_output import _err, _fmt_ts, _print_json


def _kanban_config() -> dict:
    """``config.yaml`` ``kanban:`` section, or ``{}`` when config can't be loaded."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        return (cfg.get("kanban", {}) if isinstance(cfg, dict) else {}) or {}
    except Exception:
        return {}


def _poll_loop(interval: float, tick) -> int:
    """Run ``tick()`` every ``interval`` seconds (floor 0.1) until Ctrl-C."""
    try:
        while True:
            tick()
            time.sleep(max(0.1, interval))
    except KeyboardInterrupt:
        print("\n(stopped)")
        return 0


def _cmd_tail(args: argparse.Namespace) -> int:
    last_id = 0
    print(f"Tailing events for {args.task_id}. Ctrl-C to stop.")

    def tick():
        nonlocal last_id
        with kbc.connect_closing() as conn:
            events = kb.list_events(conn, args.task_id)
        for e in events:
            if e.id > last_id:
                pl = f" {e.payload}" if e.payload else ""
                print(f"[{_fmt_ts(e.created_at)}] {e.kind}{pl}", flush=True)
                last_id = e.id

    return _poll_loop(args.interval, tick)


def _print_zero_spawn_reasons(res) -> None:
    """Name a full cap or a core-update drain hold so "Spawned: 0" is not misread as stuck. [daveey]"""
    if res.at_capacity:
        print(f"At capacity:  {res.at_capacity} (ready work waits for a running worker to finish)")
    if hold := res.core_update_drain_hold:
        print(f"Held: core-update drain (pid {hold.get('pid')}, {hold.get('path')}); no new claims, "
              "running workers continue")


def _cmd_dispatch(args: argparse.Namespace) -> int:
    # Honour kanban.default_assignee, kanban.max_in_progress,
    # kanban.max_in_progress_per_profile and kanban.max_spawn with the same
    # semantics as the gateway dispatch path.
    try:
        from hermes_cli.config import load_config
        _cfg = load_config()
        _kanban_cfg = _cfg.get("kanban", {}) if isinstance(_cfg, dict) else {}
        default_assignee = (_kanban_cfg.get("default_assignee") or "").strip() or None
        max_in_progress_per_profile = kbd._positive_int(
            _kanban_cfg.get("max_in_progress_per_profile"), None
        )
        # Memory-derived default when unset — same fallback the gateway applies.
        max_in_progress = kbd.resolve_max_in_progress(
            kbd._positive_int(_kanban_cfg.get("max_in_progress"), None)
        )
        # CLI --max is the more explicit signal, so it wins over kanban.max_spawn.
        cli_max = getattr(args, "max", None)
        max_spawn = (
            cli_max if cli_max is not None else kbd._positive_int(_kanban_cfg.get("max_spawn"), None)
        )
    except Exception:
        default_assignee = max_in_progress_per_profile = max_in_progress = None
        max_spawn = getattr(args, "max", None)
    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(
            conn,
            dry_run=args.dry_run,
            max_spawn=max_spawn,
            max_in_progress=max_in_progress,
            failure_limit=getattr(args, "failure_limit", kbd.DEFAULT_FAILURE_LIMIT),
            default_assignee=default_assignee,
            max_in_progress_per_profile=max_in_progress_per_profile,
        )
    if getattr(args, "json", False):
        _print_json({
            **{k: getattr(res, k)
               for k in ("reclaimed", "crashed", "timed_out", "stale", "auto_blocked", "promoted",
                         "reaped_terminal_workers", "stopped_run_scopes")},
            "spawned": [
                {"task_id": tid, "assignee": who, "workspace": ws} for (tid, who, ws) in res.spawned
            ],
            "skipped_unassigned": res.skipped_unassigned,
            "skipped_nonspawnable": res.skipped_nonspawnable,
            "skipped_per_profile_capped": [
                {"task_id": tid, "assignee": who, "current": current}
                for (tid, who, current) in res.skipped_per_profile_capped
            ],
            "auto_assigned_default": res.auto_assigned_default,
            "respawn_guarded": [
                {"task_id": tid, "reason": reason}
                for (tid, reason) in res.respawn_guarded
            ],
            "rate_limited": res.rate_limited,
            "no_progress_held": held_json(res.no_progress_held),
            # Cards this machine cannot lease (runs_on / placement) and claims the Board deferred. [daveey]
            "skipped_ineligible": [{"task_id": tid, "reason": reason} for (tid, reason) in res.skipped_ineligible],
            "claim_deferred": [{"task_id": tid, "reason": reason} for (tid, reason) in res.claim_deferred],
            "admission_deferred": res.admission_deferred,  # hub claim screen: unjudged, judged next tick
            "shared_blocker_released": res.shared_blocker_released,
            "shared_blocker_probed": res.shared_blocker_probed,
            "skipped_locked": res.skipped_locked,
            "skipped_owned": res.skipped_owned,
            "dispatch_owner": res.dispatch_owner,
            "ownership_error": res.ownership_error,
            "memory_pressure": res.memory_pressure,
            # A full host/board cap is the usual reason for zero spawns; without it a
            # bare "spawned: []" reads as a stuck dispatcher. [daveey]
            "at_capacity": res.at_capacity,
            # A live hermes-sync core-update drain hold refused every claim this tick. [daveey]
            "core_update_drain_hold": res.core_update_drain_hold,
        }, ascii=True)
        return 0
    print(f"Reclaimed:    {res.reclaimed}")
    if res.reaped_terminal_workers:
        print(f"Reaped workers of finished tasks: {', '.join(res.reaped_terminal_workers)}")
    for label, items in (
        ("Crashed:     ", res.crashed),
        ("Timed out:   ", res.timed_out),
        ("Stale:       ", res.stale),
        ("Auto-blocked:", res.auto_blocked),
    ):
        print(f"{label} {len(items)}")
        if items:
            print(f"  {', '.join(items)}")
    print(f"Promoted:     {res.promoted}")
    print(f"Spawned:      {len(res.spawned)}")
    _print_zero_spawn_reasons(res)
    tag = " (dry)" if args.dry_run else ""
    for tid, who, ws in res.spawned:
        print(f"  - {tid}  ->  {who}  @ {ws or '-'}{tag}")
    if res.auto_assigned_default:
        print(
            f"Auto-assigned to kanban.default_assignee={default_assignee!r}: "
            f"{', '.join(res.auto_assigned_default)}"
        )
    if res.skipped_unassigned:
        print(f"Skipped (unassigned): {', '.join(res.skipped_unassigned)}")
    for tid, who, current in res.skipped_per_profile_capped:
        print(f"Deferred ({who} at per-profile cap, {current} running): {tid}")
    if res.skipped_nonspawnable:
        print(
            f"Skipped (non-spawnable assignee — terminal lane, OK): "
            f"{', '.join(res.skipped_nonspawnable)}"
        )
    for tid, reason in res.respawn_guarded:
        print(f"Guarded ({reason}): {tid}")
    print_held(res.no_progress_held)
    for tid, reason in res.skipped_ineligible:
        print(f"Skipped (this machine cannot lease it: {reason}): {tid}")
    for tid, reason in res.claim_deferred:
        print(f"Claim deferred by the Board ({reason}): {tid}")
    print_deferred(res.admission_deferred)
    if res.rate_limited:
        print(f"Rate-limited (released to ready, no failure counted): {', '.join(res.rate_limited)}")
    if res.shared_blocker_released:
        print(f"Released (shared blocker cleared): {', '.join(res.shared_blocker_released)}")
    if res.shared_blocker_probed:
        print(f"Released (probe for an idle shared blocker): {', '.join(res.shared_blocker_probed)}")
    if res.skipped_locked:
        print("Skipped: another dispatcher holds this board's lock (no writes this tick)")
    if res.skipped_owned:
        print(f"Skipped: dispatch owner {res.dispatch_owner or '(unknown)'}; "
              f"{res.ownership_error or 'dispatcher identity does not match'}")
    if res.memory_pressure:
        print(f"Memory pressure {res.memory_pressure}: new workers restricted this tick")
    return 0


_DAEMON_DEPRECATED = (
    "hermes kanban daemon: DEPRECATED — the dispatcher now runs\ninside the gateway. To use "
    "kanban:\n\n    hermes gateway start       # starts the gateway + embedded dispatcher\n\nReady "
    "tasks will be picked up on the next dispatcher tick\n(default: every 60 seconds). Configure "
    "via config.yaml:\n\n    kanban:\n      dispatch_in_gateway: true      # default\n      "
    "dispatch_interval_seconds: 60\n      failure_limit: 2              # consecutive non-success "
    "attempts before auto-block\n\nRunning both the gateway AND this standalone daemon will\nrace "
    "for claims. If you truly need the old standalone\ndaemon (no gateway available), rerun with "
    "--force."
)


def _cmd_daemon(args: argparse.Namespace) -> int:
    """Deprecated — the dispatcher now runs inside the gateway. Kept so old
    scripts/systemd units get a clear migration message; ``--force`` (hidden
    from --help) keeps the standalone loop for hosts that truly cannot run the
    gateway. The default path exits 2 so nobody accidentally runs two
    dispatchers against the same kanban.db."""
    if not getattr(args, "force", False):
        return _err(_DAEMON_DEPRECATED, 2)

    # Init before printing "started" so the DB path is right and init errors
    # surface immediately.
    kb.init_db()

    pidfile = getattr(args, "pidfile", None)
    if pidfile:
        try:
            Path(pidfile).parent.mkdir(parents=True, exist_ok=True)
            Path(pidfile).write_text(str(os.getpid()), encoding="utf-8")
        except OSError as exc:
            print(f"warning: could not write pidfile {pidfile}: {exc}", file=sys.stderr)

    verbose = bool(getattr(args, "verbose", False))
    print(
        f"Kanban dispatcher running STANDALONE via --force (interval={args.interval}s, "
        f"pid={os.getpid()}). Ctrl-C to stop. NOTE: if a gateway is also running with "
        f"dispatch_in_gateway=true (default), you have two dispatchers racing for claims.",
        file=sys.stderr,
    )

    # Health telemetry: warn when every tick finds ready work but spawns
    # nothing (broken profile, PATH drift, missing venv, credential loss) —
    # the per-task breaker auto-blocks quietly, so the operator needs a signal.
    HEALTH_WINDOW = 6  # ticks (default 30s at interval=5)
    health_state = {"bad_ticks": 0, "last_warn_at": 0}
    from hermes_cli.kanban_dispatch_expected_holds import ExpectedHoldMonitor
    hold_monitor = ExpectedHoldMonitor()

    def _ready_queue_nonempty() -> bool:
        """Is there a ready+assigned+unclaimed task the dispatcher would spawn for?
        Control-plane lanes pulled via ``claim_task`` are correctly idle, not stuck."""
        try:
            with kbc.connect_closing() as conn:
                return kbd.has_spawnable_ready(conn)
        except Exception:
            return False

    def _on_tick(res):
        ready_pending = bool(res.skipped_unassigned) or _ready_queue_nonempty()
        # A full cap, or every ready card waiting on a known clock (quota reset, cooldown), is idle, not stuck [daveey]
        expected = hold_monitor.observe([("default", res)], time.time(),
            lambda level, message: print(f"[{_fmt_ts(int(time.time()))}] {logging.getLevelName(level)} {message}",
                                         file=sys.stderr, flush=True))
        if ready_pending and not res.spawned and not res.at_capacity and not kbd.all_clock_held([res]) and not expected:
            health_state["bad_ticks"] += 1
        else:
            health_state["bad_ticks"] = 0
        # Warn once per HEALTH_WINDOW bad ticks, at most every 5 minutes.
        if health_state["bad_ticks"] >= HEALTH_WINDOW:
            now = int(time.time())
            if now - health_state["last_warn_at"] >= 300:
                print(
                    f"[{_fmt_ts(now)}] WARN {kbd.stuck_warning(health_state['bad_ticks'], [res])}"
                    " See `hermes kanban list --status blocked` for recent spawn_failed tasks.",
                    file=sys.stderr, flush=True,
                )
                health_state["last_warn_at"] = now
        if not verbose:
            return
        did_work = (
            res.reclaimed or res.crashed or res.timed_out or res.promoted
            or res.spawned or res.auto_blocked or res.stale
        )
        if did_work:
            print(
                f"[{_fmt_ts(int(time.time()))}] reclaimed={res.reclaimed} "
                f"crashed={len(res.crashed)} timed_out={len(res.timed_out)} stale={len(res.stale)} "
                f"promoted={res.promoted} spawned={len(res.spawned)} "
                f"auto_blocked={len(res.auto_blocked)}",
                flush=True,
            )

    try:
        kbd.run_daemon(
            interval=args.interval,
            max_spawn=args.max,
            failure_limit=getattr(args, "failure_limit", kbd.DEFAULT_FAILURE_LIMIT),
            on_tick=_on_tick,
        )
    finally:
        if pidfile:
            try:
                Path(pidfile).unlink()
            except OSError:
                pass
    print("(dispatcher stopped)")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    """Live-stream task_events to the terminal."""
    kinds = {k.strip() for k in args.kinds.split(",") if k.strip()} if args.kinds else None
    print(f"Watching kanban events (initial board '{kb.get_current_board()}'). Ctrl-C to stop.", flush=True)
    # Seed cursor at the latest id so we don't replay history.
    with kbc.connect_closing() as conn:
        cursor = int(conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM task_events").fetchone()["m"])

    def tick():
        nonlocal cursor
        with kbc.connect_closing() as conn:
            rows = conn.execute(
                "SELECT e.id, e.task_id, e.kind, e.payload, e.created_at,        t.assignee, "
                "t.tenant FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id WHERE e.id > ? "
                "ORDER BY e.id ASC LIMIT 200",
                (cursor,),
            ).fetchall()
        for r in rows:
            cursor = max(cursor, int(r["id"]))
            if (kinds and r["kind"] not in kinds) or (args.assignee and r["assignee"] != args.assignee) \
                    or (args.tenant and r["tenant"] != args.tenant):
                continue
            try:
                payload = json.loads(r["payload"]) if r["payload"] else None
            except Exception:
                payload = None
            pl = f" {payload}" if payload else ""
            print(
                f"[{_fmt_ts(kb._to_epoch(r['created_at']))}] {r['task_id']:10s} "
                f"{r['kind']:18s} (@{r['assignee'] or '-'}){pl}",
                flush=True,
            )

    return _poll_loop(args.interval, tick)


_KEEP_PREFIX = "Scratch preserved: "


class _KeepLines(logging.Handler):
    """Print each ``Scratch preserved: `` record on stdout as one bare line (no logger prefix)."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if message.startswith(_KEEP_PREFIX):
                # One record is one line: a path may hold a line break, and a wrapper relays by line.
                print("?".join(message.splitlines()))
        except Exception:  # health: allow BLE001 -- logging.Handler contract: emit never raises into the caller
            self.handleError(record)


@contextlib.contextmanager
def _keep_lines_on_stdout():
    """While open, every kept scratch tree's reason is also a stdout line. [daveey]

    Subcommands log to files only, so a caller that captures this command's output (the Mac and
    worker GC wrappers) never saw why a tree was kept, and agent.log rotates within a day. File
    logging is untouched: this only adds a handler on ``hermes_cli`` for the workspace pass.
    """
    handler = _KeepLines()
    package = logging.getLogger("hermes_cli")
    package.addHandler(handler)
    try:
        yield
    finally:
        package.removeHandler(handler)


def _cmd_gc(args: argparse.Namespace) -> int:
    """Retry terminal workspace cleanup and remove old events and worker logs."""
    event_days = getattr(args, "event_retention_days", 30)
    log_days = getattr(args, "log_retention_days", 30)
    if event_days < 0 or log_days < 0:
        return _err("kanban gc: retention days must be >= 0 (0 disables that sweep)", 2)
    from hermes_cli.kanban_worktree_gc import gc_task_scratch, gc_task_worktrees, retention_days
    wt_days = getattr(args, "worktree_retention_days", None)
    wt_days = retention_days() if wt_days is None else wt_days
    if wt_days < 0:
        return _err("kanban gc: worktree retention days must be >= 0", 2)
    from hermes_cli.kanban_scratch_backoff import DEFAULT_BUDGET_S, Budget
    budget_s = getattr(args, "scratch_budget_seconds", None)
    # One budget for the whole workspace pass: the wrapper's kill covers it all.
    budget = Budget(DEFAULT_BUDGET_S if budget_s is None else budget_s)
    with _keep_lines_on_stdout():
        with kbc.connect_closing() as conn:
            removed_ws = gc_task_worktrees(conn, older_than_days=wt_days)
        with kbc.connect_closing() as conn:
            removed_ws += gc_task_scratch(conn, older_than_days=wt_days, budget=budget)

    removed_events = 0
    if event_days:
        with kbc.connect_closing() as conn:
            removed_events = kb.gc_events(conn, older_than_seconds=event_days * 24 * 3600)
    removed_logs = kb.gc_worker_logs(older_than_seconds=log_days * 24 * 3600) if log_days else 0
    print(f"GC complete: {removed_ws} workspace(s), "
          f"{removed_events} event row(s), {removed_logs} log file(s) removed")
    return 0


def _cmd_repair(args: argparse.Namespace) -> int:
    """Integrity check + narrow index-REINDEX auto-repair. Dispatched BEFORE
    the auto ``kb.init_db()`` (init refuses corrupt DBs). Exit 0 = healthy /
    repaired / no DB file, 1 = still corrupt."""
    try:
        report = kbc.repair_db()
    except Exception as exc:  # locked/busy probe, unexpected I/O
        return _err(f"kanban repair: {exc}")

    if getattr(args, "json", False):
        _print_json({
            "status": report.status,
            "db_path": str(report.db_path),
            "messages": report.messages,
            "post_repair_messages": report.post_repair_messages,
            "backup_path": str(report.backup_path) if report.backup_path else None,
            "reindexed": report.reindexed,
        }, ascii=True)
        return 0 if report.status in {"ok", "repaired", "missing"} else 1

    if report.status == "missing":
        print(f"No kanban DB at {report.db_path} — nothing to repair.")
        return 0
    if report.status == "ok":
        print(f"{report.db_path}: integrity_check ok — no repair needed.")
        return 0
    if report.status == "repaired":
        print(f"{report.db_path}: repaired.")
        print(f"  reindexed: {', '.join(report.reindexed)}")
        if report.backup_path:
            print(f"  pre-repair backup: {report.backup_path}")
        print("  integrity_check now ok.")
        return 0
    # still corrupt
    def err(line: str) -> None:
        print(line, file=sys.stderr)

    err(f"{report.db_path}: CORRUPT.")
    for line in (report.messages or [])[:10]:
        err(f"  {line}")
    if report.reindexed:
        err(f"  REINDEX ({', '.join(report.reindexed)}) attempted but integrity_check is still failing:")
        for line in (report.post_repair_messages or [])[:10]:
            err(f"    {line}")
    else:
        err("  Not an index-only failure — automatic REINDEX repair does not apply (fail-closed).")
    if report.backup_path:
        err(f"  corrupt copy quarantined at: {report.backup_path}")
    err(
        "  Not index-only: rebuild it in place with `hermes kanban recover` (salvages every readable row, "
        "fills from the newest healthy backup, writes a loss report). Never `mv` a rebuilt copy over the "
        "live kanban.db while any process has it open."
    )
    return 1


def _cmd_recover(args: argparse.Namespace) -> int:
    """Rebuild a board with table b-tree damage in place (``hermes_cli.kanban_db_recover``), in a
    fresh process so this CLI's own state can't strand anything. Exit 0 = recovered / healthy /
    dry run, 1 otherwise (busy, failed)."""
    from hermes_cli import kanban_db_recover as rec
    path = kb.kanban_db_path()
    extra = (["--dry-run"] if args.dry_run else []) + (["--no-fill"] if args.no_fill else [])
    result = rec.spawn_recovery(path, wait=True, extra_args=extra)
    if result is None:  # pragma: no cover - wait=True always returns a result
        return _err("kanban recover: no result from the recovery process")
    if getattr(args, "json", False):
        _print_json(result, ascii=True)
    else:
        print(f"{result.get('db_path')}: {result.get('status')} {result.get('reason') or ''}".rstrip())
        for name, loss in sorted((result.get("tables") or {}).items()):
            if loss.get("known_lost") or loss.get("possibly_lost") or loss.get("filled_from_backup"):
                print(f"  {name}: {loss.get('recovered')} read, {loss.get('filled_from_backup')} filled, "
                      f"{loss.get('known_lost')} known lost ({loss.get('possibly_lost')} possibly)")
        for key in ("report_path", "snapshot_path", "fill_source", "card_id", "sync"):
            if result.get(key):
                print(f"  {key}: {result[key]}")
    return 0 if result.get("status") in ("recovered", "healthy", "dry_run") else 1
