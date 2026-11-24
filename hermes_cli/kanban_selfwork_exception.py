"""One single-use admission exception for one rejected self-work request. [daveey]

``kanban.self_work.freeze_guards`` refused ``TARGET.request_id`` (``self_work_guard_frozen``). This module lets
the intake home admit that same request once, with its identity and intent unchanged, after David answers a
home-filed ask in Desktop. The freeze stays on, the courier's rejection record is never edited, and nothing here
applies to any other request: the target is a constant, not an argument.

Steps, each its own transaction on the home's primary board:

1. :func:`offer`: the home reads the original request and its rejection from the Hub with its own enrolled
   credential, checks both against ``TARGET``'s hashes, and replaces the owner card's ask with one it wrote. The
   ask carries ``STAMP`` and the home's ``OFFERED`` event carries the same stamp and the ask's digest.
2. The grant: ``kanban_card_answer.apply`` (reached only through ``kanban_card_answer_runtime.owner``, which
   verifies the recorded Desktop operation at its source) calls :func:`granting` for a default Unblock. A valid
   offer puts ``GRANT`` on that click's ``unblocked`` event beside the ordinary ``card_answer`` receipt. The card
   returns to work; nothing is admitted in that transaction.
3. :func:`reconsider`: the home reads the original again and runs it through the ordinary receiver chain with a
   private :class:`Capability`. ``kanban_selfwork_intake.admit`` calls :func:`spend` inside its write
   transaction; that is the only place the grant is consumed and the only refusal it replaces is the freeze's
   "no open guard owner". An open owner is still reused instead of creating a card.

Trust limit, inherited from the card-answer boundary and not improved here: the proof of David's click is the
source install's ActionStore row. A process with that user's file access on that machine can write such a row.

Entry is the module CLI only, run in the home profile; no tool or ``hermes`` subcommand is registered and no
argument selects a request::

    python -m hermes_cli.kanban_selfwork_exception offer | reconsider | status
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import sys
import time
from typing import NamedTuple, Optional

from hermes_cli import kanban_selfwork as policy
from hermes_cli.kanban_selfwork_guards import GuardFrozen

STAMP = "selfwork_exception_target"  # human_request field only offer() sets; validate_human_request drops it
GRANT = "selfwork_exception_grant"  # on the home's own ``unblocked`` event of the verified Desktop click
OFFERED, SPENT = "selfwork_exception_offered", "selfwork_exception_spent"
ENVELOPE = ("id", "from", "to_gw", "coll", "key", "action", "args", "base_version")  # selfwork-envelope-v1
TTL = 72 * 3600


class Target(NamedTuple):
    request_id: str
    intent_sha256: str  # canonical ENVELOPE fields of the Board record, action still ``selfwork-create``
    rejection_sha256: str  # canonical ``result`` of that record
    owner_task: str  # the card whose ask carries the offer
    approval: str


TARGET = Target(
    "sw-6da53461a17f77646652e804900721ec3f9dd180001ddae22f1ec495eb51ac56",
    "8c795046974eba853e07ce72ebe74a11b040ad7c04f367897c8cf49682b79aea",
    "358b8c87238544ba33eaf63df1c8c801f1563751a7683d55f2addd2dcdc2f545",
    "t_01a20e50e6d56778",
    "9bde97c4-1e0e-4503-b744-8459c110fcba/1",
)


class Refused(GuardFrozen):
    """The exception does not apply, so the freeze's refusal stands: definitive, nothing written."""


class _Probed(Refused):
    """offer()'s dry run reached admission; its transaction rolls back."""


class Capability:
    """Private proof that :func:`reconsider` or :func:`offer` authenticated the original for this one call.

    It travels as its own registry keyword, never inside an envelope, an intent or the intake journal."""
    __slots__ = ("bound", "grant", "probe", "sender", "tenant")

    def __init__(self, sender: str, grant: Optional[dict]):
        self.sender, self.grant, self.probe, self.tenant, self.bound = sender, grant, grant is None, None, False

    def bind(self, request: dict) -> None:
        """The envelope the receiver is about to normalise is the one the home hashed."""
        if _sha({k: request.get(k) for k in ENVELOPE}) != TARGET.intent_sha256 or request.get("from") != self.sender:
            raise ValueError("self_work_exception_intent_changed")
        self.bound = True


def bind(exception, request: dict) -> None:
    """Receiver entry: the capability is the home's own and names the envelope about to be normalised."""
    if type(exception) is not Capability:
        raise ValueError("self_work_request_invalid")
    exception.bind(request)


def require(exception) -> Capability:
    if type(exception) is not Capability or not exception.bound:
        raise policy.SelfWorkError("self-work exception capability is not the home's")
    return exception


def _canon(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=True)


def _sha(value) -> str:
    return hashlib.sha256(_canon(value).encode()).hexdigest()


