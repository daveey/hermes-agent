"""One hub card when this host's kanban sync watcher has been crashed for 5 min. [daveey]

Incident (2026-11-20): ContainmentRefused killed ``kanban_sync_watcher`` at 04:42:46 UTC; the supervisor
gave up at 04:43:21. The replica-stale alarm logged at 05:02 via the local outbox, which could not push
because sync was the thing that died. SchemaAhead already files through the ``hermes-hub`` courier;
watcher crash did not.

:func:`check` runs on the dispatcher tick (outside the dead loop). A ``.sync-state.json`` crash record
(``kanban sync loop crashed:``) older than :data:`DOWN_S` opens one hub card; while that card is open a
later crash comments. Graceful ``STOPPED`` and SchemaAhead give-ups are other alarms. Never raises.
"""
from __future__ import annotations

import json
import logging
import socket
import time
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

HUB = "hub"
KEY_PREFIX = "kanban-sync-watcher-dead"
DOWN_S = 300.0
CRASH_PREFIX = "kanban sync loop crashed:"
_FILE = ".sync-watcher-alert.json"
_CLOSED = ("done", "archived", "failed", "cancelled")
_FRESH_S = 6 * 3600


def _state_path() -> Path:
    from hermes_cli import kanban_db as kb
    return kb.kanban_home() / "kanban" / _FILE


def _read_state() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(state: dict) -> None:
    target = _state_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{_FILE}.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(target)
    except OSError:
        log.warning("kanban sync: cannot record the dead-watcher alert state at %s", target, exc_info=True)


def incident_key(host: str, since: float) -> str:
    return f"{KEY_PREFIX}:{host}:{int(since)}"


def _body(host: str, why: str, since: float, info: dict) -> str:
    when = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(since))
    return "\n".join([
        f"The Kanban sync watcher on **{host}** has been down since {when} ({why}). The gateway supervisor "
        "stopped restarting it, so this host neither pushes nor pulls; dispatch stays on "
        "`claim_deferred(sync_first_round)`. The replica-stale alarm files through the local outbox, which "
        "cannot reach the Board while sync is dead — this card is the courier path that still can.",
        "",
        f"- Crash: {why}",
        f"- Installed core: `{info.get('installed') or 'unknown'}` at `{info.get('installed_sha') or 'unknown'}`",
        f"- origin/main: `{info.get('origin_main_sha') or 'unknown'}`",
        "",
        "Do: 1) `hermes kanban sync status` on that host; 2) recover with a core that includes the crash fix "
        "(or restart the gateway once the cause is gone); 3) done when status shows `running: yes` and the "
        "dispatcher has spawned a card. Precedent: t_01a1f772bf13a473.",
    ])


def _replica_rows(hub_cli: str, runner) -> list[dict]:
    rc, out, _err = runner([hub_cli, "tasks", "--gw", HUB, "--json", "--all"])
    if rc != 0:
        return []
    try:
        rows = json.loads(out or "[]")
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict)]


def _open_card(host: str, state: dict, hub_cli: str, runner, now: float) -> Optional[str]:
    prefix = f"{KEY_PREFIX}:{host}:"
    rows = _replica_rows(hub_cli, runner)
    for row in rows:
        if (str(row.get("idempotency_key") or "").startswith(prefix)
                and str(row.get("status") or "") not in _CLOSED):
            return str(row.get("id"))
    tid = state.get("task_id")
    if not tid and state.get("request_id"):
        try:
            from hermes_cli import kanban_placement as kp
            tid = kp.remote_task_id(state["request_id"])
        except Exception:  # health: allow BLE001 -- a stale courier db only means "not known yet"
            tid = None
    if tid:
        seen = next((r for r in rows if r.get("id") == tid), None)
        if seen is not None:
            return str(tid) if str(seen.get("status") or "") not in _CLOSED else None
        if now - float(state.get("filed_at") or 0) < _FRESH_S:
            return str(tid)
    return None


