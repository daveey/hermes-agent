"""Count ``hermes update`` deferrals per gateway; hand a repeat to one durable Kanban owner.

The pre-swap pause (``update_cmd_posix_pause``) refuses to move the checkout while a gateway
cannot take an idle pause. One refusal is ordinary (a busy gateway). The same gateway refusing
update after update is a wall nothing retries through: on 2026-11-11 a pinned Ask Eve gateway
answered ``unsupported`` to eleven hub updates in a row and the only trace was a log line per
attempt. From the third consecutive deferral naming the same gateway, this files (or keeps) one
card for an agent to clear it. [daveey]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)
AUTHOR = "update-pause"
THRESHOLD = 3
#: While the owner card stays open, repeat the evidence as a comment every this many deferrals.
REFRESH_EVERY = 12
_RESULT = "__HERMES_UPDATE_PAUSE_DEFERRAL_TASK__="
_TIMEOUT_S = 60


def state_path() -> Path:
    from hermes_cli.update_pause_record import _install_key
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root() / f"update-pause-deferrals.{_install_key()}.json"


def _read() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8-sig"))
        units = data.get("units") if isinstance(data, dict) else None
        return units if isinstance(units, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(units: dict) -> None:
    path = state_path()
    if not units:
        path.unlink(missing_ok=True)
        return
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"units": units}, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def note_pause_ok() -> None:
    """The pause went through (or had nothing to stop): no gateway is on a deferral streak."""
    try:
        _write({})
    except OSError as exc:
        log.debug("Could not clear the pause deferral streaks: %s", exc)


def note_deferral(names, reason: str) -> list[str]:
    """Record that this update was deferred by the gateways *names*; the owner card ids filed.

    Only CONSECUTIVE deferrals count: a gateway this deferral does not name starts over. Best
    effort throughout: a streak that cannot be written or a card that cannot be filed never
    changes the update's own verdict."""
    names = [n for n in dict.fromkeys(names or ()) if isinstance(n, str) and n]
    filed: list[str] = []
    try:
        before, now = _read(), datetime.now(UTC).isoformat(timespec="seconds")
        units = {}
        for name in names:
            prior = before.get(name)
            prior = prior if isinstance(prior, dict) else {}
            units[name] = {"count": int(prior.get("count") or 0) + 1, "first_at": prior.get("first_at") or now,
                           "last_at": now, "reason": str(reason)[:500]}
        _write(units)
    except (OSError, ValueError, TypeError) as exc:
        log.warning("Could not record the update pause deferral: %s", exc)
        return filed
    for name, entry in units.items():
        if entry["count"] < THRESHOLD:
            continue
        task_id = report(name, entry)
        owner = task_id or "NOT FILED (see agent.log)"
        print(f"  ⚠ {name} has deferred {entry['count']} updates in a row since {entry['first_at']}. "
              f"Repair owner: {owner}")
        if task_id:
            filed.append(task_id)
    return filed


def report(unit: str, entry: dict) -> str | None:
    """File (or keep) the owner card for *unit* in a fresh interpreter, like
    ``update_upstream_conflict.report``: the updater's ``sys.modules`` must not be the kanban
    modules' import environment."""
    from hermes_cli._launchers import runtime_command
    from hermes_cli._startup_fast import project_root_str
    from hermes_constants import get_hermes_home

    root = Path(project_root_str())
    args = ["--unit", unit, "--count", str(entry["count"]), "--first-at", str(entry["first_at"]),
            "--reason", str(entry["reason"])]
    try:
        proc = subprocess.run(
            runtime_command(root, args, module="hermes_cli.update_pause_deferrals", home=get_hermes_home()),
            cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=_TIMEOUT_S, check=False)
        found = re.findall(rf"^{re.escape(_RESULT)}(t_\w+)$", proc.stdout or "", re.MULTILINE)
        if proc.returncode == 0 and found:
            return found[-1]
        detail = f"exit {proc.returncode}: " + (proc.stderr or proc.stdout or "")[-2000:]
    except (OSError, subprocess.SubprocessError) as exc:
        detail = f"{type(exc).__name__}: {exc}"
    log.warning("Could not file the update pause deferral card for %s (%s)", unit, detail)
    return None


def main(argv: list[str] | None = None) -> int:
    """Child entry point: file (or keep) the owner card and print its id on a marker line."""
    parser = argparse.ArgumentParser(prog="hermes_cli.update_pause_deferrals")
    parser.add_argument("--unit", required=True)
    parser.add_argument("--count", required=True, type=int)
    parser.add_argument("--first-at", required=True)
    parser.add_argument("--reason", required=True)
    ns = parser.parse_args(argv)
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing() as conn:
        task_id = _report(conn, ns.unit, ns.count, ns.first_at, ns.reason)
    print(f"{_RESULT}{task_id}", flush=True)
    return 0


def _report(conn, unit: str, count: int, first_at: str, reason: str) -> str:
    import socket

    from hermes_cli import kanban_db as kb
    from hermes_cli.update_pause_record import install_root

    root = install_root()
    key = "update-pause-deferred:" + hashlib.sha256(f"{root}\0{unit}".encode()).hexdigest()[:24]
    evidence = (f"`{unit}` has deferred {count} `hermes update` runs in a row since {first_at}. "
                f"Latest refusal: {reason}")
    # Serialize lookup + create so overlapping unattended updates cannot create two owners.
    with kb.write_txn(conn, allow_nested=True):
        row = conn.execute(
            "SELECT id, status FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1", (key,),
        ).fetchone()
        if row and row[1] not in {"done", "failed", "cancelled"}:
            if count % REFRESH_EVERY == 0 and not conn.execute(
                "SELECT 1 FROM task_comments WHERE task_id = ? AND author = ? AND body = ?",
                (row[0], AUTHOR, evidence),
            ).fetchone():
                kb.add_comment(conn, row[0], AUTHOR, evidence)
            return row[0]
        # A finished owner must not swallow a streak that outlived it; release its stable key atomically.
        if row:
            conn.execute("UPDATE tasks SET idempotency_key = NULL WHERE id = ?", (row[0],))
        return kb.create_task(
            conn, title=f"hermes update keeps deferring on gateway {unit}", assignee="default",
            created_by=AUTHOR, idempotency_key=key, workspace_kind="scratch", requires=["terminal", "file"],
            completion_contract="local-only",
            body=f"On `{socket.gethostname()}` (checkout `{root}`) the pre-swap gateway pause refused the "
                 f"update {count} times in a row, each time naming the same gateway. The checkout has not "
                 f"moved since {first_at} and every later update defers the same way until this is cleared.\n\n"
                 f"{evidence}\n\n"
                 "Find why that gateway cannot take an idle pause, with evidence (its unit definition and "
                 "drop-ins, the code root it runs, its `gateway_state.json`, the update log), then remove the "
                 "cause:\n"
                 "- it runs this checkout on code older than the idle pause: restart it when idle;\n"
                 "- it is pinned to its own checkout (`ExecStart`/`PYTHONPATH` elsewhere) yet was not left "
                 "alone: fix the detection (`hermes_cli/update_fleet_scope.py` `gateway_foreign_code_root`), "
                 "since a restart only brings it back on its pin;\n"
                 "- it is busy at every attempt: find the turn or worker that never finishes.\n\n"
                 "Acceptance: the next `hermes update` on this host passes the pre-swap pause (no "
                 "`Update deferred` line naming this gateway) and the checkout HEAD moves. Do not stop a "
                 "gateway that has work in flight.",
        )


if __name__ == "__main__":
    sys.exit(main())
