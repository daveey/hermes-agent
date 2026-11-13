"""Exact delivery proof rides the existing operator result, not a second ledger."""
import hashlib
import json

import pytest

from hermes_cli import kanban_db as kb

from hermes_cli import kanban_supervisor_requests as requests
from tests.hermes_cli import test_kanban_operator_call as helpers

fleet = helpers.fleet


def test_completion_cannot_claim_delivery_with_unrelated_comment(fleet):
    f = fleet
    kb.add_comment(f.h, f.target, "someone-else", "unrelated")
    with pytest.raises(ValueError, match="exact operator delivery receipt"):
        kb.complete_task(f.w, f.caller, expected_run_id=f.run_id,
                         summary=f"Posted the findings to {f.target}.")
    assert kb.get_task(f.w, f.caller).status == "running"


def _receipt(proof):
    return {"completion_receipt": {"version": 1, "items": [{
        "scope": "cross-card findings", "disposition": "delivered", "remaining": [],
        "evidence": [{"ref": "operator request", "observed_at": "2026-11-12T00:00:00Z",
                      "verification": "independently_checked", "operator_delivery": proof}]}]}}


def test_exact_receipt_admitted_at_canonical_completion(fleet):
    f = fleet
    submitted = helpers._submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "exact"})
    helpers._round(f)
    proof = requests.get_request(f.w, submitted["request_id"])["result"][0]["operator_delivery"]
    assert kb.complete_task(f.w, f.caller, expected_run_id=f.run_id,
                            summary=f"Posted the findings to {f.target}.", metadata=_receipt(proof))
    assert kb.get_task(f.w, f.caller).status == "done"


def _delivered(f):
    submitted = helpers._submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "exact"})
    helpers._round(f)
    return requests.get_request(f.w, submitted["request_id"])["result"][0]["operator_delivery"]


@pytest.mark.parametrize("field,value", [
    ("content_sha256", "0" * 64), ("caller_gateway", "impostor"),
    ("request_uid", "wrong-request"), ("target_task_id", "t_0000000000000000"),
    ("caller_run_uid", "old-run"), ("executor_gateway", "impostor"), ("board", "other"),
])
def test_forged_replica_receipt_rejected(fleet, field, value):
    f = fleet
    proof = _delivered(f)
    proof[field] = value
    # A forged replica and receipt agree; the independent Board copy does not.
    raw = f.rep.execute("SELECT payload FROM task_events WHERE uid=?", (proof["result_event_uid"],)).fetchone()[0]
    payload = json.loads(raw)
    payload["operator_delivery"] = {k: v for k, v in proof.items() if k != "result_event_uid"}
    if field == "executor_gateway":
        payload["executor_gateway"] = value
        f.origin[proof["result_event_uid"]] = value
    with kb.write_txn(f.rep):
        f.rep.execute("UPDATE task_events SET payload=? WHERE uid=?", (json.dumps(payload), proof["result_event_uid"]))
    with pytest.raises(ValueError, match="completion_receipt"):
        kb.complete_task(f.w, f.caller, expected_run_id=f.run_id, metadata=_receipt(proof))
    assert kb.get_task(f.w, f.caller).status == "running"


@pytest.mark.parametrize("outcome", ["forwarded", "failed", "rejected"])
def test_nonterminal_or_failed_request_is_not_delivery(fleet, outcome):
    f = fleet
    proof = _delivered(f)
    with kb.write_txn(f.w):
        f.w.execute("UPDATE operator_calls SET outcome=? WHERE request_uid=?", (outcome, proof["request_uid"]))
    with pytest.raises(ValueError, match="completion_receipt"):
        kb.complete_task(f.w, f.caller, expected_run_id=f.run_id, metadata=_receipt(proof))


