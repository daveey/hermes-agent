"""Hub admission at pickup: the hub judges a foreign card it would run before it copies and claims it. [daveey]

Admission used to run only where a card was created (``kanban_create`` / CLI create / move / courier landing).
A creator on an older core, or any path without the gate, could still put ``runs_on=[machine:hub]`` on a card,
and the hub's pickup copied and claimed it unjudged (t_01a1cf65a941bf43, created on worker-4, 11-12 10:55Z).
The executor is the one place every such card passes, so the hub checks it here:

* a card whose latest verdict (``hub_admission`` event, else its ``created`` payload) admits is copied as before;
* otherwise the judge runs (outside any write transaction). Admitted: copied, verdict recorded. Denied: copied
  only to record the verdict and re-place it on the worker queue (``moved`` event citing the verdict), so it is
  never claimed here. Unavailable: left on the Board and retried next tick (fail closed);
* :func:`refusals` is the dispatcher's backstop: a hub-bound foreign copy with no admit verdict is never claimed,
  whichever path copied it.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict
from typing import Any, Iterable, Mapping, Optional

_log = logging.getLogger(__name__)

ENTRY = "pickup"
MISSING = "hub_admission_missing"
UNAVAILABLE = "hub admission unavailable; retried next tick"


def hub_bound(labels: Optional[list[str]], own: str, cfg: Any,
              machines: Mapping[str, Mapping[str, Any]]) -> bool:
    """True when ``own`` is the hub and only the hub can claim a card with these ``runs_on`` labels."""
    from hermes_cli import kanban_worker_node as wn
    from hermes_cli.kanban_hub_admission_placement import is_hub
    if not labels or not is_hub(own, cfg):
        return False
    if any(v.startswith("machine:") and is_hub(v[8:], cfg) for v in labels):
        return True
    if any(v.startswith("machine:") for v in labels):
        return False  # pinned to a named non-hub machine
    return not any(set(labels) <= wn.effective_labels(entry, name) for name, entry in machines.items()
                   if not is_hub(name, cfg) and (entry or {}).get("state", "active") == "active")


def verdict(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    """The card's newest verdict: its latest ``hub_admission`` event, else its ``created`` payload's."""
    from hermes_cli.kanban_hub_admission_placement import latest_verdict
    found = latest_verdict(conn, task_id)
    if found is not None:
        return found
    row = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created' "
                       "ORDER BY id LIMIT 1", (task_id,)).fetchone()
    try:
        audit = (json.loads(row[0]) or {}).get("hub_admission") if row and row[0] else None
    except (TypeError, ValueError, AttributeError):
        return None
    return audit if isinstance(audit, dict) else None


def _admitted(conn: sqlite3.Connection, task_id: str) -> bool:
    found = verdict(conn, task_id)
    return bool(found and found.get("admit") is True)


def _labels(raw: Any) -> Optional[list[str]]:
    from hermes_cli import kanban_worker_node as wn
    return wn._parse(raw)


def _fields(card: Mapping[str, Any], board: str) -> dict:
    from hermes_cli.kanban_placement import without_placement_note

    def _list(raw):
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            return None
        return value if isinstance(value, list) else None
    return {"title": card["title"] or "", "body": without_placement_note(card["body"] or ""),
            "assignee": card["assignee"], "workspace_kind": card["workspace_kind"] or "scratch",
            "requires": _list(card["requires"]), "skills": _list(card["skills"]), "board": board,
            # A mandatory pin's attestation label rides the re-placed queue labels (kanban_mandatory_pin).
            **{k: card[k] for k in ("model_override", "provider_override", "model_pin_mandatory")
               if k in card.keys()}}


def _denied(audit: Mapping[str, Any], replaced: bool) -> str:
    """The pickup-skip text of a deny: the verdict, plus why it stays put when no worker queue took it."""
    why = f"hub admission denied: {str(audit.get('why') or '')[:150]}"
    if replaced:
        return why
    kept = f"kept home ({audit['home']})" if audit.get("home") else "no worker queue fits"
    return f"{why}; {kept}, left for its creator"


def _target(fields: dict, own: str, cfg: dict, audit: dict) -> Optional[list[str]]:
    """The worker-queue labels a denied card moves to (the creation-time deny route), or None when none fits."""
    from hermes_cli import kanban_hub_admission_placement as hp
    from hermes_cli import kanban_placement as kp
    if kp.structural_reason({k: fields[k] for k in ("title", "body", "workspace_kind", "assignee", "requires",
                                                    "board")}, cfg):
        return None
    requires = list(fields.get("requires") or [])
    if kp.portable_reason({**fields, "runs_on": None}, cfg):
        requires = list(dict.fromkeys([*requires, "hub:write"]))
    out = hp.pool_route(fields, cfg, own, audit, requires or None, queue=True)
    if home := (out.get("hub_admission") or {}).get("home"):
        audit["home"] = home  # the recorded verdict says why no queue fits: the claim gate refuses the text
    return out.get("runs_on") or None


def screen(conn: sqlite3.Connection, replica: sqlite3.Connection, own: str, ids: Iterable[str], *,
           cfg: Optional[dict] = None,
           machines: Optional[Mapping[str, Mapping[str, Any]]] = None,
           ) -> tuple[list[str], dict[str, dict], dict[str, tuple[str, str]]]:
    """Split pickup's ``ids``: ``(copy, plans, skips)``. ``plans[tid]`` is the verdict to record after the copy
    (``target`` set: re-place it); ``skips`` stay on the Board with a pickup-skip reason. No write happens here."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_hub_admission as judge
    from hermes_cli import kanban_hub_admission_placement as hp
    from hermes_cli import kanban_placement as kp
    from hermes_cli import kanban_worker_node as wn
    ids = list(ids)
    cfg = kp.config() if cfg is None else cfg
    if not ids or not hp.is_hub(own, cfg):
        return ids, {}, {}
    if machines is None:
        machines = (wn.registry() or {}).get("machines") or {}
    board = sync.board_slug_for_path(sync._db_key(conn) or "")
    copy: list[str] = []
    plans: dict[str, dict] = {}
    skips: dict[str, tuple[str, str]] = {}
    down = False
    for tid in ids:
        card = replica.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        labels = _labels(card["runs_on"]) if card is not None else None
        if card is None or not hub_bound(labels, own, cfg, machines) or _admitted(replica, tid):
            copy.append(tid)
            continue
        prior = verdict(replica, tid)
        if prior and prior.get("entry") == ENTRY and prior.get("runs_on") == labels:
            # Denied here already and not re-placed (no worker queue fits): never judged again on this placement.
            skips[tid] = ("other", _denied(prior, False))
            continue
        if down or conn.in_transaction:
            if not down:
                _log.warning("hub admission: pickup of %s deferred, the board connection is in a transaction", tid)
            skips[tid] = ("other", UNAVAILABLE)
            continue
        fields = _fields(card, board)
        try:
            found = judge.admit({"title": fields["title"], "body": fields["body"]})
        except Exception:  # health: allow BLE001 -- a failed judge is unavailable: never claimed unjudged
            found = judge.Verdict()
        if not found.admit and found.why == judge.Verdict().why:
            down = True  # one outage per tick: never one judge timeout per card
            skips[tid] = ("other", UNAVAILABLE)
            continue
        audit = {**asdict(found), "entry": ENTRY, "runs_on": labels, "created_on": card["created_on"]}
        plans[tid] = {"audit": audit, "target": None if found.admit else _target(fields, own, cfg, audit)}
        copy.append(tid)
    return copy, plans, skips


