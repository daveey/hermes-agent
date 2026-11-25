"""Caused spend stops: advisory classification, canonical-only settlement. [daveey]

No local scope renewal or human identity is minted here. Board revalidates all
mutable premises; an authenticated receipt is historical settlement, not local
execution clearance. Normal sync delivers the resulting lifecycle rows.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
import time

CAUSES = frozenset({"scope_stopped", "capability_unavailable"})
PROMPT = """Review this stopped card against its original acceptance, not an agent's narrower run mandate.
Return JSON only with decision release, wait_on, archive, or escalate and a nonempty reason.
release: remaining work belongs to this card; quote at least 30 characters verbatim from evidence
in remaining_quote. The canonical operation grants a fixed ten-run round, never a custom budget.
wait_on: target is an explicitly named open prerequisite card. archive: target is an explicitly
named done/archived replacement. Include target only for wait_on/archive. If uncertain, escalate.
Safety restrictions cannot be reviewed or released. Capability availability is not inferred from prose.
"""


def record(raw):
    try:
        value = json.loads(raw or "{}") if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def effective_cause(conn, task_id, stop):
    """Annotations belong to the exact episode; safety dominates even ordinary roots."""
    from hermes_cli.kanban_refusal_receipt import restricted
    if restricted(conn, task_id):
        return "safety_refusal"
    episode = stop.get("uid") or str(stop["id"])
    causes = [record(stop.get("payload")).get("cause")]
    for kind, raw in conn.execute("SELECT kind,payload FROM task_events WHERE task_id=? AND kind IN "
                                  "('decision_hold_restricted','decision_retained')", (task_id,)):
        p = record(raw)
        if kind == "decision_hold_restricted" and p.get("episode") == episode:
            causes.append(p.get("cause"))
        if kind == "decision_retained" and p.get("episode_uid") == episode:
            causes.append("safety_refusal" if p.get("superseded_by_safety_stop") is True
                          else record(p.get("disposition")).get("reason"))
    for cause in ("safety_refusal", "capability_unavailable", "scope_stopped"):
        if cause in causes:
            return cause
    return next((c for c in causes if c), None)


def fenced(conn, task_id):
    from hermes_cli.kanban_containment_guard import pending
    from hermes_cli.kanban_stop_ended import stopped_at
    from hermes_cli.kanban_continuation import HUMAN_BLOCK_KINDS
    row = conn.execute("SELECT status,human_request,block_kind,current_run_id FROM tasks WHERE id=?",
                       (task_id,)).fetchone()
    return (row is None or row[0] != "triage" or bool(row[1]) or row[2] in HUMAN_BLOCK_KINDS
            or row[3] is not None or pending(conn, task_id) or stopped_at(conn, task_id) is not None)


def grant_count(conn, task_id):
    """Conservative count: agent budget edits never reset caused-stop grants."""
    episodes = set()
    for kind, raw, uid in conn.execute("SELECT kind,payload,id FROM task_events WHERE task_id=? AND kind IN "
                                       "('spend_review','spend_review_granted','spend_review_settled')", (task_id,)):
        p = record(raw)
        if kind == "spend_review_granted" or p.get("verdict") == "release":
            episodes.add(p.get("episode") or str(uid))
    return len(episodes)


def enrich(conn, task_id, stop, ctx):
    cause = effective_cause(conn, task_id, stop)
    if cause is None:
        return ctx
    ctx["cause"] = cause
    # Board is the definitive window validator. This bounded advisory corpus must
    # never confer authority; an overinclusive old quote is refused there.
    texts = [record(stop.get("payload")).get("reason") or ""]

    texts += [c["body"] for c in ctx["comments"] if c["in_window"] and c["author"] not in
              {"kanban-spend-reviewer", "kanban-spend-budget"}]
    for raw, in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='decision_retained'",
                             (task_id,)):
        p = record(raw)
        if p.get("episode_uid") == (stop.get("uid") or str(stop["id"])):
            texts.append(str(record(p.get("disposition")).get("summary") or "")[:2000])
    ctx["target_evidence"] = [text for text in texts if text]
    ctx["evidence"] = ctx["target_evidence"] + [r["summary"] for r in ctx["runs"] if r["summary"]]
    return ctx


def validate(verdict, ctx):
    """Return only bounded v1 wire fields, never model-supplied identity."""
    choice = verdict.get("decision")
    reason = verdict.get("reason")
    if choice not in {"release", "wait_on", "archive", "escalate"}:
        raise ValueError("invalid caused-stop decision")
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 2000:
        raise ValueError("missing or oversized reason")
    fields = {"verdict": choice, "reason": reason.strip()}
    if choice == "release":
        # Board independently checks its trusted live registry and placement.
        # Neither model prose nor a local machine snapshot attests capability.
        quote = verdict.get("remaining_quote")
        if (not isinstance(quote, str) or not 30 <= len(quote.strip()) <= 2000
                or not any(quote.strip() in text for text in ctx.get("evidence", []))):
            raise ValueError("no verbatim remaining-work quote")
        fields["quote"] = quote.strip()
    if choice in {"wait_on", "archive"}:
        target = verdict.get("target")
        if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,200}", target):
            raise ValueError("invalid target")
        # Same boundary as Board spend_review.ts names(): one sentence-ending "." or ":" is prose, a longer
        # coordinate (t_x.2, t_x:child, t_x-old, gw:t_x) is another name.
        if target == ctx["task_id"] or not any(re.search(
                r"(?<![A-Za-z0-9._:-])" + re.escape(target)
                + r"(?=\Z|[^A-Za-z0-9._:-]|[.:](?:\Z|[^A-Za-z0-9._:-]))", text)
                for text in ctx.get("target_evidence", [])):
            raise ValueError("target is not named in stop evidence")
        fields["target"] = target
    return fields


class Refused(ValueError):
    """An exact canonical response definitively refused this request."""


class Unconfirmed(ValueError):
    """The original request remains pending; never choose a replacement verdict."""


def canonical(conn, task_id, episode, fields, max_grants, *, resend=True):
    """Authenticated v1 operation and exact receipt readback; no local release fallback."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as client
    state = sync.sync_state(conn)
    if not state or sync.board_mode(conn) != "primary":
        raise ValueError("caused-stop review needs the active canonical primary Board")
    board = state["board_id"]
    # Fixed per-episode identity: uncertain transport must not mint another grant.
    uid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"spend-review:{board}:{task_id}:{episode}"))
    wire = {"client_op_id": uid, "op": "spend_review", "task": task_id, "event_uid": uid,
            "episode": episode, "max_grants": min(max_grants, 2), **fields}
    from hermes_cli import kanban_spend_review_request as journal
    prior = journal.get(conn, task_id, episode)
    if prior and "refused" in prior:
        raise Refused(prior["refused"])
    hub = client.Hub(client.load_identity(), deadline=time.monotonic() + 90)
    try:
        if prior:
            if prior["board"] != board or prior["gateway"] != hub.ident.gateway:
                raise Unconfirmed("frozen request belongs to another Board or gateway")
            wire = prior["wire"]
        else:
            capability = hub.decision_retain_capability(board)
            if not isinstance(capability, dict) or type(capability.get("spend_review")) is not int or capability["spend_review"] != 1:
                raise ValueError("Board spend_review v1 unavailable")
            wire = journal.freeze(conn, board, hub.ident.gateway, wire)["wire"]
        uid = wire["event_uid"]
        fields = wire
        digest = hashlib.sha256(json.dumps(wire, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode()).hexdigest()
        proof = hub.row_evidence(board, "task_events", uid) if prior else None
        if not isinstance(proof, dict) or proof.get("state") != "live":
            if not resend:
                raise Unconfirmed("frozen request readback pending; current holds prohibit resubmission")
            client.push(conn, hub, board, hub.ident.gateway, primary=True)
            reply = hub.ops(board, [wire])
            results = reply.get("results") if isinstance(reply, dict) else None
            if (not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict)
                    or results[0].get("client_op_id") != uid):
                raise Unconfirmed("unknown canonical reply")
            if results[0].get("status") == "rejected":
                reason = f"canonical spend review refused: {results[0].get('reason')}"
                journal.refuse(conn, task_id, episode, reason)
                raise Refused(reason)
            if results[0].get("status") != "applied":
                raise Unconfirmed("unknown canonical status")
            proof = hub.row_evidence(board, "task_events", uid)
        row = proof.get("row") if isinstance(proof, dict) and proof.get("state") == "live" else None
        p = record(row.get("payload")) if isinstance(row, dict) else {}
        expected = {"version": 1, "task": task_id, "episode": episode, "verdict": fields["verdict"],
                    "runs": 10 if fields["verdict"] == "release" else None,
                    "reason": fields["reason"], "quote": fields.get("quote"), "target": fields.get("target"),
                    "max_grants": wire["max_grants"], "request_uid": uid, "request_sha256": digest,
                    "actor_kind": "system", "actor_session": None, "actor_gateway": hub.ident.gateway,
                    "authority_kind": "spend_reviewer", "authority_ref": "kanban-spend-reviewer"}
        if (not isinstance(proof, dict) or proof.get("board") != board
                or proof.get("table") != "task_events" or proof.get("uid") != uid
                or not isinstance(row, dict) or row.get("uid") != uid or row.get("task_id") != task_id
                or row.get("kind") != "spend_review" or row.get("version") != 1
                or row.get("deleted_at") is not None or row.get("origin_gateway") != hub.ident.gateway
                or any(k not in p or p[k] != value for k, value in expected.items())):
            raise ValueError("canonical spend review receipt mismatch")
        return p
    finally:
        hub.close()