@pytest.mark.parametrize("change", ["args", "actor", "request", "target", "home"])
def test_canonical_request_must_bind_delivery(fleet, change):
    f = fleet
    proof = _delivered(f)
    uid = f.w.execute("SELECT request_event_uid FROM operator_calls WHERE request_uid=?", (proof["request_uid"],)).fetchone()[0]
    payload = json.loads(f.canonical[uid]["payload"])
    container, key, value = {
        "args": (payload["args"], "text", "different content"),
        "actor": (payload["caller"], "gateway", "impostor"),
        "request": (payload, "request_uid", "other"),
        "target": (payload, "target_task_id", "t_0000000000000000"),
        "home": (payload, "home", "impostor"),
    }[change]
    container[key] = value
    f.canonical[uid]["payload"] = json.dumps(payload)
    with pytest.raises(ValueError, match="completion_receipt"):
        kb.complete_task(f.w, f.caller, expected_run_id=f.run_id, metadata=_receipt(proof))


@pytest.mark.parametrize("field,value", [
    ("outcome", "failed"), ("outcome", "pending"), ("request_uid", "wrong"),
    ("action", "move"), ("executor_gateway", "impostor"), ("caller", {}),
    ("operator_delivery", {}),
])
def test_canonical_result_must_attest_exact_application(fleet, field, value):
    f = fleet
    proof = _delivered(f)
    row = f.canonical[proof["result_event_uid"]]
    payload = json.loads(row["payload"])
    payload[field] = value
    row["payload"] = json.dumps(payload)
    with pytest.raises(ValueError, match="completion_receipt"):
        kb.complete_task(f.w, f.caller, expected_run_id=f.run_id, metadata=_receipt(proof))


@pytest.mark.parametrize("field,value", [
    ("uid", "other"), ("task_id", "t_0000000000000000"), ("kind", "commented"),
    ("origin_gateway", "impostor"), ("deleted_at", 1), ("payload", "[]"),
])
def test_canonical_event_identity_required(fleet, field, value):
    f = fleet
    proof = _delivered(f)
    uid = proof["result_event_uid"]
    if field == "origin_gateway":
        f.origin[uid] = value
    else:
        f.canonical[uid][field] = value
    with pytest.raises(ValueError, match="completion_receipt"):
        kb.complete_task(f.w, f.caller, expected_run_id=f.run_id, metadata=_receipt(proof))


def test_rejected_disposition_cannot_prove_delivery(fleet):
    f = fleet
    proof = _delivered(f)
    metadata = _receipt(proof)
    metadata["completion_receipt"]["items"][0]["disposition"] = "rejected"
    with pytest.raises(ValueError, match="completion_receipt"):
        kb.complete_task(f.w, f.caller, expected_run_id=f.run_id,
                         summary=f"Posted findings to {f.target}.", metadata=metadata)


@pytest.mark.parametrize("field,value", [("body", "changed"), ("author", "other"), ("origin_gateway", "other")])
def test_executor_readback_rejects_changed_comment(fleet, field, value):
    from hermes_cli.kanban_operator_delivery import applied_proof
    f = fleet
    proof = _delivered(f)
    uid = f.w.execute("SELECT request_event_uid FROM operator_calls WHERE request_uid=?", (proof["request_uid"],)).fetchone()[0]
    request = json.loads(f.canonical[uid]["payload"])
    with kb.write_txn(f.h):
        f.h.execute(f"UPDATE task_comments SET {field}=? WHERE id=?", (value, proof["comment_id"]))
    with pytest.raises(ValueError, match="operator delivery"):
        applied_proof(f.h, request, "hub")


@pytest.mark.parametrize("field,value", [("comment_id", True), ("comment_id", 0), ("content_sha256", "a" * 16),
                                         ("extra", "x"), ("caller_gateway", ""), ("board", [])])
def test_malformed_delivery_schema_rejected(fleet, field, value):
    f = fleet
    proof = _delivered(f)
    proof[field] = value
    with pytest.raises(ValueError, match="completion_receipt"):
        kb.complete_task(f.w, f.caller, expected_run_id=f.run_id, metadata=_receipt(proof))


