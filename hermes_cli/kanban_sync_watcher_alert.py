"""One hub card when this host's kanban sync watcher has been crashed for 5 min. [daveey]

Incident (2026-11-20): ContainmentRefused killed ``kanban_sync_watcher`` at 04:42:46 UTC; the supervisor
gave up at 04:43:21. The replica-stale alarm logged at 05:02 via the local outbox, which could not push
because sync was the thing that died. SchemaAhead already files through the ``hermes-hub`` courier;
watcher crash did not.

:func:`check` runs on the dispatcher tick (outside the dead loop). A ``.sync-state.json`` crash record
(``kanban sync loop crashed:``) with no sync loop running now opens one hub card once the incident is
:data:`DOWN_S` old; while that card is open a later incident comments. Graceful ``STOPPED`` and SchemaAhead
give-ups are other alarms. Never raises.

The incident is this module's own ``first_seen_dead`` (the first crash record's ``since``), not the crash
text: a supervisor respawn that crashes again with another message is the same incident. A completed round
(the record cleared) ends it. A failed courier call backs off :data:`RETRY_S`. The hub gateway never files
through the courier: its card would wait on the hub's own sync-gated board, so it logs one ERROR per
incident and records it in the state file instead.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

HUB = "hub"
KEY_PREFIX = "kanban-sync-watcher-dead"
DOWN_S = 300.0
RETRY_S = 600.0
CRASH_PREFIX = "kanban sync loop crashed:"
_FILE = ".sync-watcher-alert.json"
_CLOSED = ("done", "archived", "failed", "cancelled")
_FRESH_S = 6 * 3600
# Per-incident fields, dropped once a completed round clears the crash record. The card ids stay so a
# quick next incident comments on a still-open card.
_INCIDENT = ("first_seen_dead", "last_attempt", "last_error", "hub_incident")


class CourierFailed(RuntimeError):
    """A ``hermes-hub tasks``/``create-task``/comment call failed; the next try waits :data:`RETRY_S`."""


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


def _body(host: str, why: str, since: float, info: dict, now: float) -> str:
    when = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(since))
    minutes = max(0, int((now - since) // 60))
    return "\n".join([
        f"The Kanban sync loop on **{host}** crashed at {when} ({why}) and has not completed a round for "
        f"{minutes} min, so this host neither pushes nor pulls; dispatch stays on "
        "`claim_deferred(sync_first_round)`. The replica-stale alarm files through the local outbox, which "
        "cannot reach the Board while sync is dead — this card is the courier path that still can.",
        "",
        f"- Crash: {why}",
        f"- Installed core: `{info.get('installed') or 'unknown'}` at `{info.get('installed_sha') or 'unknown'}`",
        f"- origin/main: `{info.get('origin_main_sha') or 'unknown'}`",
        "",
        "Do: 1) `hermes kanban sync status` on that host; 2) recover with a core that includes the crash fix "
        "(the supervisor retries the loop, or restart the gateway once the cause is gone); "
        "3) done when status shows `running: yes` and the dispatcher has spawned a card. "
        "Precedent: t_01a1f772bf13a473.",
    ])


def _replica_rows(hub_cli: str, runner) -> list[dict]:
    rc, out, err = runner([hub_cli, "tasks", "--gw", HUB, "--json", "--all"])
    if rc != 0:
        raise CourierFailed(f"tasks exit {rc}: {(err or out or '')[-300:]}")
    try:
        rows = json.loads(out or "[]")
    except ValueError as exc:
        raise CourierFailed(f"tasks returned non-JSON: {exc}") from exc
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
    elif (state.get("request_id") and now - float(state.get("filed_at") or 0) < _FRESH_S
          and not any(r.get("idempotency_key") == state["request_id"] for r in rows)):
        # The first create is queued but neither the courier nor the replica knows its card yet. That is
        # "unknown", not "no open card": a second create would carry a different request id and could
        # file a duplicate card once delivery catches up.
        raise CourierFailed(f"create request {state['request_id']} is not resolved yet; later incident deferred")
    return None


def report(host: str, why: str, since: float, *, runner: Optional[Callable[[list[str]], tuple[int, str, str]]] = None,
           hub_cli: Optional[str] = None, info: Optional[dict] = None, now: Optional[float] = None) -> Optional[str]:
    """Open (or comment) this host's dead-watcher card on the hub. Never raises; a failure is recorded
    (``last_attempt``/``last_error``) so :func:`check` waits :data:`RETRY_S` before the next try."""
    wall = time.time() if now is None else now
    try:
        return _report(host, why, since, runner, hub_cli, info, wall)
    except Exception as exc:  # health: allow BLE001 -- an alert must never break the dispatcher tick
        log.warning("kanban sync: could not open the dead-watcher card on the hub: %s", exc,
                    exc_info=not isinstance(exc, CourierFailed))
        _write_state({**_read_state(), "last_attempt": wall, "last_error": str(exc)[:500]})
        return None


def _report(host, why, since, runner, hub_cli, info, now) -> Optional[str]:
    key = incident_key(host, since)
    state = _read_state()
    if state.get("key") == key:
        return None
    if host == HUB:
        # The hub's card would sit on its own sync-gated board, which is the thing that is dead. Known gap:
        # the hub's dead sync needs a non-Kanban route.
        _write_state({**state, "key": key, "hub_incident": {"since": since, "why": why, "seen_at": now}})
        log.error("kanban sync: the hub's own sync watcher has been down since %s (%s); no hub card filed "
                  "(it would wait on this dead sync), recover the hub gateway by hand",
                  time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(since)), why)
        return None
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
    info = info if info is not None else cores()
    state = {**state, "last_attempt": now}
    state.pop("last_error", None)
    open_tid = _open_card(host, state, hub_cli, runner, now)
    if open_tid:
        rid = f"{key}:comment"
        text = (f"Again on {host} at {time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(since))}: {why} "
                f"(installed core {info.get('installed_sha') or 'unknown'})")
        argv = [hub_cli, "request", HUB, "tasks", f"default/default/{open_tid}", "comment", "--arg",
                f"text={text[:7900]}", "--id", rid]
        rc, out, err = runner(argv)
        if rc not in (0, 3):
            raise CourierFailed(f"comment on {open_tid} exit {rc}: {(err or out or '')[-300:]}")
        _write_state({**state, "key": key, "task_id": open_tid, "commented_at": now})
        log.error("kanban sync: watcher still down; commented hub card %s (%s)", open_tid, rid)
        return rid
    rid = key
    title = f"Kanban sync watcher dead on {host} for 5+ min"
    argv = [hub_cli, "create-task", HUB, title, "--body", _body(host, why, since, info, now), "--assignee", "default",
            "--origin-session", "", "--id", rid, "--option", f"idempotency_key={json.dumps(key)}",
            "--option", "priority=900"]
    rc, out, err = runner(argv)
    if rc not in (0, 3):
        raise CourierFailed(f"create-task exit {rc}: {(err or out or '')[-300:]}")
    _write_state({**state, "key": key, "request_id": rid, "task_id": None, "filed_at": now})
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


def loop_running() -> bool:
    """A sync loop runs now: this process's (the dispatcher and the sync watcher share the gateway process),
    or another live process holds the host ``.sync.lock``. A crashed loop releases that lock."""
    from hermes_cli.kanban_sync_claims import loop_running_here
    if loop_running_here():
        return True
    try:
        from gateway.kanban_singleton import holder_info
        from hermes_cli import kanban_db as kb
        return holder_info(kb.kanban_home() / "kanban" / ".sync.lock") is not None
    except Exception:  # health: allow BLE001 -- an unreadable lock table is "no other loop known"
        return False


def _gateway() -> str:
    """This gateway's Board name (``ident.gateway``), as the SchemaAhead alarm labels its cards."""
    from hermes_cli import kanban_sync_client as ksc
    gateway = ksc.load_identity().gateway
    if not gateway:
        raise ValueError("kanban.sync.gateway is not set")
    return gateway


