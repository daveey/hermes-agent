"""One hub card per quarantined containment receipt, so its owner hears of the hold. [daveey]

A quarantine (``kanban_sync_quarantine``) holds a card and its dependency descendants until an operator rules on
the Board's evidence; before this module its only signals were one log line and a count in ``sync status``.
:func:`notify` runs in each board's sync round, after the replica's holds are copied into the live file. Each
receipt not yet filed opens ONE hub card per host through the ``hermes-hub`` courier (the dead-watcher alert's
route, ``kanban_sync_watcher_alert``), idempotency key ``containment-quarantine:<board>:<receipt>:<gateway>:<ms>``
(``ms`` = the quarantine row's first-seen time, so a re-quarantine after a release files again), naming the board,
the held tasks, the receipt, the refusal kind and the exact release command. The hub gateway never files through
the courier (its card would wait on its own sync-gated board): it logs one ERROR per receipt and records it. A
failed courier call backs off :data:`RETRY_S`; state persists in ``kanban/.sync-quarantine-alert.json``. Never
raises.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

HUB = "hub"
KEY_PREFIX = "containment-quarantine"
RETRY_S = 600.0
_FILE = ".sync-quarantine-alert.json"


def key(board: str, entry: dict, gateway: str) -> str:
    """Per receipt, per host (``ident.gateway``, as the dead-watcher key) and per quarantine episode."""
    return f"{KEY_PREFIX}:{board}:{entry['receipt']}:{gateway}:{entry['quarantined_at']}"


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
        log.warning("kanban sync: cannot record the quarantine notice state at %s", target, exc_info=True)


def _body(entry: dict) -> str:
    from hermes_cli.kanban_sync_quarantine import release_command
    board, receipt = entry["board"], entry["receipt"]
    return "\n".join([
        f"Board **{board}**: this host quarantined containment receipt `{receipt}` because its evidence conflicts "
        f"({entry['kind']}: {entry['reason']}). The cards below are held here (no dispatch, pickup or push) until "
        "an operator rules on the Board's copy; every other card keeps syncing.",
        "",
        f"- Receipt: `{receipt}` (task `{entry['task']}`)",
        f"- Held tasks: {', '.join(f'`{t}`' for t in entry['held_tasks']) or '-'}",
        f"- Refusal kind: `{entry['kind']}`",
        "",
        "Do: 1) compare the Board's evidence for the receipt with this host's first observation "
        f"(`hermes kanban --board {board} sync quarantine --json`); 2) if the Board's copy is the legitimate one, "
        f"release it on that host: `{release_command(board, receipt)}` (records your acceptance and replays the "
        "withheld rows); 3) done when `hermes kanban sync status` shows no quarantined receipt.",
    ])


def notify(conn, board: str, *, runner: Optional[Callable[[list[str]], tuple[int, str, str]]] = None,
           hub_cli: Optional[str] = None, host: Optional[str] = None, now: Optional[float] = None) -> list[str]:
    """File one hub card per new quarantined receipt of ``board`` in ``conn``; the request ids filed. Never raises."""
    try:
        return _notify(conn, board, runner, hub_cli, host, time.time() if now is None else now)
    except Exception as exc:  # health: allow BLE001 -- a notice must never fail the sync round
        log.warning("kanban sync: quarantine notice for %s failed: %s", board, exc, exc_info=True)
        _write_state({**_read_state(), "last_attempt": time.time() if now is None else now,
                      "last_error": str(exc)[:500]})
        return []


def _notify(conn, board, runner, hub_cli, host, now) -> list[str]:
    from hermes_cli import kanban_sync_quarantine as quarantine
    entries = quarantine.entries(conn)
    state = _read_state()
    mine = f"{KEY_PREFIX}:{board}:"
    if not entries:  # nothing held: forget released receipts' keys, no identity lookup on a healthy round
        if any(k.startswith(mine) for k in state.get("filed") or {}):
            _write_state({**state, "filed": {k: v for k, v in state["filed"].items() if not k.startswith(mine)}})
        return []
    if host is None:
        from hermes_cli.kanban_sync_watcher_alert import _gateway
        host = _gateway()
    filed = dict(state.get("filed") or {})
    current = {key(board, e, host) for e in entries}
    stale = [k for k in filed if k.startswith(mine) and k not in current]
    todo = [e for e in entries if key(board, e, host) not in filed]
    if stale and not todo:  # released receipts: forget them (a re-quarantine has a new key anyway)
        _write_state({**state, "filed": {k: v for k, v in filed.items() if k not in stale}})
    if not todo:
        return []
    if state.get("last_error") and now - float(state.get("last_attempt") or 0) < RETRY_S:
        return []
    for k in stale:
        filed.pop(k, None)
    if host == HUB:
        for entry in todo:
            log.error("kanban sync: hub quarantined containment receipt %s on %s (tasks %s, %s); no hub card filed "
                      "(it would wait on this board's own sync); release with `%s`", entry["receipt"], board,
                      entry["held_tasks"], entry["kind"], quarantine.release_command(board, entry["receipt"]))
            filed[key(board, entry, host)] = {"hub": True, "seen_at": now}
        _write_state({**state, "filed": filed})
        return []
    from hermes_cli import kanban_capability_registry as reg
    from hermes_cli import kanban_placement as kp
    runner = runner or kp.hub_runner
    if runner is kp._default_runner:
        from hermes_state_guard import _in_test_context
        if _in_test_context():
            log.warning("kanban sync: quarantine hub card not filed from a test process")
            return []
    hub_cli = hub_cli or reg.hub_cli_path(kp.config().get("hub_cli"))
    out: list[str] = []
    for entry in todo:
        rid = key(board, entry, host)
        title = f"Containment receipt quarantined on {host}/{board}: {entry['task']}"
        argv = [hub_cli, "create-task", HUB, title, "--body", _body(entry), "--assignee", "default",
                "--origin-session", "", "--id", rid, "--option", f"idempotency_key={json.dumps(rid)}",
                "--option", "priority=900"]
        rc, stdout, err = runner(argv)
        if rc not in (0, 3):
            error = f"create-task exit {rc}: {(err or stdout or '')[-300:]}"
            _write_state({**state, "filed": filed, "last_attempt": now, "last_error": error})
            log.warning("kanban sync: quarantine hub card for %s not filed (%s); retry in %ds", rid, error, RETRY_S)
            return out
        filed[rid] = {"request_id": rid, "filed_at": now, "host": host}
        out.append(rid)
        log.error("kanban sync: opened a hub card for quarantined receipt %s on %s (request %s)",
                  entry["receipt"], board, rid)
    _write_state({k: v for k, v in {**state, "filed": filed, "last_attempt": now}.items() if k != "last_error"})
    return out