def test_applied_result_carries_exact_delivery_identity(fleet):
    f = fleet
    text = "Exact payload: café\nsecond line"
    submitted = helpers._submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": text})
    helpers._round(f)
    result = requests.get_request(f.w, submitted["request_id"])["result"][0]
    proof = result.get("operator_delivery")
    assert proof, "applied result lost exact delivery proof"
    assert proof["content_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert proof["target_task_id"] == f.target
    assert proof["caller_task_id"] == f.caller
    assert proof["caller_run_uid"] == f.w.execute("SELECT uid FROM task_runs WHERE id=?", (f.run_id,)).fetchone()[0]
    assert proof["executor_gateway"] == "hub"
    row = f.rep.execute("SELECT task_id, payload FROM task_events WHERE uid=?", (proof["result_event_uid"],)).fetchone()
    assert row[0] == f.target
    payload = json.loads(row[1])
    assert payload["request_uid"] == proof["request_uid"]
    assert payload["operator_delivery"] == {k: v for k, v in proof.items() if k != "result_event_uid"}
    comment = f.h.execute("SELECT body, author FROM task_comments WHERE id=?", (proof["comment_id"],)).fetchone()
    assert tuple(comment) == (text, f"worker-5/{f.caller}")


@pytest.mark.parametrize("summary", [
    "Did not post the findings to {t}.", "Queued a comment to {t}; the home has not applied it yet.",
    "Will relay the findings to {t} once the home applies it.", "The comment to {t} failed and was not sent.",
    "The findings weren't posted to {t}.", "Haven't sent anything to {t}.",
])
def test_non_delivery_wording_is_not_a_claim(fleet, summary):
    f = fleet
    helpers._submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "queued"})
    assert kb.complete_task(f.w, f.caller, expected_run_id=f.run_id, summary=summary.format(t=f.target))


def test_pending_request_cannot_be_claimed_as_delivered(fleet):
    f = fleet
    helpers._submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "pending"})
    with pytest.raises(ValueError, match="exact operator delivery receipt"):
        kb.complete_task(f.w, f.caller, expected_run_id=f.run_id, summary=f"Posted the findings to {f.target}.")
    assert kb.get_task(f.w, f.caller).status == "running"


def test_local_card_comment_claim_stays_outside_operator_gate(fleet):
    f = fleet
    local = f.card(f.w, "local sibling")
    kb.add_comment(f.w, local, "default", "direct local comment")
    assert kb.complete_task(f.w, f.caller, expected_run_id=f.run_id, summary=f"Commented the findings on {local}.")


def test_own_synced_comment_on_foreign_card_stays_outside_operator_gate(fleet):
    # A direct hub comment (not an operator call) syncs back authored "<gateway>/<caller>".
    # Real-row replay: 5 of 261 done cards on worker-3 claimed such deliveries.
    f = fleet
    kb.add_comment(f.rep, f.target, f"worker-5/{f.caller}", "findings")
    assert kb.complete_task(f.w, f.caller, expected_run_id=f.run_id, summary=f"Posted the findings to {f.target}.")


@pytest.mark.parametrize("author", ["worker-5/t_00000000deadbeef", "worker-9/{c}", "default"])
def test_other_authors_synced_comment_is_not_own_delivery(fleet, author):
    f = fleet
    kb.add_comment(f.rep, f.target, author.format(c=f.caller), "findings")
    with pytest.raises(ValueError, match="exact operator delivery receipt"):
        kb.complete_task(f.w, f.caller, expected_run_id=f.run_id, summary=f"Posted the findings to {f.target}.")


def test_own_synced_comment_never_replaces_a_filed_request_receipt(fleet):
    f = fleet
    helpers._submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "pending"})
    kb.add_comment(f.rep, f.target, f"worker-5/{f.caller}", "findings")
    with pytest.raises(ValueError, match="exact operator delivery receipt"):
        kb.complete_task(f.w, f.caller, expected_run_id=f.run_id, summary=f"Posted the findings to {f.target}.")