def apply(conn: sqlite3.Connection, own: str, plans: Mapping[str, dict], imported: Iterable[str],
          ) -> dict[str, tuple[str, str]]:
    """After the copy: record each verdict; re-place each denied card. Returns a pickup skip for each denied id."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_move as km
    from hermes_cli import kanban_worker_node as wn
    out: dict[str, tuple[str, str]] = {}
    for tid in imported:
        plan = plans.get(tid)
        if plan is None:
            continue
        audit, target = plan["audit"], plan["target"]
        try:
            with kb.write_txn(conn):
                row = conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()
                if row is None:
                    continue
                kb._append_event(conn, tid, "hub_admission", audit)
                if audit["admit"]:
                    continue
                out[tid] = ("other", _denied(audit, target is not None))
                if target is None:
                    continue  # the backstop (refusals) keeps it unclaimed here
                previous = wn._parse(row["runs_on"])
                wn.set_runs_on(conn, tid, target)
                kb._append_event(conn, tid, km.MOVED_EVENT, {
                    "from_machine": own, "runs_on": target, "previous_runs_on": previous,
                    "reason": f"hub admission denied at pickup: {str(audit.get('why') or '')[:300]}",
                    "move_no": len(km.history(conn, tid)) + 1, "forced": False, "matches": [], "queued": True,
                    "hub_admission": audit})
        except Exception:  # health: allow BLE001 -- the dispatcher backstop still refuses an unjudged copy
            _log.warning("hub admission: could not record the pickup verdict for %s", tid, exc_info=True)
    if out:
        _log.info("hub admission: pickup denied %d foreign card(s) for the hub: %s", len(out), sorted(out))
    return out


def refusals(conn: sqlite3.Connection, rows: Iterable[Mapping[str, Any]], own: str,
             machines: Mapping[str, Mapping[str, Any]], cfg: Optional[dict] = None) -> dict[str, str]:
    """``{task_id: MISSING}`` for foreign copies only the hub can run that carry no admit verdict."""
    from hermes_cli import kanban_placement as kp
    cfg = kp.config() if cfg is None else cfg
    out: dict[str, str] = {}
    for row in rows:
        if not row["created_on"] or row["created_on"] == own:
            continue
        if hub_bound(_labels(row["runs_on"]), own, cfg, machines) and not _admitted(conn, row["id"]):
            out[row["id"]] = MISSING
    return out
