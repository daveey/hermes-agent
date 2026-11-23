"""Stale-triage sweep: release triage cards nobody specifies, once per triage entry. [daveey]

Nothing consumes ``triage`` on boards without a specifier, and a card no chat owns (cron, automation,
CLI) has no other releaser. The sweep used to fire once per card ever, so the second time such a card
landed in triage it stayed there. Now it fires once per triage entry, capped at ``TRIAGE_SWEEP_CAP``
sweeps per card per day; past the cap the card stays in triage with one board-supervisor card as its
producer.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Optional

from hermes_cli import kanban_db as _kb
from hermes_cli.kanban_model_neutral import forwarded_text, forwarded_title

logger = logging.getLogger(__name__)

TRIAGE_STALE_SECONDS = 3600
TRIAGE_SWEEP_CAP = 3
TRIAGE_SWEEP_CAP_WINDOW = 86400
# Events that put a card into triage. A card in triage whose newest such event is newer than its last
# ``triage_swept`` re-entered triage after that sweep.
TRIAGE_ENTRY_EVENTS = ("gave_up", "block_loop_detected", "agent_block_escalated", "supervisor_escalated",
                       "continuation_exhausted", "blocked", "status", "spend_budget_triaged")
_TRIAGE_BRIEF = (
    "\n\n---\n[triage sweep] This card sat in triage with no specifier. Specify it "
    "(clarify title/body, then do the work), or park it with kanban_schedule + a wake_check "
    "probe if it waits on something external. Do not return it to triage."
)
_MARKS = ",".join("?" * len(TRIAGE_ENTRY_EVENTS))
# The re-entry predicate on alias ``t`` (params ``_PENDING_ARGS``): a triage entry newer than the last sweep, and the
# sweep not capped since that entry. Shared by ``sweep_stale_triage`` and ``awaiting_stale_sweep``.
_PENDING = (
    f"COALESCE((SELECT MAX(n.id) FROM task_events n WHERE n.task_id = t.id AND n.kind IN ({_MARKS})), 0) > "
    "COALESCE((SELECT MAX(s.id) FROM task_events s WHERE s.task_id = t.id AND s.kind = 'triage_swept'), -1) "
    "AND NOT EXISTS (SELECT 1 FROM task_events c WHERE c.task_id = t.id AND c.kind = 'triage_sweep_capped' "
    f"AND c.id > COALESCE((SELECT MAX(n.id) FROM task_events n WHERE n.task_id = t.id AND n.kind IN ({_MARKS})), 0))")
_PENDING_ARGS = (*TRIAGE_ENTRY_EVENTS, *TRIAGE_ENTRY_EVENTS)


def _gate_exclusion() -> str:
    """Cards waiting at an enforced lane gate are not stale triage (``kanban_lanes.held_exclusion``)."""
    from hermes_cli.kanban_lanes import held_exclusion

    return held_exclusion("t") + " "


def _supervisor_profile() -> str:
    """``kanban.orchestrator_profile``, else ``default``."""
    try:
        from hermes_cli.config import load_config_readonly
        name = ((load_config_readonly() or {}).get("kanban") or {}).get("orchestrator_profile")
    except Exception:  # config unreadable: fall back to the default profile
        logger.warning("kanban: triage sweep could not read kanban.orchestrator_profile", exc_info=True)
        name = None
    return (name.strip() if isinstance(name, str) else "") or "default"


def triage_reasons(conn: sqlite3.Connection, task_id: str, limit: int = 3) -> list[str]:
    """The card's last ``limit`` triage entries, oldest first, as ``kind at <epoch>: reason`` lines."""
    rows = conn.execute(
        f"SELECT kind, payload, created_at FROM task_events WHERE task_id = ? AND kind IN ({_MARKS}) "
        "ORDER BY id DESC LIMIT ?", (task_id, *TRIAGE_ENTRY_EVENTS, limit),
    ).fetchall()
    out = []
    for kind, payload, created_at in reversed(rows):
        try:
            data = json.loads(payload) if payload else {}
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        why = data.get("reason") or data.get("error") or data.get("reason_code") or "(no reason recorded)"
        out.append(f"{kind} at {created_at}: {' '.join(str(why).split())[:400]}")
    return out


