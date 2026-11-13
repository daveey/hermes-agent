"""workspace_source v1 CLI (core PR-2; v4.1 R3-a, v4 §2.3).

* ``hermes kanban claim`` never takes a card with workspace_source custody or a manifest
  (``source_cli_claim_refused``): a CLI claim sends no ``source_protocol`` and would run outside custody.
* ``hermes kanban source-release <task> --to published|unchanged``: the operator frees a card whose source holder
  is decommissioned or disabled. It first verifies that GitHub's branch head equals ``manifest.commit`` (``git
  ls-remote``), then sends the admin-key-only ``/leases source_release`` with the manifest revision as CAS. The
  Board refuses while a lease is live.
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_cli import kanban_workspace_source as ws

ADMIN_KEY = "~/.config/hermes-mail/admin.key"
CLAIM_REFUSED = "source_cli_claim_refused"


def claim_refusal(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    row = conn.execute("SELECT workspace_source, workspace_custody FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    if row[0] is not None or ws.parse_custody(row[1]) is not None:
        return (f"{CLAIM_REFUSED}: {task_id} carries workspace_source custody; only the dispatcher may claim it "
                "(it attaches the run to custody). Let the dispatcher run it, or move it.")
    return None


def remote_head(url: str, branch: str, *, timeout: float = 30.0) -> Optional[str]:
    """``git ls-remote <url> <branch>`` (the manifest's branch ref), None when absent."""
    out = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "ls-remote", url, branch], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                         check=False)
    if out.returncode != 0:
        raise RuntimeError(f"git ls-remote {url} failed: {out.stderr.strip()[-200:]}")
    for line in out.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == branch:
            return sha
    return None


def _admin_hub() -> Any:
    from hermes_cli import kanban_sync_client as client
    key_path = Path(ADMIN_KEY).expanduser()
    if not key_path.is_file():
        raise PermissionError(f"no admin key at {ADMIN_KEY}: source-release runs on the hub (Board admin key)")
    ident = client.load_identity()
    key = key_path.read_text(encoding="utf-8-sig").strip()
    return client.Hub(client.Identity(url=ident.url, key=key, gateway=ident.gateway))


def source_release(conn: sqlite3.Connection, task_id: str, to_state: str, *, by: str,
                   head: Callable[[str, str], Optional[str]] = remote_head,
                   hub: Any = None) -> dict:
    if to_state not in ("published", "unchanged"):
        return {"ok": False, "reason_code": "invalid", "error": "--to must be published or unchanged"}
    row = conn.execute("SELECT workspace_source FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return {"ok": False, "reason_code": "not_found", "error": f"no such task: {task_id}"}
    try:
        manifest = ws.parse_manifest(row[0])
    except ws.ManifestInvalid as exc:
        return {"ok": False, "reason_code": "workspace_source_unavailable", "error": f"no valid manifest: {exc}"}
    try:
        seen = head(manifest.url, manifest.branch)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "reason_code": "remote_unreadable", "error": str(exc)}
    if seen != manifest.commit:
        return {"ok": False, "reason_code": "remote_drift" if seen else "remote_branch_missing",
                "error": f"{manifest.branch} on GitHub is {seen or 'missing'}, the manifest pins {manifest.commit}; "
                         "the branch must match before custody can be released"}
    from hermes_cli import kanban_db_sync as sync
    try:
        hub = hub or _admin_hub()
        res = hub.leases(sync._state(conn)["board_id"], {
            "action": "source_release", "task": task_id, "to_state": to_state,
            "expected_revision": manifest.manifest_revision, "by": by[:100], "client_op_id": sync.uuid7()})
    except Exception as exc:  # health: allow BLE001 -- an operator CLI reports every failure
        return {"ok": False, "reason_code": "board_unavailable", "error": str(exc)[:300]}
    if not isinstance(res, dict) or res.get("status") != "applied":
        res = res if isinstance(res, dict) else {}
        return {"ok": False, "reason_code": str(res.get("reason") or "rejected"), "error": str(res.get("detail") or "")}
    return {"ok": True, "task": task_id, "to_state": to_state, "manifest_revision": manifest.manifest_revision,
            "commit": manifest.commit}


def cmd_source_release(args: argparse.Namespace) -> int:
    import json

    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.profiles import current_profile_name
    with kbc.connect_closing() as conn:
        res = source_release(conn, args.task_id, args.to, by=getattr(args, "by", None) or current_profile_name("user")
                             or "operator")
    if getattr(args, "json", False):
        print(json.dumps(res))
    elif res["ok"]:
        print(f"Released workspace_source custody of {res['task']} -> {res['to_state']} "
              f"(revision {res['manifest_revision']}, {res['commit'][:12]})")
    else:
        print(f"source-release refused ({res['reason_code']}): {res['error']}", file=sys.stderr)
    return 0 if res["ok"] else 1