def _strict(raw: bytes):
    """JSON with no duplicate keys and no non-finite numbers: either would make the hashed bytes ambiguous."""
    def pairs(items):
        keys = [k for k, _ in items]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate key")
        return dict(items)

    def constant(name):
        raise ValueError(name)
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)


def original(records) -> dict:
    """The exact rejected request as the Hub holds it now, or a refusal. Never rebuilt from a caller's copy."""
    from hermes_cli.kanban_card_answer import AnswerRefused
    try:
        record = _strict(records.raw(TARGET.request_id)).get("request")
    except AnswerRefused as exc:  # missing or pruned: no reconstruction from prose
        raise Refused(f"original request unavailable ({exc})") from None
    except (ValueError, UnicodeError, AttributeError):
        raise Refused("original request record is not unambiguous JSON") from None
    if not isinstance(record, dict) or any(k not in record for k in ENVELOPE) or record["id"] != TARGET.request_id:
        raise Refused("original request record is incomplete")
    envelope = {k: record[k] for k in ENVELOPE}
    try:
        same = _sha(envelope) == TARGET.intent_sha256
        rejected = record.get("state") == "rejected" and _sha(record.get("result")) == TARGET.rejection_sha256
    except (TypeError, ValueError):
        same = rejected = False
    if not same:
        raise Refused("original request intent changed")
    if not rejected:
        raise Refused("original rejection changed")
    return envelope


def _home(conn) -> str:
    """This board is the configured self-work intake home's own primary board; returns its gateway."""
    from hermes_cli import kanban_card_answer as ca
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_selfwork_owner as owner
    gateway = owner._runtime()[1]
    board = kb.get_current_board()
    try:
        owner.validate_home(conn, {"owner": {"gateway": gateway, "profile": owner._runtime()[2], "board": board},
                                   "origin": {}}, board)
        ca._owner(conn, TARGET.owner_task, gateway, require_primary=True)
    except (policy.SelfWorkError, ca.AnswerRefused) as exc:
        raise Refused(f"not the intake home's primary board ({exc})") from None
    return gateway


def _own(conn, kind: str) -> list[dict]:
    from hermes_cli.kanban_release import own_events
    return own_events(conn, TARGET.owner_task, (kind,))


def _bound(stamp) -> bool:
    return (isinstance(stamp, dict) and set(stamp) == {"version", "task", "request_id", "intent_sha256",
                                                       "rejection_sha256", "tenant", "scope", "approval",
                                                       "expires_at", "offer_uid"}
            and (stamp["version"], stamp["task"], stamp["request_id"], stamp["intent_sha256"],
                 stamp["rejection_sha256"], stamp["approval"])
            == (1, TARGET.owner_task, TARGET.request_id, TARGET.intent_sha256, TARGET.rejection_sha256,
                TARGET.approval)
            and type(stamp["expires_at"]) is int and isinstance(stamp["offer_uid"], str))


def _spent(conn, offer_uid: str) -> bool:
    return any(e["payload"].get("offer_uid") == offer_uid for e in _own(conn, SPENT))


def _grant(conn, offer_uid: str) -> Optional[dict]:
    """The grant David's verified click recorded for this offer: on this board's own ``unblocked`` event, with
    the complete card-answer receipt and its decision comment, for a stamp this board's own offer event holds."""
    from hermes_cli import kanban_card_answer as ca
    offers = [e["payload"].get(STAMP) for e in _own(conn, OFFERED)]
    for event in _own(conn, "unblocked"):
        grant, receipt = event["payload"].get(GRANT), event["payload"].get(ca.FIELD)
        if (not isinstance(grant, dict) or not isinstance(receipt, dict) or grant.get("offer_uid") != offer_uid
                or not _bound(grant.get("stamp")) or grant["stamp"] not in offers
                or grant["stamp"]["offer_uid"] != offer_uid):
            continue
        request = {k: receipt.get(k) for k in ("ownerGateway", "ownerProfile", "board", "task", "generation",
                                               "choice", "operationId")}
        if (request["choice"] == "unblock" and grant.get("operationId") == request["operationId"]
                and all(isinstance(v, str) for v in request.values())
                and ca._valid_receipt(conn, TARGET.owner_task, request, ca.digest(request), "unblocked", receipt)):
            return grant
    return None


