"""Exact comment identity attached to the existing operator result event."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3


_CLAIM = frozenset({"posted", "sent", "delivered", "commented", "relayed"})
# A negation, queue or intent word governs a claim verb only from just before it
# ("was not posted", "queued and will be sent", "once delivered"): pending stays
# queued wording and must not need (or fake) a delivery proof. The same word
# elsewhere in the clause ("Delivered to t_X with no errors") does not undo the
# claim, so it still needs the exact receipt (fail closed).
_NOT_CLAIM = frozenset({"not", "never", "no", "nothing", "none", "neither", "nor", "without", "queued", "pending",
                        "failed", "refused", "rejected", "unable", "will", "would", "could", "should", "may",
                        "might", "if", "once", "until", "awaiting", "waiting", "yet"})
_GOVERN_WINDOW = 3  # words before the verb: "has not yet been posted", "will later be sent"
# A comma, ':', a dash, '(' or a conjunction ends the governing span: "filed no card, so I posted ..." and "No
# longer pending - posted to ..." are claims. A dash is an em/en dash, U+2212, or an ASCII hyphen run of any length
# that touches a space or the clause edge on either side ("pending - posted", "pending --- posted", "pending -posted",
# "pending- posted"), or a run of 2+ hyphens even glued on both sides ("pending--posted"); a single intra-word
# hyphen ("re-posted") is no dash.
_BOUNDARY = frozenset({",", ":", "—", "–", "−", "(", "so", "and", "but", "then", "after", "before",
                       "because", "which", "while", "that"})
# Group 1 is a word glued by one hyphen to the next ("re" in "re-posted", "cross" in "cross-posted").
_WORD = re.compile(r"([\w’']+)-(?=[\w’'])|[\w’']+|[,:—–−(]|(?<!\S)-+|-+(?!\S)|-{2,}")
_TASK_ID = re.compile(r"\bt_[0-9a-f]{8}(?:[0-9a-f]{8})?\b")
_BE = ("be", "being")
# Only adverbs may stand between be/being and the verb for it to govern ("to be soon posted", "is being quickly
# sent"); "To be safe I posted ..." and "Being thorough I posted ..." stay claims (fail closed).
_ADVERBS = frozenset({"soon", "later", "eventually", "still", "also", "again", "just", "now", "only", "first",
                      "properly", "manually", "automatically"})
# Common -ly words that are no adverbs: be/being does not govern across them ("Being early posted to t_X" is a
# claim, fail closed). Known fail-open: any other -ly non-adverb between be/being and the verb still governs.
_NOT_ADVERBS = frozenset({"reply", "supply", "apply", "comply", "imply", "multiply", "rely", "ally", "rally",
                          "family", "assembly", "anomaly", "early", "friendly", "costly", "timely", "july", "italy"})


def _is_boundary(w):
    return w in _BOUNDARY or not w.strip("-")


def _adverb(w):
    return w in _ADVERBS or (len(w) > 3 and w.endswith("ly") and w not in _NOT_ADVERBS)


def _governed(words, i, glued=frozenset()):
    lo = max(0, i - _GOVERN_WINDOW)
    for j in range(i - 1, -1, -1):
        # "to be posted", "will be sent", "is being posted", "to be later posted": an infinitive, future or
        # progressive form is never a past claim, whatever boundary cuts it from its queue word ("Queued: to be
        # posted to ..."). Known fail-open: the rare past-tense infinitive "it turned out to be posted twice to
        # t_X" is not a claim. A hyphen-joined prefix is part of the word after it ("to be re-posted").
        if words[j] in _BE:
            return True
        if j in glued:
            continue
        if not _adverb(words[j]):
            break
    for w in reversed(words[lo:i]):
        if _is_boundary(w):
            return False
        if w in _NOT_CLAIM or w.endswith(("n't", "n’t")):
            return True
    return False


def claimed_targets(text):
    """Explicit affirmative past-tense delivery claims, one clause at a time.

    A clause claims delivery when one of its claim verbs is not governed by a
    negation/intent word in the few words before it; every card id in that
    clause is then claimed.
    """
    targets = set()
    for clause in re.split(r"[.!?;\n]", text if isinstance(text, str) else ""):
        matches = list(_WORD.finditer(clause))
        words = [(m.group(1) or m.group(0)).lower() for m in matches]
        glued = frozenset(i for i, m in enumerate(matches) if m.group(1))
        if any(w in _CLAIM and not _governed(words, i, glued) for i, w in enumerate(words)):
            targets.update(_TASK_ID.findall(clause))
    return targets


def gated_claims(conn, task_id, text):
    """Claims that only an operator delivery can make true.

    A card on this host's board can be commented directly and stays outside this
    gate. A replica-only (foreign) card, or any target this card asked an
    operator call to comment on, needs the exact receipt: an unrelated comment,
    a pending request or a failed one never proves delivery.
    """
    claims = claimed_targets(text) - {task_id}
    if not claims:
        return set()
    from hermes_cli import kanban_operator_call as calls
    requested = set()
    if calls._has_log(conn):
        requested = {r[0] for r in conn.execute(
            "SELECT target_task_id FROM operator_calls WHERE side='request' AND action='comment' "
            "AND caller_task_id=?", (task_id,))}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='supervisor_requests'").fetchone():
        # Filed but not yet forwarded: still only a queued request, never delivery.
        for (raw,) in conn.execute("SELECT ops_json FROM supervisor_requests WHERE requester_task_id=?",
                                   (task_id,)):
            try:
                ops = json.loads(raw)
            except ValueError:
                continue
            requested.update(op.get("task_id") for op in ops if isinstance(op, dict)
                             and op.get("op") == "operator" and op.get("action") == "comment")
    rest = claims - requested
    if rest:
        # Migrated callers carry portable request history, not executor-local
        # ledgers. Such history can require proof but never authenticate it.
        from hermes_cli.kanban_operator_delivery_custody import requested_targets
        requested.update(requested_targets(conn, task_id))
        with calls._replica(conn) as rep:
            requested.update(requested_targets(rep, task_id))
    rest = claims - requested
    if rest:
        own = calls._own_gateway(conn)
        with calls._replica(conn) as rep:
            for t in rest:
                local = conn.execute("SELECT 1 FROM tasks WHERE id=?", (t,)).fetchone()
                row = None if local else calls._target_row(rep, t)
                if row is not None and row["created_on"] != own and not _own_synced_comment(rep, t, own, task_id):
                    requested.add(t)  # replica-only foreign card with no comment of ours synced back
    return claims & requested


def _own_synced_comment(rep, target, own, task_id):
    """A direct hub comment by this card, synced back authored ``<gateway>/<task>``.

    Only for targets with no operator request: those always need the exact
    receipt. Authorship is the binding here; content is not (no request text).
    """
    try:
        return rep.execute("SELECT 1 FROM task_comments WHERE task_id=? AND author=? LIMIT 1",
                           (target, f"{own}/{task_id}")).fetchone() is not None
    except sqlite3.Error:
        return False


def verify_local(conn, task_id, proof, *, admitted_run=None):
    """Recheck task custody, retaining the original request's caller/run identity."""
    from hermes_cli import kanban_operator_call as calls
    from hermes_cli.kanban_operator_delivery_custody import carried_request, current_run
    run = current_run(conn, task_id)
    if admitted_run is not None and run != admitted_run:
        raise ValueError("completion_receipt: operator current run changed during admission")
    row = (conn.execute("SELECT * FROM operator_calls WHERE request_uid=? AND side='request'",
                        (proof["request_uid"],)).fetchone() if calls._has_log(conn) else None)
    if row is not None and row["outcome"] != "done":
        raise ValueError("completion_receipt: operator request is pending, failed or missing")
    if row is None:
        row, request = carried_request(conn, task_id, proof)
    else:
        event = conn.execute("SELECT payload FROM task_events WHERE uid=? AND task_id=? AND kind=?",
                             (row["request_event_uid"], task_id, calls.REQUEST_KIND)).fetchone()
        if event is None:
            raise ValueError("completion_receipt: request event missing")
        request = json.loads(event[0])
    if (row["caller_task_id"] != task_id or proof["caller_task_id"] != task_id
            or row["caller_run_uid"] != proof["caller_run_uid"]):
        raise ValueError("completion_receipt: operator caller/run mismatch")
    if (row["action"] != "comment" or row["target_task_id"] != proof["target_task_id"]
            or row["board"] != proof["board"] or calls._board_slug(conn) != proof["board"]
            or row["caller_gateway"] != proof["caller_gateway"]):
        raise ValueError("completion_receipt: operator target/board/actor mismatch")
    _bind_request(request, row, proof)
    return row, request


