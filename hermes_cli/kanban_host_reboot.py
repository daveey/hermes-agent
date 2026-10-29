"""Tell a host reboot from a worker crash in the dead-worker sweep, and name the card that rebooted the host. [daveey]

On 2026-10-29 a Kanban worker on hermes-hub ran a test suite whose subprocess executed
``sudo -n /sbin/shutdown -r now``. The dispatcher booked every running card as ``crashed`` ("pid N not
alive"), retried the culprit, and the culprit rebooted the hub twice more.

Every spawn already records ``worker_started_at = "<boot_id>:<pid1 start>|<start time>"``
(``_process_fingerprint``). When that boot id is not the current one, the worker died with its host:

- the run is booked ``host_reboot`` (not ``crashed``) and no failure is counted: a reboot is not the card's fault;
- the journal of the spawn's boot is read (``journalctl -b <boot> _COMM=sudo``) for a sudo
  shutdown/reboot/poweroff. If its ``PWD`` lies in the card's workspace (or a path naming the card id), or in
  the assignee profile's scratch dir while this card is that profile's only reboot victim, the card is the
  culprit: it is blocked (``capability``) quoting the journal line instead of being retried.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Callable, Iterable, Optional

HOST_REBOOT_OUTCOME = "host_reboot"
_log = logging.getLogger(__name__)

_SUDO_RE = re.compile(r"\bsudo\[\d+\]:\s+(?P<user>\S+)\s*:\s*(?P<fields>.*)$")
_POWER_RE = re.compile(
    r"^(?:\S*/)?(?:shutdown|reboot|poweroff|halt)(?:\s|$)"
    r"|^(?:\S*/)?systemctl(?:\s+\S+)*?\s+(?:reboot|poweroff|halt|kexec)(?:\s|$)"
    r"|^(?:\S*/)?(?:tel)?init\s+[06]\b"
)
_REFUSED_RE = re.compile(r"command not allowed|incorrect password|a password is required|NOT in sudoers")
_BOOT_RE = re.compile(r"^[0-9a-f]{32}$")

# Previous boots' journals never change, so one read per boot id is enough.
_journal_cache: dict[str, list[dict]] = {}


def boot_of(worker_started_at) -> str:
    """The boot id a spawn fingerprint was taken on ("" for legacy/int/UNVERIFIED fingerprints)."""
    s = str(worker_started_at or "")
    if "|" not in s:
        return ""
    return s.split("|", 1)[0].split(":", 1)[0].strip()


def current_boot() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def rebooted_since(worker_started_at, now_boot: Optional[str] = None) -> bool:
    """True only when both boot ids are known and differ (never on an unreadable /proc)."""
    spawn = boot_of(worker_started_at)
    now = current_boot() if now_boot is None else now_boot
    return bool(spawn and now and spawn != now)


def parse_power_commands(journal_text: str) -> list[dict]:
    """sudo journal lines that ran (not refused) a shutdown/reboot/poweroff/halt command."""
    found = []
    for line in journal_text.splitlines():
        m = _SUDO_RE.search(line)
        if not m or _REFUSED_RE.search(line):
            continue
        fields = {}
        for part in m.group("fields").split(" ; "):
            k, sep, v = part.partition("=")
            if sep:
                fields[k.strip()] = v.strip()
        cmd = fields.get("COMMAND", "")
        if cmd and _POWER_RE.search(cmd):
            found.append({"line": line.strip()[:400], "user": m.group("user"), "pwd": fields.get("PWD", ""),
                          "command": cmd})
    return found


def _read_journal(boot: str) -> str:
    p = subprocess.run(["journalctl", "-b", boot, "_COMM=sudo", "--no-pager", "-q", "-o", "short-iso"],
                       capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL, check=False)
    return p.stdout if p.returncode == 0 else ""


def power_commands(spawn_boot: str, reader: Optional[Callable[[str], str]] = None) -> list[dict]:
    """Power commands sudo ran during the boot ``spawn_boot`` (dashed /proc form accepted). [] when unreadable."""
    boot = spawn_boot.replace("-", "").lower()
    if not _BOOT_RE.match(boot):
        return []
    if reader is None and boot in _journal_cache:
        return _journal_cache[boot]
    try:
        found = parse_power_commands((reader or _read_journal)(boot))
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if reader is None:
        _journal_cache[boot] = found
    return found


def _under(path: str, root) -> bool:
    if not path or not root:
        return False
    p, r = os.path.normpath(path), os.path.normpath(str(root))
    return p == r or p.startswith(r.rstrip("/") + "/")


def profile_scratch_dirs(assignee: Optional[str], hermes_root: Path) -> list[Path]:
    if not assignee:
        return []
    if assignee == "default":
        return [hermes_root / "cache" / "scratch"]
    return [hermes_root / "profiles" / assignee / "cache" / "scratch"]


def hermes_root() -> Path:
    """The root Hermes home (``~/.hermes``), even when this process runs under a profile."""
    from hermes_constants import get_hermes_home
    home = Path(get_hermes_home())
    return home.parent.parent if home.parent.name == "profiles" else home


def victims(rows, is_local: Callable) -> tuple[str, dict]:
    """(current boot, {assignee: number of this host's running cards spawned on an earlier boot})."""
    now = current_boot()
    counts: dict = {}
    for row in rows:
        if is_local(row) and rebooted_since(row["worker_started_at"], now):
            counts[row["assignee"]] = counts.get(row["assignee"], 0) + 1
    return now, counts


def note(dead, row, now_boot: str, reboot_victims: dict) -> None:
    """Rebook a dead worker (``_DeadWorker``) whose spawn boot is not this boot as a host reboot, naming the
    card as the culprit when the spawn boot's journal shows it ran the sudo shutdown. No-op otherwise."""
    if not rebooted_since(row["worker_started_at"], now_boot):
        return
    spawn_boot = boot_of(row["worker_started_at"])
    dead.host_reboot = True
    dead.event_kind = HOST_REBOOT_OUTCOME
    dead.event_payload.update(host_reboot=True, spawn_boot=spawn_boot, current_boot=now_boot)
    culprit = find_culprit(power_commands(spawn_boot), task_id=row["id"], workspace_path=row["workspace_path"],
                           assignee=row["assignee"], sole_profile_victim=reboot_victims.get(row["assignee"]) == 1)
    head = f"host rebooted under worker pid {dead.event_payload.get('pid')} (boot {spawn_boot[:8]} -> {now_boot[:8]})"
    if culprit:
        dead.reboot_culprit = culprit
        dead.event_payload["reboot_culprit"] = culprit
        dead.error_text = (f"{head}: THIS card rebooted the host. Journal ({culprit['match']}): {culprit['line']} "
                           "Blocked instead of retried: remove whatever runs the real shutdown (e.g. a test "
                           "subprocess with the real sudo on PATH), then unblock. ") + dead.error_text
    else:
        dead.error_text = f"{head}; requeued without counting a failure. " + dead.error_text


def comment_culprit(conn, task_id: str, culprit: dict) -> None:
    """Say on the culprit card and on its linked cards which command rebooted the host."""
    from hermes_cli import kanban_db as kb
    try:
        links = conn.execute("SELECT parent_id AS other FROM task_links WHERE child_id = ? UNION "
                             "SELECT child_id FROM task_links WHERE parent_id = ?", (task_id, task_id)).fetchall()
        kb.add_comment(conn, task_id, "dispatcher", (
            f"host-reboot: this card's worker rebooted the host. Journal ({culprit.get('match')}): "
            f"`{culprit.get('line')}`. Blocked instead of retried; a retry would reboot it again."))
        for link in links:
            kb.add_comment(conn, link["other"], "dispatcher", f"host-reboot: linked card {task_id} rebooted the "
                           f"host (`{culprit.get('command')}` in {culprit.get('pwd')}); it is blocked.")
    except (sqlite3.Error, ValueError):  # the block already landed; a missing comment must not undo the sweep
        _log.warning("kanban: host-reboot comment failed for %s", task_id, exc_info=True)


def find_culprit(commands: Iterable[dict], *, task_id: str, workspace_path: Optional[str],
                 assignee: Optional[str], sole_profile_victim: bool,
                 root: Optional[Path] = None) -> Optional[dict]:
    """The journal power command this card issued, with how it was matched; None when it was a bystander."""
    root = root or hermes_root()
    for rec in commands:
        pwd = rec.get("pwd") or ""
        if workspace_path and _under(pwd, workspace_path):
            return {**rec, "match": "workspace"}
        if task_id and task_id in pwd.split("/"):
            return {**rec, "match": "task_id"}
        if sole_profile_victim and any(_under(pwd, d) for d in profile_scratch_dirs(assignee, root)):
            return {**rec, "match": "profile_scratch"}
    return None