def granting(conn, task: str, ask, *, now: Optional[int] = None) -> Optional[dict]:
    """The stamp a default Unblock on ``ask`` grants, or None when the click is an ordinary unblock.

    None for any ask this board's own newest offer event does not hold in full: an agent-written ask with the
    same text, a stamp another gateway wrote into the synced column, an event another gateway pushed. A real
    offer that expired or was already answered refuses the click instead of unblocking without a grant."""
    from hermes_cli.kanban_card_answer import AnswerRefused
    from hermes_cli.kanban_release import STAMP as RELEASE, ask_digest
    stamp = ask.get(STAMP) if isinstance(ask, dict) else None
    if task != TARGET.owner_task or not _bound(stamp):
        return None
    offers = _own(conn, OFFERED)
    if (not offers or offers[0]["payload"].get(STAMP) != stamp
            or offers[0]["payload"].get("ask_sha256") != ask_digest(ask)):
        return None
    if RELEASE in ask:
        raise AnswerRefused("selfwork_exception_offer_invalid")
    if (int(time.time()) if now is None else now) >= stamp["expires_at"]:
        raise AnswerRefused("selfwork_exception_offer_expired")
    if _spent(conn, stamp["offer_uid"]) or _grant(conn, stamp["offer_uid"]) is not None:
        raise AnswerRefused("selfwork_exception_offer_used")
    return stamp


def same_offer(conn, fresh, current, stamp) -> bool:
    """card-answer's fence, under the write lock: the ask is the one answered and, when the click grants, its
    offer is still this board's own, live and unanswered."""
    return fresh == current and (stamp is None or granting(conn, current["task"], current["ask"]) == stamp)


def grant_fields(stamp, request: dict, fingerprint: str, origin: str) -> dict:
    """What a granting click adds to its ``unblocked`` event, beside the ``card_answer`` receipt."""
    if stamp is None:
        return {}
    return {GRANT: {"offer_uid": stamp["offer_uid"], "stamp": stamp, "originGateway": origin,
                    "operationId": request["operationId"], "fingerprint": fingerprint}}


def check(conn, exception, *, request_id: str, now: int) -> dict:
    """Read-only: the capability still names a live, unspent grant for this request. Called at preflight and
    again by :func:`spend` under admission's write lock."""
    cap = require(exception)
    if request_id != TARGET.request_id:
        raise Refused("exception names another request")
    if cap.probe:
        return {}
    grant = _grant(conn, cap.grant["offer_uid"])
    if grant is None or grant != cap.grant:
        raise Refused("no verified Desktop grant for this request")
    if now >= grant["stamp"]["expires_at"]:
        raise Refused("exception grant expired")
    if _spent(conn, grant["offer_uid"]):
        raise Refused("exception grant already used")
    return grant


def spend(conn, exception, *, request_id: str, tenant, now: int) -> None:
    """Consume the grant. Caller is ``kanban_selfwork_intake.admit`` inside its write transaction, so a later
    failure there rolls this back and two admissions cannot both pass :func:`check`."""
    from hermes_cli import kanban_db as kb
    if not conn.in_transaction:
        raise policy.SelfWorkError("self-work exception is spent only inside admission")
    grant = check(conn, exception, request_id=request_id, now=now)
    _home(conn)
    if exception.probe:
        exception.tenant = (tenant,)
        raise _Probed("probe")
    if tenant != grant["stamp"]["tenant"]:
        raise Refused("tenant_drift: admission would use another tenant than the one granted")
    kb._append_event(conn, TARGET.owner_task, SPENT, {
        "offer_uid": grant["offer_uid"], "request_id": request_id, "operationId": grant["operationId"],
        "tenant": tenant, "at": now})


def _receive(envelope: dict, grant: Optional[dict]) -> tuple[dict, Capability]:
    from hermes_cli.kanban_selfwork_receiver import receive
    cap = Capability(envelope["from"], grant)
    return receive(envelope, exception=cap), cap


def _ask(envelope: dict, tenant) -> dict:
    from hermes_cli.kanban_human_request import validate_human_request
    create = envelope["args"]["create"]
    title, declared = str(create.get("title") or "")[:200], create["self_work"]
    return validate_human_request({
        "action": (f"Grant one admission exception for the rejected request \"{title}\" "
                   f"({TARGET.request_id}), for accounting recovery only. Unblock = the home admits that same "
                   "request once, unchanged, and this card goes back to work; the guard freeze stays on. "
                   "Archive = decline: the request stays rejected and this card's delivery is dropped."),
        "why_human": ("Tried: the request was refused by kanban.self_work.freeze_guards and no open guard owner "
                      "exists to add it to. Only you can: decide whether this one request may pass the freeze."),
        "interaction": "approval",
        "scope": (f"One request, once: {declared.get('repository')}/{declared.get('area')} "
                  f"kind {declared.get('kind')}, tenant {tenant or 'none'}, sent by {envelope['from']}. "
                  f"Intent sha256 {TARGET.intent_sha256}. Approval {TARGET.approval}.\n"
                  "Options:\nA) Unblock: admit this request once; if an open guard owner exists by then, the "
                  "request is added to it instead of creating a card.\nB) Archive: decline; nothing is admitted.\n"
                  "Neither option releases any other card or changes the freeze. "
                  "If you do nothing, the offer expires and nothing is admitted."),
        "evidence": (f"Hub request {TARGET.request_id}: state rejected, code self_work_guard_frozen, "
                     f"result sha256 {TARGET.rejection_sha256}, read by this home when it filed this ask."),
    })


