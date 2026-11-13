"""One hub Kanban card per host incident when a gateway gives up its Board sync role over SchemaAhead. [daveey]

Incident (2026-11-12): David-MBP's gateway gave up the sync role at 23:53:44 PST and daveey-mini's at 23:58:38 PST
(``kanban_sync_client.SchemaAhead``: a newer core had migrated the live board). Nothing turned that into a card:
a chat found it 12 min later through a refused ``kanban_create``. Each host stayed off the Board until its own core
update.

:func:`report` runs where the loop gives the role up (``run_forever``: at start, and mid-run). The host's Board
sync is down then, so the card goes to the hub through the ``hermes-hub`` courier (``create-task hub``), the same
route a placement relay takes. One incident (host + the sync-state ``since``) files once; while that card is open,
a later incident comments on it instead of opening another. The card names the columns, the installed core and the
newest core seen launching against this home. No Inbox.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

HUB = "hub"
KEY_PREFIX = "kanban-sync-schema-ahead"
_FILE = ".sync-schema-alert.json"
_CLOSED = ("done", "archived", "failed", "cancelled")
# A card filed this recently but not yet in the replica (the hub client syncs it in a minute or so) still counts
# as open, so a quick second incident comments instead of opening a duplicate.
_FRESH_S = 6 * 3600
_LAUNCH_RE = re.compile(r"Checkout changed since last launch \([^)]*-> git:HEAD:([0-9a-f]{7,40})\)")


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
        log.warning("kanban sync: cannot record the SchemaAhead alert state at %s", target, exc_info=True)


def incident_key(host: str, since: float) -> str:
    return f"{KEY_PREFIX}:{host}:{int(since)}"


def cores(home: Optional[Path] = None) -> dict[str, Optional[str]]:
    """``{installed, installed_sha, newest_sha, origin_main_sha}`` for this home, each best effort.

    ``newest_sha`` is the last checkout other than the installed one that agent.log says launched against this
    home ("Checkout changed since last launch (... -> git:HEAD:<sha>)"): usually the core that migrated the board.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.boot_bootstrap import read_git_head
    from hermes_cli.kanban_schema_owner import installed_core
    home = home or kb.kanban_home()
    installed = installed_core(home)
    installed_sha = read_git_head(installed) if installed else None
    newest = None
    try:
        with open(home / "logs" / "agent.log", "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 4_000_000))
            tail = fh.read().decode("utf-8", "replace")
        for sha in _LAUNCH_RE.findall(tail):
            if not (installed_sha and installed_sha.startswith(sha)):
                newest = sha
    except OSError:
        pass
    origin_main = None
    if installed:
        from hermes_cli.boot_bootstrap import _git_binary
        import subprocess
        git = _git_binary()
        if git:
            try:
                out = subprocess.run([git, "-C", str(installed), "rev-parse", "origin/main"], capture_output=True,
                                     text=True, encoding="utf-8", errors="replace", timeout=10, check=False)
                origin_main = out.stdout.strip() if out.returncode == 0 else None
            except (OSError, subprocess.SubprocessError):
                origin_main = None
    return {"installed": str(installed) if installed else None, "installed_sha": installed_sha,
            "newest_sha": newest, "origin_main_sha": origin_main}


def _body(host: str, why: str, since: float, info: dict) -> str:
    when = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(since))
    return "\n".join([
        f"The Kanban gateway on **{host}** gave up its Board sync role at {when}: its board holds columns its "
        "core does not declare (SchemaAhead), so this host neither pushes nor pulls cards until it runs a core "
        "that declares them. Cards on its board with another machine's `runs_on` cannot dispatch, and "
        "`kanban_create` with a pool `runs_on` there is refused (`sync_not_running`).",
        "",
        f"- Refusal: {why}",
        f"- Installed core: `{info.get('installed') or 'unknown'}` at `{info.get('installed_sha') or 'unknown'}`",
        f"- Newest core seen launching against this home (agent.log): `{info.get('newest_sha') or 'none logged'}`",
        f"- origin/main in the installed checkout: `{info.get('origin_main_sha') or 'unknown'}`",
        "",
        "Do: 1) on that host, find what launched the newer core against the live home (agent.log \"Checkout "
        "changed since last launch\" and \"home maintenance: code changed\" lines, hermes-sync.log, "
        "`cache/core-update/build.log`); since core main has `kanban_schema_owner`, a non-installed checkout opens "
        "the board read-only instead of migrating it, so name the core that did; 2) recovery is that host's core "
        "update past the newest core (`hermes-core-wt eta`/`deliver`, or `hermes-sync core-expedite <sha>`); "
        "3) done when `hermes kanban sync status` on that host shows `running: yes`. Never drop the columns.",
    ])


def _replica_rows(hub_cli: str, runner) -> list[dict]:
    """The hub replica's cards, closed ones included (``--all``), so a recently filed card that already closed
    is not mistaken for one still waiting to appear in the replica."""
    rc, out, _err = runner([hub_cli, "tasks", "--gw", HUB, "--json", "--all"])
    if rc != 0:
        return []
    try:
        rows = json.loads(out or "[]")
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict)]