def _cap(conn: sqlite3.Connection, row, now: int) -> str:
    """Leave the card in triage and give it a producer: one board-supervisor card per card per day,
    named by a ``triage_sweep_capped`` event and a comment. Returns the supervisor card id."""
    tid = row["id"]
    reasons = triage_reasons(conn, tid)
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    body = (
        f"Card {tid} keeps landing in triage. The stale-triage sweep released it "
        f"{TRIAGE_SWEEP_CAP} times in 24 h and it came back each time, so the sweep stopped and left it in "
        "triage. No chat owns it, so nothing else will release it: this card is its producer.\n\n"
        "Source title (historical evidence):\n" + forwarded_text(row['title']) + "\n\n"
        "Its last triage reasons (historical evidence):\n" + "\n".join(forwarded_text(r) for r in reasons) + "\n\n"
        f"Find why {tid} keeps failing and fix that cause (or file the fix card). Then release it with "
        f"`hermes kanban promote {tid} --reason '<what changed>'`, or archive it if the work is no longer "
        f"wanted. Record what you did as a comment on {tid}."
    )
    sup_id = _kb.create_task(
        conn, title=forwarded_title(f"Triage loop: {row['title']}"[:200], f"Triage loop: {tid}"), body=body,
        assignee=_supervisor_profile(), created_by="triage-sweep", workspace_kind="scratch",
        idempotency_key=f"triage-sweep-cap:{tid}:{day}", tenant=row["tenant"],
    )
    with _kb.write_txn(conn):
        _kb._append_event(conn, tid, "triage_sweep_capped", {
            "supervisor_task_id": sup_id, "cap": TRIAGE_SWEEP_CAP, "window": TRIAGE_SWEEP_CAP_WINDOW,
            "reasons": reasons,
        })
    try:
        _kb.add_comment(conn, tid, "triage-sweep", f"Triage sweep capped ({TRIAGE_SWEEP_CAP} sweeps in 24 h). "
                                                   f"Supervisor card {sup_id} owns releasing it.")
    except Exception:
        logger.warning("kanban: triage cap comment failed for %s", tid, exc_info=True)
    return sup_id


def stranded_contained(conn: sqlite3.Connection, *, now: Optional[float] = None) -> list[dict]:
    """Held triage cards whose authority finished 30+ min ago with nobody left to release them, from the
    ``kanban_wait_info.describe_contained`` judgment: ``[{id, title, tenant, episode, wait}]``. Read-only."""
    from hermes_cli import kanban_contained_wait, kanban_wait_info as kwi
    try:
        ids = [r[0] for r in conn.execute("SELECT id FROM tasks WHERE status = 'triage' AND containment_episode IS NOT NULL")]
    except sqlite3.Error:
        return []
    out = []
    for tid, info in kanban_contained_wait.read(conn, ids).items():
        wait = kwi.describe_contained(info, now=now)
        if wait["agent_owned"]:
            row = conn.execute("SELECT title, tenant FROM tasks WHERE id = ?", (tid,)).fetchone()
            out.append({"id": tid, "title": row[0], "tenant": row[1], "episode": info["episode"], "wait": wait})
    return out


def _local_creator_links(conn: sqlite3.Connection, tid: str, gateway: str, rows: list[dict]) -> list[dict]:
    """Match _local_links' persistent creator binding without scanning the whole board."""
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if not task or dict(task).get("created_on") not in (None, "", gateway):
        return []
    if not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='creator_chats_resolved' LIMIT 1",
                        (tid,)).fetchone():
        return []
    # A foreign reconciler can stale-release this row; local reconciliation restores it.
    return [r for r in rows if r["chat_gateway"] == gateway and r["kind"] == "created"]


def _contained_chat_owners(conn: sqlite3.Connection, ids: list[str]) -> set[str]:
    """Owning chats on any gateway, including subscriptions not projected into links yet.

    Stored releases win over lingering remote subscriptions; local resolved creator links
    persist like _local_links even when a foreign reconciler stale-releases their flag.
    Notice-only subscriptions and machine/placeholder sessions are not owners. Read failures propagate to the sweep's
    fail-closed boundary rather than becoming evidence of absent ownership. [daveey]
    """
    from hermes_cli.kanban_db_notify import is_machine_session
    from hermes_cli.kanban_session_activity import _counted_sub_clause
    from hermes_cli.kanban_origin import OriginError, gateway_name, parse_origin
    from hermes_cli.kanban_placement import is_placeholder_session

    gateway = gateway_name()
    owned: set[str] = set()
    has_links = conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone()
    for tid in ids:
        rows = [dict(r) for r in conn.execute("SELECT * FROM task_chat_links WHERE task_id=?", (tid,))] if has_links else []
        known = {(r["chat_gateway"], r["chat_profile"] or "default", r["chat_session"]) for r in rows}
        candidates = [r for r in rows if not r["released"] and r["kind"] in ("created", "shepherd")]
        candidates.extend(_local_creator_links(conn, tid, gateway, rows))
        # Per-card reads only (no board-wide scan on the dispatcher hot path). Subs count the way
        # _owner_hub_links does: creators, shepherds and origin bindings, not notice-only subs.
        made_in = conn.execute("SELECT session_id FROM tasks WHERE id=?", (tid,)).fetchone()
        if made_in and made_in[0]:
            candidates.append({"chat_gateway": gateway, "chat_profile": None, "chat_session": made_in[0]})
        for local in conn.execute(
                "SELECT s.chat_id FROM kanban_notify_subs s JOIN tasks t ON t.id=s.task_id "
                f"WHERE s.task_id=? AND s.platform='tui' AND s.chat_id != '' AND {_counted_sub_clause(conn)}", (tid,)):
            candidates.append({"chat_gateway": gateway, "chat_profile": None, "chat_session": local[0]})
        for sub in conn.execute(
                "SELECT s.chat_id FROM kanban_notify_subs s JOIN tasks t ON t.id=s.task_id "
                f"WHERE s.task_id=? AND s.platform='hub' AND s.chat_id != '' AND {_counted_sub_clause(conn)}", (tid,)):
            try:
                origin = parse_origin(sub[0])
            except OriginError:
                continue
            key = (origin.gateway, origin.profile or "default", origin.session)
            if key not in known:
                candidates.append({"chat_gateway": origin.gateway, "chat_profile": origin.profile,
                                   "chat_session": origin.session})
                known.add(key)
        for row in candidates:
            if not is_placeholder_session(row["chat_session"]) and not is_machine_session(
                    row["chat_gateway"], row["chat_session"], local_gateway=gateway, profile=row["chat_profile"]):
                owned.add(tid)
    return owned