def review(conn, task_id, episode, verdict, ctx):
    from hermes_cli import kanban_spend_reviewer as reviewer
    from hermes_cli import kanban_spend_decision as decision
    from hermes_cli.kanban_wake_check import foreign_copy
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_spend_review_request as journal
    frozen = journal.get(conn, task_id, episode)
    try:
        stop = decision.pending_episode(conn, task_id)
        enabled, cap = reviewer.settings()
        allowed = (stop is not None and reviewer.episode_of(stop) == episode and not fenced(conn, task_id)
                   and not foreign_copy(conn, task_id) and not reviewer._disposed(conn, task_id, episode)
                   and not reviewer._human_after_stop(conn, task_id, stop) and enabled
                   and effective_cause(conn, task_id, stop) in CAUSES
                   and grant_count(conn, task_id) < min(cap, 2))
        if reviewer.state(conn, task_id, episode) != reviewer.STARTED:
            return "moot"
        if frozen:
            receipt = canonical(conn, task_id, episode, {}, cap, resend=allowed)
        else:
            if not allowed or effective_cause(conn, task_id, stop) != ctx.get("cause"):
                raise ValueError("stop changed or another hold is active")
            fields = validate(verdict, ctx)
            receipt = canonical(conn, task_id, episode, fields, cap)
    except Refused as exc:
        result = reviewer.escalate(conn, task_id, episode, str(exc))
        if result == "moot":
            with kb.write_txn(conn):
                kb._append_event(conn, task_id, reviewer.ESCALATED, {"episode": episode, "reason": str(exc)})
        return result
    except (ValueError, OSError, RuntimeError) as exc:
        if journal.get(conn, task_id, episode):
            reviewer.logger.warning("kanban: frozen spend review %s awaits reconciliation: %s", task_id, exc)
            return "pending"
        return reviewer.escalate(conn, task_id, episode, f"caused-stop review not confirmed: {exc}")
    if receipt["verdict"] == "escalate":
        return reviewer.escalate(conn, task_id, episode, receipt["reason"])
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, reviewer.SETTLED, {"episode": episode,
                         "receipt_uid": receipt["request_uid"], "verdict": receipt["verdict"]})
    return "settled"  # canonical effect confirmed; normal sync, not this helper, projects clearance