def offer(conn, records, *, now: Optional[int] = None) -> dict:
    """File the home's exact ask on the owner card. The card must already wait on David (``blocked`` with an
    agent's ask whose scope names the request); only then is that ask replaced, in one transaction with the
    offer event. Returns the stamp; a live offer is returned as is."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_ask_resolved import store_request
    from hermes_cli.kanban_release import ask_digest
    now = int(time.time()) if now is None else now
    _home(conn)
    envelope = original(records)
    result, cap = _receive(envelope, None)  # dry run through the real chain; admission rolls back at spend()
    if cap.tenant is None:
        raise Refused(f"request would not reach admission ({result.get('code')}: {result.get('error')})")
    with kb.write_txn(conn):
        _home(conn)
        row = conn.execute("SELECT status, human_request FROM tasks WHERE id=?", (TARGET.owner_task,)).fetchone()
        try:
            ask = json.loads(row["human_request"] or "null")
        except (TypeError, ValueError):
            ask = None
        if row["status"] != "blocked" or not isinstance(ask, dict):
            raise Refused("owner card is not waiting on a human ask")
        live = granting(conn, TARGET.owner_task, ask, now=now) if STAMP in ask else None
        if live is not None:
            return live
        if STAMP in ask or TARGET.request_id not in str(ask.get("scope") or ""):
            raise Refused("owner card's ask is not a request for this exception")
        stamp = {"version": 1, "task": TARGET.owner_task, "request_id": TARGET.request_id,
                 "intent_sha256": TARGET.intent_sha256, "rejection_sha256": TARGET.rejection_sha256,
                 "tenant": cap.tenant[0], "scope": "accounting-recovery-only", "approval": TARGET.approval,
                 "expires_at": now + TTL, "offer_uid": secrets.token_hex(16)}
        filed = {**_ask(envelope, cap.tenant[0]), STAMP: stamp}
        store_request(conn, TARGET.owner_task, filed)
        kb._append_event(conn, TARGET.owner_task, OFFERED, {STAMP: stamp, "ask_sha256": ask_digest(filed)})
    return stamp


def _live_grant(conn) -> Optional[dict]:
    for event in _own(conn, OFFERED):
        stamp = event["payload"].get(STAMP)
        grant = _grant(conn, stamp["offer_uid"]) if _bound(stamp) else None
        if grant is not None:
            return grant
    return None


def reconsider(conn, records) -> dict:
    """Admit the original once under the newest verified grant. The caller supplies nothing: the request, its
    sender and the grant are all read here. A repeat returns the stored intake receipt and spends nothing."""
    _home(conn)
    grant = _live_grant(conn)
    if grant is None:
        raise Refused("no verified Desktop grant for this request")
    result, _ = _receive(original(records), grant)
    return result


def status(conn) -> dict:
    """Supported readback by the original request ID; the courier's rejection record is separate and unchanged."""
    grant = _live_grant(conn)
    offers = [e["payload"].get(STAMP) for e in _own(conn, OFFERED)]
    row = None
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='self_work_intake' AND type='table'").fetchone():
        row = conn.execute("SELECT receipt_json FROM self_work_intake WHERE request_id=?",
                           (TARGET.request_id,)).fetchone()
    return {"request_id": TARGET.request_id, "owner_task": TARGET.owner_task,
            "offer": offers[0] if offers else None,
            "grant": grant and {"offer_uid": grant["offer_uid"], "operationId": grant["operationId"]},
            "spent": [e["payload"] for e in _own(conn, SPENT)],
            "admission": json.loads(row[0]) if row else None}


def main(argv=None) -> int:
    from hermes_cli.kanban_card_answer import AnswerRefused
    from hermes_cli.kanban_card_answer_runtime import _board_db
    from hermes_cli.kanban_card_answer_transport import Records
    from hermes_cli.kanban_db_connect import connect_closing
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or argv[0] not in ("offer", "reconsider", "status"):
        print(json.dumps({"ok": False, "error": "usage"}))
        return 64
    try:
        with connect_closing(_board_db("default")) as conn:
            result = (status(conn) if argv[0] == "status" else
                      offer(conn, Records()) if argv[0] == "offer" else reconsider(conn, Records()))
        out, code = {"ok": True, "result": result}, 0
    except (AnswerRefused, policy.SelfWorkError) as exc:
        out, code = {"ok": False, "refused": str(exc) or "refused"}, 2
    except (OSError, sqlite3.OperationalError):
        out, code = {"ok": False, "retryable": True, "error": "unavailable"}, 3
    print(json.dumps(out, sort_keys=True, default=str))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