def report(host: str, why: str, since: float, *, runner: Optional[Callable[[list[str]], tuple[int, str, str]]] = None,
           hub_cli: Optional[str] = None, info: Optional[dict] = None, now: Optional[float] = None) -> Optional[str]:
    """Open (or comment) this host's dead-watcher card on the hub. Never raises."""
    try:
        return _report(host, why, since, runner, hub_cli, info, time.time() if now is None else now)
    except Exception:  # health: allow BLE001 -- an alert must never break the dispatcher tick
        log.warning("kanban sync: could not open the dead-watcher card on the hub", exc_info=True)
        return None


def _report(host, why, since, runner, hub_cli, info, now) -> Optional[str]:
    from hermes_cli import kanban_capability_registry as reg
    from hermes_cli import kanban_placement as kp
    from hermes_cli.kanban_sync_schema_alert import cores
    runner = runner or kp.hub_runner
    if runner is kp._default_runner:
        from hermes_state_guard import _in_test_context
        if _in_test_context():
            log.warning("kanban sync: dead-watcher hub card not filed from a test process")
            return None
    hub_cli = hub_cli or reg.hub_cli_path(kp.config().get("hub_cli"))
    key = incident_key(host, since)
    state = _read_state()
    if state.get("key") == key:
        return None
    info = info if info is not None else cores()
    open_tid = _open_card(host, state, hub_cli, runner, now)
    if open_tid:
        rid = f"{key}:comment"
        text = (f"Again on {host} at {time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(since))}: {why} "
                f"(installed core {info.get('installed_sha') or 'unknown'})")
        argv = [hub_cli, "request", HUB, "tasks", f"default/default/{open_tid}", "comment", "--arg",
                f"text={text[:7900]}", "--id", rid]
        rc, out, err = runner(argv)
        if rc not in (0, 3):
            log.warning("kanban sync: could not comment the dead-watcher card %s on the hub (exit %s): %s",
                        open_tid, rc, (err or out)[-300:])
            return None
        _write_state({**state, "key": key, "task_id": open_tid, "commented_at": now})
        log.error("kanban sync: watcher still down; commented hub card %s (%s)", open_tid, rid)
        return rid
    rid = key
    title = f"Kanban sync watcher dead on {host} for 5+ min"
    argv = [hub_cli, "create-task", HUB, title, "--body", _body(host, why, since, info), "--assignee", "default",
            "--origin-session", "", "--id", rid, "--option", f"idempotency_key={json.dumps(key)}",
            "--option", "priority=900"]
    rc, out, err = runner(argv)
    if rc not in (0, 3):
        log.warning("kanban sync: could not open the dead-watcher card on the hub (exit %s): %s",
                    rc, (err or out)[-300:])
        return None
    _write_state({"key": key, "request_id": rid, "task_id": None, "filed_at": now})
    log.error("kanban sync: opened a hub card for a dead sync watcher (request %s)", rid)
    return rid


def check(*, now: Optional[float] = None, runner: Optional[Callable[[list[str]], tuple[int, str, str]]] = None,
          hub_cli: Optional[str] = None, host: Optional[str] = None, info: Optional[dict[str, Any]] = None
          ) -> Optional[str]:
    """File if this host's sync loop has been crashed for :data:`DOWN_S`. Never raises."""
    try:
        return _check(now, runner, hub_cli, host, info)
    except Exception:  # health: allow BLE001 -- an alert must never break the dispatcher tick
        log.warning("kanban sync: dead-watcher check failed", exc_info=True)
        return None


def _check(now, runner, hub_cli, host, info) -> Optional[str]:
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_runstate as runstate
    from hermes_cli.kanban_sync_schema_alert import _updating
    if not sync.sync_enabled() or _updating():
        return None
    record = runstate._read(runstate.path()) or {}
    if record.get("running") is not False:
        return None
    why = str(record.get("reason") or "")
    if not why.startswith(CRASH_PREFIX):
        return None
    since = float(record.get("since") or 0)
    wall = time.time() if now is None else now
    if since <= 0 or wall - since < DOWN_S:
        return None
    return report(host or socket.gethostname(), why, since, runner=runner, hub_cli=hub_cli, info=info, now=wall)
