"""Hold a card whose profile owner is persistently down instead of charging it a crash. [daveey]

2026-11-02 08:21Z on hermes-hub: ``hermes-gateway-askeve-builder.service`` had been ``failed`` since
08:04 (a deliberate pause stopped it). The dispatcher still claimed t_01a19b8ff5c2ddc9 for that profile
twice in 40 s. Each worker's owner dial got ``Gateway inaccessible: service_failed``, which is neither a
silent nor a draining owner, so the worker exited 1, the sweep booked ``crashed`` twice and the
2-failure breaker sent the card to triage. No turn ran, and nothing told whoever could fix the owner.

A failed, masked or disabled service is a known, persistent "owner is down" state with its own class:

- the worker exits ``KANBAN_OWNER_DOWN_EXIT_CODE`` (``kanban_worker_client``) for such a refusal before
  any admission, and the sweep books it ``owner_down`` and requeues it without counting a failure
  (``kanban_owner_unavailable.note``, bounded by the same neutral-streak limit);
- before claiming for a non-default profile, the dispatcher runs the read-only service probe
  (``gateway_runtime_service._systemd``: ``systemctl show``, never ``start``). On a persistent code it
  claims nothing, parks the card ``scheduled`` (reason ``owner_down:<profile>:<code>``) with a wake check
  that passes once the unit is no longer failed/masked, and files ONE Kanban card per profile and code
  ("profile <p> owner down: <code>") for whoever can fix the owner, under the open card that already
  works on that unit when there is one. Later parks comment on that card instead of filing another.

Any other refusal code keeps the old path.
"""
from __future__ import annotations

import logging
import shlex
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

# The worker's exit for a persistent pre-admission owner refusal. 68 == BSD EX_NOHOST, unused by any
# other worker exit (69 owner unavailable, 75 rate limit, 76 owner restart, 78 terminal provider).
KANBAN_OWNER_DOWN_EXIT_CODE = 68
OWNER_DOWN_OUTCOME = "owner_down"
# ``RuntimeStartError`` reasons that do not clear on their own: an operator stopped, masked or disabled
# the owner's service. Draining/starting/unknown states are transient and keep their own handling.
PERSISTENT_CODES = frozenset({"service_failed", "service_masked", "service_disabled"})
CARD_CREATOR = "kanban-owner-down"
PROBE_TTL_SECONDS = 30.0
_PROBE_BUDGET_SECONDS = 5.0
_cache: dict[str, tuple[float, Optional[OwnerDown]]] = {}


@dataclass(frozen=True)
class OwnerDown:
    profile: str
    code: str
    unit: str
    wake_check: str

    @property
    def reason(self) -> str:
        return f"owner_down:{self.profile}:{self.code}"

    @property
    def title(self) -> str:
        return f"profile {self.profile} owner down: {self.code}"


def persistent(reason_code: Optional[str]) -> bool:
    return reason_code in PERSISTENT_CODES


def wake_check_for(unit: str, *, system: bool = False) -> str:
    """Exit 0 once *unit* is neither failed nor masked, 1 while it is, 2 when systemctl cannot answer."""
    scope = "" if system else "--user "
    env = "" if system else 'XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" '
    return (f"s=$({env}systemctl {scope}show {shlex.quote(unit)} -p ActiveState -p UnitFileState --value) "
            "|| exit 2; case \"$s\" in *failed*|*masked*) exit 1;; esac; exit 0")


def _profile_home(profile: str) -> Optional[Path]:
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
    try:
        return Path(resolve_profile_env(normalize_profile_name(profile)))
    except (FileNotFoundError, ValueError, OSError):
        return None


def _probe_uncached(profile: str) -> Optional[OwnerDown]:
    home = _profile_home(profile)
    if home is None:
        return None
    from hermes_cli.gateway_runtime_service import RuntimeStartError, _systemd, service_suffix
    try:
        _systemd(home, time.monotonic() + _PROBE_BUDGET_SECONDS)
        return None
    except RuntimeStartError as exc:
        if not persistent(exc.reason):
            return None
        suffix = service_suffix(home)
        unit = f"hermes-gateway{'-' + suffix if suffix else ''}.service"
        system = (not (Path.home() / ".config/systemd/user" / unit).exists()
                  and (Path("/etc/systemd/system") / unit).exists())
        return OwnerDown(profile, exc.reason, unit, wake_check_for(unit, system=system))
    except (OSError, TimeoutError, ValueError):
        return None  # cannot tell: today's path (claim, and the worker says what it finds)