def _bind_request(request, row, proof):
    from hermes_cli import kanban_operator_call as calls
    caller, args = request.get("caller") or {}, request.get("args") or {}
    text = args.get("text")
    if (request.get("request_uid") != proof["request_uid"] or request.get("action") != "comment"
            or request.get("target_task_id") != proof["target_task_id"]
            or request.get("board") != proof["board"] or request.get("home") != proof["executor_gateway"]
            or any(caller.get(k) != proof[p] for k, p in (
                ("task_id", "caller_task_id"), ("run_uid", "caller_run_uid"), ("gateway", "caller_gateway")))
            or not isinstance(text, str)
            or hashlib.sha256(text.encode("utf-8")).hexdigest() != proof["content_sha256"]
            or calls._digest({"action": "comment", "task_id": proof["target_task_id"], **args}) != row["args_digest"]):
        raise ValueError("completion_receipt: exact request/content binding mismatch")


def _canonical_event(board, uid, task, kind, gateway):
    from hermes_cli import kanban_operator_call as calls
    try:
        evidence = calls._evidence(board, uid)
    except Exception as exc:  # health: allow BLE001 -- unreachable Board refuses completion; retry later
        raise ValueError(f"completion_receipt: canonical Board evidence unavailable ({type(exc).__name__})") from exc
    return canonical_payload(evidence, uid, task, kind, gateway)