def _check(now, runner, hub_cli, host, info) -> Optional[str]:
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_runstate as runstate
    if not sync.sync_enabled():
        return None
    wall = time.time() if now is None else now
    record = runstate._read(runstate.path())
    state = _read_state()
    if record is None:  # a completed round cleared the record: any incident is over
        if any(k in state for k in _INCIDENT):
            _write_state({k: v for k, v in state.items() if k not in _INCIDENT})
        return None
    why = str(record.get("reason") or "")
    if record.get("running") is not False or not why.startswith(CRASH_PREFIX):
        return None
    if loop_running():  # a respawned loop whose first round has not completed yet
        return None
    first = state.get("first_seen_dead")
    if not isinstance(first, (int, float)) or first <= 0:
        since = float(record.get("since") or 0)
        first = since if 0 < since <= wall else wall
        state = {**state, "first_seen_dead": first}
        _write_state(state)
    if wall - first < DOWN_S:
        return None
    from hermes_cli.kanban_sync_schema_alert import _updating
    if _updating():
        return None
    if state.get("last_error") and wall - float(state.get("last_attempt") or 0) < RETRY_S:
        return None
    if host is None:
        try:
            host = _gateway()
        except Exception as exc:  # health: allow BLE001 -- recorded below; retried after the backoff
            log.warning("kanban sync: dead-watcher alert has no gateway identity: %s", exc)
            _write_state({**state, "last_attempt": wall, "last_error": f"no gateway identity: {exc}"[:500]})
            return None
    return report(host, why, first, runner=runner, hub_cli=hub_cli, info=info, now=wall)