def probe(profile: Optional[str]) -> Optional[OwnerDown]:
    """The persistent down state of *profile*'s own gateway service, or None (up, unknown, or a
    profile with no service of its own). Read-only; cached ``PROBE_TTL_SECONDS`` per profile."""
    if not profile or profile == "default" or not sys.platform.startswith("linux"):
        return None
    now = time.monotonic()
    hit = _cache.get(profile)
    if hit is not None and now - hit[0] < PROBE_TTL_SECONDS:
        return hit[1]
    result = _probe_uncached(profile)
    _cache[profile] = (now, result)
    return result


def _related_owner(conn: sqlite3.Connection, down: OwnerDown) -> Optional[str]:
    """The open card already working on the unit (its title or body names it), newest running first."""
    row = conn.execute(
        "SELECT id FROM tasks WHERE status NOT IN ('done', 'archived') "
        "AND (title LIKE ? OR COALESCE(body, '') LIKE ?) "
        "AND COALESCE(created_by, '') NOT IN (?, 'self-heal') AND title NOT LIKE 'self-heal%' "
        "AND title NOT LIKE 'profile % owner down: %' "
        "ORDER BY status = 'running' DESC, created_at DESC LIMIT 1",
        (f"%{down.unit}%", f"%{down.unit}%", CARD_CREATOR)).fetchone()
    return row[0] if row else None


def ensure_card(conn: sqlite3.Connection, down: OwnerDown, held_task: str) -> str:
    """The one open owner-down card for (profile, code): created on first sight, commented on after."""
    from hermes_cli import kanban_db as kb
    row = conn.execute(
        "SELECT id FROM tasks WHERE title = ? AND created_by = ? AND status NOT IN ('done', 'archived') "
        "ORDER BY created_at DESC LIMIT 1", (down.title, CARD_CREATOR)).fetchone()
    if row:
        kb.add_comment(conn, row[0], CARD_CREATOR, f"Also holding {held_task} until the owner is back.")
        return row[0]
    related = _related_owner(conn, down)
    body = (
        f"The `{down.profile}` profile's gateway service `{down.unit}` is `{down.code}`, so no worker can "
        f"reach its owner. The dispatcher holds every card assigned to `{down.profile}` in `scheduled` "
        f"(reason `{down.reason}`) without spending its failure budget; they wake by themselves once the "
        "unit is no longer failed/masked.\n\n"
        f"First held card: {held_task}.\n\n"
        "Steps: 1) Find out why the unit is down (`systemctl --user status` / journal). 2) If it is down "
        "on purpose (a pause) or another open card owns it, do not start it: leave this card waiting on "
        "that owner. 3) Otherwise fix and start it, confirm the held cards wake, then complete this card."
        + (f"\n\nWaits on {related}, the open card working on this unit." if related else ""))
    return kb.create_task(conn, title=down.title, body=body, assignee="default", created_by=CARD_CREATOR,
                          parents=[related] if related else (), workspace_kind="scratch",
                          idempotency_key=f"owner-down:{down.profile}:{down.code}:{held_task}")


def hold(conn: sqlite3.Connection, task_id: str, down: OwnerDown) -> bool:
    """Park *task_id* on the down owner (no claim, no run, no failure) and file/update its card.
    False when the park did not land (the caller keeps today's path)."""
    from hermes_cli import kanban_db as kb
    card = ensure_card(conn, down, task_id)
    reason = (f"{down.reason} — the profile's gateway service {down.unit} is {down.code.split('_', 1)[1]}; "
              f"held until it is back (owner card {card})")
    return bool(kb.schedule_task(conn, task_id, reason=reason, wake_check=down.wake_check,
                                 awaiting={"kind": "card", "ref": card}))


def preflight(conn: sqlite3.Connection, task_id: str, assignee: Optional[str]) -> Optional[OwnerDown]:
    """Dispatcher hook before the claim: the down state when the card was held, else None. Never raises
    for a probe or park failure (the card then takes today's path); SQLite errors propagate."""
    try:
        down = probe(assignee)
        if down is None:
            return None
        return down if hold(conn, task_id, down) else None
    except sqlite3.Error:
        raise
    except Exception:  # health: allow BLE001 -- a broken preflight must never strand dispatch; traceback logged [daveey]
        _log.warning("kanban owner-down preflight failed for %s (%s)", task_id, assignee, exc_info=True)
        return None