def test_unreachable_board_refuses_delivery_proof_cleanly(fleet, monkeypatch):
    from hermes_cli import kanban_operator_call as calls
    f = fleet
    proof = _delivered(f)

    def down(*_a, **_k):
        raise OSError("board unreachable")
    monkeypatch.setattr(calls, "_evidence", down)
    with pytest.raises(ValueError, match="canonical Board evidence unavailable"):
        kb.complete_task(f.w, f.caller, expected_run_id=f.run_id, metadata=_receipt(proof))
    assert kb.get_task(f.w, f.caller).status == "running"


@pytest.mark.parametrize("summary", [
    "Delivered the note to {t} with no errors.", "Posted the findings to {t} once the review finished.",
    "Sent the summary to {t} so it may be closed.", "Relayed the findings to {t}, if anyone asks.",
])
def test_negation_outside_the_claim_verb_still_needs_a_receipt(fleet, summary):
    """A negation/intent word elsewhere in the clause does not govern the delivery verb (fail closed)."""
    f = fleet
    helpers._submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "pending"})
    with pytest.raises(ValueError, match="exact operator delivery receipt"):
        kb.complete_task(f.w, f.caller, expected_run_id=f.run_id, summary=summary.format(t=f.target))
    assert kb.get_task(f.w, f.caller).status == "running"


@pytest.mark.parametrize("text,claimed", [
    ("Delivered the note to t_0123456789abcdef with no errors.", True),
    ("The note to t_0123456789abcdef was not posted.", False),
    ("Comment to t_0123456789abcdef queued, not yet posted.", False),
    ("The comment to t_0123456789abcdef failed and was not sent.", False),
    ("The comment to t_0123456789abcdef is queued and will be posted later.", False),
    ("Never actually got delivered to t_0123456789abcdef.", False),
    ("No probes were found, so nothing was commented on t_0123456789abcdef.", False),
    ("Ship filed no card, so I posted both IDs on t_0123456789abcdef.", True),
    ("Posted to t_0123456789abcdef; no errors.", True),
    # ':' / dashes / '(' end the governing span like a comma does.
    ("No longer pending: posted to t_0123456789abcdef.", True),
    ("No longer pending — posted to t_0123456789abcdef.", True),
    ("No longer pending – posted to t_0123456789abcdef.", True),
    ("Was pending (posted to t_0123456789abcdef since).", True),
    ("Comment to t_0123456789abcdef: not posted.", False),
    ("Comment to t_0123456789abcdef: queued.", False),
    ("Comment to t_0123456789abcdef — failed and was not sent.", False),
    ("Comment to t_0123456789abcdef (not posted).", False),
    # A standalone spaced ASCII hyphen is a dash too; an intra-word hyphen is not.
    ("No longer pending - posted to t_0123456789abcdef.", True),
    ("Comment to t_0123456789abcdef - not posted.", False),
    ("Comment to t_0123456789abcdef - queued.", False),
    ("Comment to t_0123456789abcdef - failed and was not sent.", False),
    ("The note to t_0123456789abcdef was not re-posted.", False),
    ("Re-posted the note to t_0123456789abcdef.", True),
    # A future/infinitive form ("to be posted", "will be sent") is never a claim, across any boundary.
    ("Queued to be posted to t_0123456789abcdef.", False),
    ("Queued (to be posted to t_0123456789abcdef).", False),
    ("Queued: to be posted to t_0123456789abcdef.", False),
    ("Queued - to be posted to t_0123456789abcdef.", False),
    ("Queued: will be sent to t_0123456789abcdef.", False),
    ("Done (to be delivered to t_0123456789abcdef).", False),
    ("Has been posted to t_0123456789abcdef.", True),
    ("Posted to t_0123456789abcdef as it should be.", True),
    # A spaced ASCII double hyphen and U+2212 are dashes as well.
    ("Not done -- posted to t_0123456789abcdef.", True),
    ("Not done − posted to t_0123456789abcdef.", True),
    ("Comment to t_0123456789abcdef -- not posted.", False),
    # "be"/"being" govern from inside the window too: adverbs and the progressive are not claims.
    ("Queued: to be later posted to t_0123456789abcdef.", False),
    ("Queued: to be eventually sent to t_0123456789abcdef.", False),
    ("The note to t_0123456789abcdef is being posted.", False),
    ("Queued to be soon posted to t_0123456789abcdef.", False),
    ("The note to t_0123456789abcdef is being quickly posted.", False),
    # Windowed be/being governs only across an adverb gap: an unrelated "to be safe" or "being thorough" before
    # an affirmative claim leaves it a claim (fail closed).
    ("To be safe I posted the summary to t_0123456789abcdef.", True),
    ("Being thorough I posted to t_0123456789abcdef.", True),
    ("To be sure, posted to t_0123456789abcdef.", True),
    # A spaced dash run of any length is a boundary, and a hyphen glued to either side of the verb splits too.
    ("Not done --- posted to t_0123456789abcdef.", True),
    ("Not done ---- posted to t_0123456789abcdef.", True),
    ("Not done -posted to t_0123456789abcdef.", True),
    ("Not done- posted to t_0123456789abcdef.", True),
    ("Comment to t_0123456789abcdef --- not posted.", False),
    ("Comment to t_0123456789abcdef -not posted.", False),
    ("The note to t_0123456789abcdef was never re-posted.", False),
    # A hyphen-joined prefix is part of the verb, so be/being still governs it; alone it stays a claim.
    ("The note is to be re-posted to t_0123456789abcdef.", False),
    ("The note is to be cross-posted to t_0123456789abcdef.", False),
    ("The note to t_0123456789abcdef is being soon re-sent.", False),
    ("Cross-posted the note to t_0123456789abcdef.", True),
    # Common -ly nouns, verbs and adjectives are no adverbs: be/being does not govern across them (fail closed).
    ("Being early posted to t_0123456789abcdef.", True),
    ("Being family posted to t_0123456789abcdef.", True),
    ("To be supply posted to t_0123456789abcdef.", True),
    ("Queued to be manually re-posted to t_0123456789abcdef.", False),
    # A run of 2+ hyphens is a dash even glued on both sides; a single intra-word hyphen is not.
    ("Not done--posted to t_0123456789abcdef.", True),
    ("Not done---posted to t_0123456789abcdef.", True),
    ("Comment to t_0123456789abcdef--not posted.", False),
])
def test_claimed_targets_scopes_negation_to_the_verb(text, claimed):
    from hermes_cli.kanban_operator_delivery import claimed_targets
    assert claimed_targets(text) == ({"t_0123456789abcdef"} if claimed else set())