def escalate_stranded_contained(conn: sqlite3.Connection, *, now: Optional[float] = None,
                                skip: Optional[set] = None) -> list[str]:
    """A stranded contained card with a chat is woken by the owner wake (``wait.agent_owned``); one with NO chat has
    no owner, so it gets one board-supervisor card per containment episode (key ``containment-strand:<task>:<episode>``)
    plus a comment on the held card. Writes no event on the held card (its history is containment custody). Returns
    the supervisor card ids created this call.

    Retained copies of another gateway's card are skipped. Those copies have no triage authority, and a worker
    must not create a supervisor card for one. [daveey]"""
    from hermes_cli.kanban_wake_check import foreign_ids

    skipped = foreign_ids(conn) if skip is None else skip
    stranded = [card for card in stranded_contained(conn, now=now)
                if card["id"] not in skipped and not conn.execute(
                    "SELECT 1 FROM tasks WHERE idempotency_key = ?",
                    (f"containment-strand:{card['id']}:{card['episode']}",)).fetchone()]
    if not stranded:
        return []
    try:
        linked = _contained_chat_owners(conn, [card["id"] for card in stranded])
    except Exception:
        logger.warning("kanban: containment strand check could not read chat links", exc_info=True)
        return []
    created: list[str] = []
    for card in stranded:
        tid, episode = card["id"], card["episode"]
        if tid in linked or tid in skipped:
            continue
        key = f"containment-strand:{tid}:{episode}"
        if conn.execute("SELECT 1 FROM tasks WHERE idempotency_key = ?", (key,)).fetchone():
            continue
        wait, shown = card["wait"], card["wait"]["contained"]
        body = (
            f"Card {tid} is held by containment episode {episode} and no chat owns it. Its "
            f"authority card {shown['authority']['id']} is {shown['authority']['status']}, so nothing will release it: "
            "\nHistorical source title and wait:\n" + forwarded_text(card['title']) + "\n"
            + forwarded_text(wait['waiting_on']) + "\n\n"
            "The held card cannot be archived, promoted, completed, unblocked or unlinked. "
            "The exits are `kanban_resume_contained(task_id, episode, kind='authority')` from a live run of the "
            f"authority (reopen {shown['authority']['id']} first if it is done), or `kind='producer'` once the producer "
            "has a new completed run. If the hold is deliberate (a spend or decision hold), say so on this card and "
            f"close it. Record what you did as a comment on {tid}."
        )
        try:
            sup_id = _kb.create_task(
                conn, title=forwarded_title(f"Contained card stranded: {card['title']}"[:200],
                                            f"Contained card stranded: {tid}"), body=body,
                assignee=_supervisor_profile(), created_by="triage-sweep", workspace_kind="scratch",
                idempotency_key=key, tenant=card["tenant"])
            _kb.add_comment(conn, tid, "triage-sweep",
                            f"Containment episode {episode} is stranded (authority {shown['authority']['id']} is "
                            f"{shown['authority']['status']}) and no chat owns this card. Supervisor card {sup_id} owns it.")
            created.append(sup_id)
        except Exception:
            logger.exception("kanban: containment strand escalation failed for %s", tid)
    return created


