"""Validate independently fetched courier records, not JSON submitted as proof.

The read_request seam is private and must use the enrolled gateway's authenticated
Hub API. This module does not expose a tool/CLI that can manufacture user evidence.
"""
from __future__ import annotations

from hermes_cli.kanban_card_answer import AnswerRefused


def verify(answer: dict, *, request_id: str, proof_id: str, owner_gateway: str, read_request) -> dict:
    """Bind the immutable source request to an owner-requested source readback.

    An enrolled source can submit a request, but only its operation-store reader
    produces the completed proof. Failed/pending proof never authorizes a write.
    """
    original = read_request(request_id)
    proof = read_request(proof_id)
    fields = {"ownerGateway", "ownerProfile", "board", "task", "generation", "choice", "operationId"}
    if (not isinstance(answer, dict) or set(answer) != fields
            or any(not isinstance(v, str) or not v for v in answer.values())):
        raise AnswerRefused("source_request_invalid")
    if not isinstance(original, dict) or not isinstance(proof, dict):
        raise AnswerRefused("source_proof_unavailable")
    args = original.get("args")
    if not isinstance(args, dict) or set(args) != {"answer", "source"}:
        raise AnswerRefused("source_request_invalid")
    source = args["source"]
    if (not isinstance(source, dict) or set(source) != {"gateway", "profile", "session"}
            or any(not isinstance(v, str) or not v or "/" in v for v in source.values())):
        raise AnswerRefused("source_request_invalid")
    if (original.get("id") != request_id or original.get("coll") != "tasks"
            or original.get("action") != "card-answer"
            or original.get("from") != "gw:" + source["gateway"]
            or original.get("to_gw") != owner_gateway or answer.get("ownerGateway") != owner_gateway
            or original.get("key") != f"{answer['ownerProfile']}/{answer['board']}/{answer['task']}"
            or args["answer"] != answer):
        raise AnswerRefused("source_request_mismatch")
    expected = {"answer": answer, "source": source, "requestId": request_id}
    if (proof.get("id") != proof_id or proof.get("coll") != "sessions"
            or proof.get("action") != "card-answer-proof" or proof.get("from") != "gw:" + owner_gateway
            or proof.get("to_gw") != source["gateway"]
            or proof.get("key") != f"{source['profile']}/{source['session']}"
            or proof.get("args") != expected):
        raise AnswerRefused("source_proof_mismatch")
    if proof.get("state") != "done":
        raise AnswerRefused("source_proof_unavailable")
    result = proof.get("result")
    wanted = {**expected, "gateway": source["gateway"], "recorded": True}
    if not isinstance(result, dict) or result != wanted or result.get("recorded") is not True:
        raise AnswerRefused("unverified_user_decision")
    return result