def test_summary_edit_cannot_add_an_unproved_delivery_claim(fleet):
    """kanban edit rewrites a done run's summary: a new gated claim needs the same receipt."""
    from hermes_cli.kanban_completion_receipt import ReceiptEditRefused
    f = fleet
    helpers._submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "pending"})
    assert kb.complete_task(f.w, f.caller, expected_run_id=f.run_id, summary=f"Queued a note for {f.target}.")
    with pytest.raises(ReceiptEditRefused, match="exact operator delivery receipt"):
        kb.edit_task(f.w, f.caller, result="r", summary=f"Posted the findings to {f.target}.")
    run = f.w.execute("SELECT summary FROM task_runs WHERE task_id=? AND outcome='completed'",
                      (f.caller,)).fetchone()
    assert run[0] == f"Queued a note for {f.target}."
    # Controls: non-claim wording still edits.
    assert kb.edit_task(f.w, f.caller, result="r", summary=f"The note to {f.target} is queued, not yet posted.")


def test_summary_edit_keeps_a_claim_the_admitted_receipt_proves(fleet):
    f = fleet
    proof = _delivered(f)
    assert kb.complete_task(f.w, f.caller, expected_run_id=f.run_id,
                            summary=f"Posted the findings to {f.target}.", metadata=_receipt(proof))
    assert kb.edit_task(f.w, f.caller, result="r", summary=f"Posted the corrected findings to {f.target}.")