def canonical_payload(evidence, uid, task, kind, gateway):
    """Decode an exact authenticated event; shared by transport and completion."""
    row = evidence.get("row") if isinstance(evidence, dict) else None
    if (not isinstance(row, dict) or evidence.get("state") != "live"
            or evidence.get("table") != "task_events" or evidence.get("uid") != uid
            or row.get("uid") != uid or row.get("task_id") != task or row.get("kind") != kind
            or row.get("origin_gateway") != gateway or row.get("deleted_at") is not None):
        raise ValueError("completion_receipt: canonical event identity unavailable or mismatched")
    payload = row.get("payload")
    payload = json.loads(payload) if isinstance(payload, str) else payload
    if not isinstance(payload, dict):
        raise ValueError("completion_receipt: malformed canonical event payload")
    return payload


def verify(conn, task_id, proof):
    from hermes_cli import kanban_operator_call as calls
    from hermes_cli.kanban_operator_delivery_custody import current_run
    run = current_run(conn, task_id)
    row, local_request = verify_local(conn, task_id, proof, admitted_run=run)
    request = _canonical_event(proof["board"], row["request_event_uid"], task_id,
                               calls.REQUEST_KIND, row["caller_gateway"])
    if request != local_request:
        raise ValueError("completion_receipt: canonical request differs from submitted request")
    _bind_request(request, row, proof)
    with calls._replica(conn) as rep:
        target = calls._target_row(rep, proof["target_task_id"])
    if target is None or target["created_on"] != request["home"]:
        raise ValueError("completion_receipt: target home mismatch")
    result = _canonical_event(proof["board"], proof["result_event_uid"], proof["target_task_id"],
                              calls.RESULT_KIND, request["home"])
    if (result.get("outcome") not in ("applied", "already_applied") or result.get("action") != "comment"
            or result.get("request_uid") != proof["request_uid"]
            or result.get("executor_gateway") != request["home"]
            or result.get("caller") != {k: request["caller"].get(k) for k in (
                "gateway", "task_id", "run_id", "run_uid")}):
        raise ValueError("completion_receipt: no verified applied operator result")
    expected = {**(result.get("operator_delivery") or {}), "result_event_uid": proof["result_event_uid"]}
    if expected != proof:
        raise ValueError("completion_receipt: exact operator result/digest mismatch")
    return run, dict(row), local_request


def applied_proof(conn, payload, own):
    """Read back the actual comment, including on an idempotent replay."""
    from hermes_cli.kanban_operator_call import COMMENT_MARK
    target, uid = payload["target_task_id"], payload["request_uid"]
    caller = payload.get("caller") or {}
    for row in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC",
                            (target, COMMENT_MARK)):
        marker = json.loads(row[0])
        if marker.get("operator_request_uid") != uid:
            continue
        comment = conn.execute("SELECT body, author, origin_gateway FROM task_comments WHERE task_id=? AND id=?",
                               (target, marker["comment_id"])).fetchone()
        if comment is None or comment[0] != (payload.get("args") or {}).get("text") \
                or comment[1] != f"{caller.get('gateway')}/{caller.get('task_id')}" \
                or comment[2] != caller.get("gateway") or marker.get("operator_caller") != {
                    k: caller.get(k) for k in ("gateway", "task_id", "run_id", "run_uid")}:
            raise ValueError("operator delivery comment identity mismatch")
        return {"request_uid": uid, "target_task_id": target, "board": payload["board"],
                "caller_task_id": caller["task_id"], "caller_run_uid": caller["run_uid"],
                "caller_gateway": caller["gateway"], "executor_gateway": own,
                "comment_id": marker["comment_id"],
                "content_sha256": hashlib.sha256(comment[0].encode("utf-8")).hexdigest()}
    raise ValueError("operator delivery comment missing")