def _open_card(host: str, state: dict, hub_cli: str, runner, now: float) -> Optional[str]:
    """The id of this host's open SchemaAhead card on the hub, or None."""
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
        if seen is not None:  # in the replica: open or closed decides it
            return str(tid) if str(seen.get("status") or "") not in _CLOSED else None
        if now - float(state.get("filed_at") or 0) < _FRESH_S:
            return str(tid)  # filed recently, not in the replica yet
    return None


def report(host: str, why: str, since: float, *, runner: Optional[Callable[[list[str]], tuple[int, str, str]]] = None,
           hub_cli: Optional[str] = None, info: Optional[dict] = None, now: Optional[float] = None) -> Optional[str]:
    """Open (or comment) this host's SchemaAhead card on the hub. Returns the request id sent, else None.

    Never raises: the sync loop is already stopping when this runs."""
    try:
        return _report(host, why, since, runner, hub_cli, info, time.time() if now is None else now)
    except Exception:  # health: allow BLE001 -- an alert must never break the sync loop's shutdown
        log.warning("kanban sync: could not open the SchemaAhead card on the hub", exc_info=True)
        return None


def _report(host, why, since, runner, hub_cli, info, now) -> Optional[str]:
    from hermes_cli import kanban_capability_registry as reg
    from hermes_cli import kanban_placement as kp
    runner = runner or kp.hub_runner
    if runner is kp._default_runner:
        from hermes_state_guard import _in_test_context
        if _in_test_context():  # never file a real hub card from a test process
            log.warning("kanban sync: SchemaAhead hub card not filed from a test process")
            return None
    hub_cli = hub_cli or reg.hub_cli_path(kp.config().get("hub_cli"))
    key = incident_key(host, since)
    state = _read_state()
    if state.get("key") == key:
        return None  # this incident is already on the hub
    info = info if info is not None else cores()
    open_tid = _open_card(host, state, hub_cli, runner, now)
    if open_tid:
        rid = f"{key}:comment"
        text = (f"Again on {host} at {time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(since))}: {why} "
                f"(installed core {info.get('installed_sha') or 'unknown'}, newest seen "
                f"{info.get('newest_sha') or 'none logged'})")
        argv = [hub_cli, "request", HUB, "tasks", f"default/default/{open_tid}", "comment", "--arg",
                f"text={text[:7900]}", "--id", rid]
        rc, out, err = runner(argv)
        if rc not in (0, 3):
            log.warning("kanban sync: could not comment the SchemaAhead card %s on the hub (exit %s): %s",
                        open_tid, rc, (err or out)[-300:])
            return None
        _write_state({**state, "key": key, "task_id": open_tid, "commented_at": now})
        log.error("kanban sync: SchemaAhead again; commented hub card %s (%s)", open_tid, rid)
        return rid
    rid = key
    title = f"Kanban sync gave up on {host}: a newer core migrated its board (SchemaAhead)"
    argv = [hub_cli, "create-task", HUB, title, "--body", _body(host, why, since, info), "--assignee", "default",
            "--origin-session", "", "--id", rid, "--option", f"idempotency_key={json.dumps(key)}",
            "--option", "priority=900"]
    rc, out, err = runner(argv)
    if rc not in (0, 3):
        log.warning("kanban sync: could not open the SchemaAhead card on the hub (exit %s): %s", rc,
                    (err or out)[-300:])
        return None
    _write_state({"key": key, "request_id": rid, "task_id": None, "filed_at": now})
    log.error("kanban sync: opened a hub card for this SchemaAhead give-up (request %s)", rid)
    return rid


def on_give_up(gateway: str, why: str) -> None:
    """``run_forever``'s hook, right after it recorded the stop: the incident is that record's ``since``.

    A give-up while this host's own core update is under way (the installed checkout moved past the commit this
    process started on, and this process runs that checkout) is the update's transition, not an incident: the
    update restarts this gateway on the new core."""
    from hermes_cli import kanban_sync_runstate as runstate
    if _updating():
        log.warning("kanban sync: gave up the sync role while this host's core update is under way (%s -> %s); "
                    "no hub card", (_START_HEAD or "")[:12], (_installed_head() or "")[:12])
        return
    record = runstate._read(runstate.path()) or {}
    report(gateway, why, float(record.get("since") or time.time()))


_START_HEAD: Optional[str] = None


def _installed_head() -> Optional[str]:
    from hermes_cli import kanban_db as kb
    from hermes_cli.boot_bootstrap import read_git_head
    from hermes_cli.kanban_schema_owner import installed_core
    installed = installed_core(kb.kanban_home())
    return read_git_head(installed) if installed else None


def note_start() -> None:
    """``run_forever`` calls this as it starts: the installed commit this gateway's code was loaded from."""
    global _START_HEAD
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_schema_owner import installed_core, running_core
        if installed_core(kb.kanban_home()) == running_core():
            _START_HEAD = _installed_head()
    except Exception:  # health: allow BLE001 -- best effort; unknown means "not updating"
        _START_HEAD = None


def _updating() -> bool:
    if not _START_HEAD:
        return False
    now = _installed_head()
    return bool(now) and now != _START_HEAD
