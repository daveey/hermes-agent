"""``hermes kanban wait-check``: the one supported cross-gateway wait probe.

Exit 0 = condition met, 1 = not yet, 2 = cannot tell (unknown card, unreachable gateway,
unreadable DB). The wake-check scheduler treats 2 as broken (3 in a row wakes the card),
never as "not yet". Arguments travel as argv (``shlex.join`` for ssh), so SQL/quoting
never gets re-split by a remote shell.

    hermes kanban wait-check task t_x [--gateway hub] [--board SLUG] [--until done,blocked,archived]
    hermes kanban wait-check board-empty SLUG [--gateway hub]
"""
from __future__ import annotations

import os
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Optional

DEFAULT_UNTIL = ("done", "blocked", "archived")
GATEWAY_SSH = {"hub": "ubuntu@100.104.201.88"}
SSH_TIMEOUT = 25


def _db(board: Optional[str]) -> Path:
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    if os.environ.get("HERMES_KANBAN_DB") and board in (None, "default"):
        return Path(os.environ["HERMES_KANBAN_DB"])
    return home / "kanban.db" if board in (None, "default") else home / "kanban" / "boards" / board / "kanban.db"


def _query(path: Path, sql: str, args=()):
    last: Optional[Exception] = None
    for _ in range(3):  # WAL/lock flakes are transient
        try:
            c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=20)
            try:
                return c.execute(sql, args).fetchall()
            finally:
                c.close()
        except sqlite3.OperationalError as exc:
            last = exc
    raise last  # type: ignore[misc]


def check_local(kind: str, target: str, *, board: Optional[str] = None, until=DEFAULT_UNTIL) -> tuple[int, str]:
    try:
        if kind == "task":
            rows = _query(_db(board), "select status from tasks where id=?", (target,))
            if not rows:
                return 2, f"error: {target} not found"
            return (0 if rows[0][0] in set(until) else 1), f"{target} {rows[0][0]}"
        if kind == "board-empty":
            n = _query(_db(target), "select count(*) from tasks where status not in ('done','archived')")[0][0]
            return (0 if n == 0 else 1), f"{target} open={n}"
    except Exception as exc:  # any failure is "cannot tell", never "not yet"
        return 2, f"error: {exc}"
    return 2, f"error: unknown check {kind!r}"


def check_remote(gateway: str, kind: str, target: str, *, board: Optional[str] = None, until=DEFAULT_UNTIL) -> tuple[int, str]:
    inner = ["hermes", "kanban", "wait-check", kind, target, "--until", ",".join(until)]
    if board:
        inner += ["--board", board]
    host = GATEWAY_SSH.get(gateway, gateway)
    remote = "export PATH=$HOME/.local/bin:$HOME/.hermes/bin:$PATH; " + shlex.join(inner)
    try:
        p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_TIMEOUT}", host, remote],
                           capture_output=True, text=True, timeout=SSH_TIMEOUT + 15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 2, f"error: ssh {host}: {exc}"
    out = (p.stdout + p.stderr).strip()
    if p.returncode in (0, 1):
        return p.returncode, out
    return 2, f"error: remote wait-check on {host} exited {p.returncode}: {out[-300:]}"


def run(args) -> int:
    until = tuple(u for u in (args.until or "").split(",") if u) or DEFAULT_UNTIL
    gw = getattr(args, "gateway", None)
    if gw in (None, "", "local"):
        rc, msg = check_local(args.check, args.target, board=args.board, until=until)
    else:
        rc, msg = check_remote(gw, args.check, args.target, board=args.board, until=until)
    print(msg, file=sys.stderr if rc == 2 else sys.stdout)
    return rc