def awaiting_stale_sweep(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when :func:`sweep_stale_triage` will still release this card on its own (once idle long enough): in
    triage with no stored human_request, not gate-held, re-entered since its last sweep and not capped, and not a
    hold the sweep skips (spend
    decision, containment, an open kernel recovery card, a judged goal hold). Such a card is agent-owned; it is not
    stranded."""
    # Any stored human_request (even an invalid one) makes the sweep skip the card, so it is not the sweep's.
    if conn.execute("SELECT 1 FROM tasks t WHERE t.id = ? AND t.status = 'triage' AND t.human_request IS NULL "
                    + _gate_exclusion()
                    + "AND " + _PENDING, (task_id, *_PENDING_ARGS)).fetchone() is None:
        return False
    from hermes_cli.kanban_containment_connections import held as containment_held
    from hermes_cli.kanban_goal_recovery import current_park
    from hermes_cli.kanban_resume_requester import open_recovery_card
    from hermes_cli.kanban_spend_decision import pending as spend_decision_pending
    return not (spend_decision_pending(conn, task_id) or containment_held(conn, task_id)
                or open_recovery_card(conn, task_id) or current_park(conn, task_id))


def sweep_stale_triage(
    conn: sqlite3.Connection, *, default_assignee: Optional[str] = None,
    older_than: int = TRIAGE_STALE_SECONDS, now: Optional[int] = None,
) -> list[str]:
    """Move triage cards idle for ``older_than`` seconds to ``ready`` on the default profile with a
    "specify or park with a probe" brief. A card is eligible once per triage entry: never swept, or a
    ``TRIAGE_ENTRY_EVENTS`` event newer than its last ``triage_swept``. Past ``TRIAGE_SWEEP_CAP`` sweeps
    in ``TRIAGE_SWEEP_CAP_WINDOW`` it is capped instead (see :func:`_cap`). Cards with an open human
    request, or an open kernel recovery card for their current park, are left alone. Returns swept task ids."""
    now = int(now if now is not None else time.time())
    cutoff = now - older_than
    swept: list[str] = []
    rows = conn.execute(
        "SELECT t.id, t.title, t.body, t.assignee, t.human_request, t.tenant FROM tasks t "
        "WHERE t.status = 'triage' " + _gate_exclusion() +
        "AND COALESCE((SELECT MAX(e.created_at) FROM task_events e WHERE e.task_id = t.id), t.created_at) <= ? "
        "AND " + _PENDING, (cutoff, *_PENDING_ARGS),
    ).fetchall()
    from hermes_cli.kanban_resume_requester import open_recovery_card
    from hermes_cli.kanban_goal_recovery import current_park
    from hermes_cli.kanban_wake_check import foreign_ids

    # Retained executor copies have no triage policy authority. Local cap keys cannot
    # arbitrate supervisor creation across gateways, even when neither has synced yet.
    from hermes_cli.kanban_spend_decision import pending as spend_decision_pending, repair_triage_routes
    copies = foreign_ids(conn)
    # Decision ownership needs repair even while execution is gated, fresh, capped
    # or contained. Only the home board may publish its replacement route.
    repair_triage_routes(conn, skip=copies)
    try:
        # After the foreign-copy fence. A retained worker copy must not create a supervisor card.
        escalate_stranded_contained(conn, now=now, skip=copies)
    except Exception:
        logger.exception("kanban: containment strand escalation failed")
    from hermes_cli.kanban_containment_connections import held as containment_held
    for row in rows:
        if row["id"] in copies or containment_held(conn, row["id"]):
            continue
        if spend_decision_pending(conn, row["id"]):
            continue
        if row["human_request"] or open_recovery_card(conn, row["id"]) or current_park(conn, row['id']):
            continue  # a human owns it, or its kernel recovery card is its producer
        from hermes_cli.kanban_triage_cleanup import archive_requested_fixture
        if archive_requested_fixture(conn, row["id"]):
            continue
        recent = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'triage_swept' AND created_at > ?",
            (row["id"], now - TRIAGE_SWEEP_CAP_WINDOW),
        ).fetchone()[0]
        if recent >= TRIAGE_SWEEP_CAP:
            try:
                _cap(conn, row, now)
            except Exception:
                logger.exception("kanban: triage sweep cap failed for %s", row["id"])
            continue
        assignee = (row["assignee"] or "").strip() or default_assignee or "default"
        body = row["body"] or ""
        if _TRIAGE_BRIEF.strip() not in body:
            body += _TRIAGE_BRIEF
        with _kb.write_txn(conn):
            if spend_decision_pending(conn, row["id"]) or containment_held(conn, row["id"]):
                continue
            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', assignee = ?, body = ? WHERE id = ? AND status = 'triage'",
                (assignee, body, row["id"]),
            )
            if cur.rowcount:
                _kb._append_event(conn, row["id"], "triage_swept", {"assignee": assignee, "older_than": older_than})
                swept.append(row["id"])
    return swept
