"""In-memory stand-in for the hermes-hub Board service (worker/src/rows.ts + board.ts), for the sync client tests.

Implements the contract the client relies on: row ops (per-field LWW by HLC on tasks, ``created_on`` set once,
``import`` refusing another gateway's id, a live lease fencing status moves), ``client_op_id`` idempotency, a dense
``seq`` feed of full rows, a retention floor (``410``), paged snapshots and leases. Several gateways share one board.
"""

from __future__ import annotations

import copy
import json
import math
import re
import time
from decimal import Decimal
from typing import Any, Optional

from hermes_cli import kanban_sync_client as ksc

FENCED = {"status", "current_run_uid", "result", "started_at", "completed_at"}
# rows.ts RUN_FENCED / FENCED_EVENT_KINDS (hermes-hub #111): a non-holder's run handoff and lifecycle events.
RUN_FENCED = ("status", "outcome", "ended_at", "summary", "metadata")
FENCED_EVENT_KINDS = frozenset({"completed", "blocked", "unblocked", "scheduled", "review_requested",
                                "changes_requested", "reclaimed", "archived", "status"})


def _js_number(x: float) -> str:
    """ECMAScript Number::toString of the wire number ``x`` as JSON.stringify writes it: ``null`` for NaN and
    +-Infinity (an int past the double range parses to Infinity), else the shortest round-trip digits (the ones
    Python's ``repr`` picks) laid out by the spec's fixed/exponent rule."""
    try:
        x = float(x)
    except OverflowError:
        return "null"
    if x != x or x in (math.inf, -math.inf):
        return "null"
    if x == 0:
        return "0"
    _, digits, exp = Decimal(repr(abs(x))).normalize().as_tuple()
    s, k = "".join(map(str, digits)), len(digits)
    n = int(exp) + k  # x = 0.s * 10**n
    sign = "-" if x < 0 else ""
    if k <= n <= 21:
        return sign + s + "0" * (n - k)
    if 0 < n <= 21:
        return sign + s[:n] + "." + s[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * -n + s
    return f"{sign}{s[0]}{'.' + s[1:] if k > 1 else ''}e{'+' if n > 0 else '-'}{abs(n - 1)}"


def _js_stringify(value: object) -> str:
    """``JSON.stringify(JSON.parse(json.dumps(value)))`` for a wire JSON value: strings as ``json.dumps`` with no
    ASCII escaping, numbers by :func:`_js_number`. Lone surrogates are out of scope (JS escapes them)."""
    if value is None or isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, (int, float)):
        return _js_number(value)
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(str(k), ensure_ascii=False) + ":" + _js_stringify(v)
                              for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_js_stringify(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def _js_string(value: object) -> str:
    """ECMAScript ``String(value)`` for a wire JSON value: null -> ``null``, booleans -> ``true``/``false``, numbers
    as Number::toString (NaN, +-Infinity spelled out), an array as ``Array.prototype.join`` (``,``; null elements
    are empty), an object -> ``[object Object]``."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        try:
            x = float(value)
        except OverflowError:
            return "Infinity" if value > 0 else "-Infinity"
        if x != x:
            return "NaN"
        return {math.inf: "Infinity", -math.inf: "-Infinity"}.get(x) or _js_number(x)
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return ",".join("" if v is None else _js_string(v) for v in value)
    return "[object Object]"


# StrWhiteSpaceChar (what ``Number()`` trims): TAB VT FF ZWNBSP, every Zs (SP NBSP U+1680 U+2000-U+200A U+202F
# U+205F U+3000) and LineTerminator (LF CR LS PS). Not Python's str.strip(): that also strips U+001C-U+001F and
# U+0085 (NaN in JS) and keeps U+FEFF (trimmed in JS).
_JS_WHITESPACE = "\t\v\f\ufeff \u00a0\u1680" + "".join(map(chr, range(0x2000, 0x200B))) + "\u202f\u205f\u3000" \
    + "\n\r\u2028\u2029"
# StrNumericLiteral with ASCII digits only (``\d`` would take U+0663, NaN in JS) and no ``_`` separators (float()
# reads "1_000"; JS: NaN).
_JS_DECIMAL = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|[+-]?Infinity")
_JS_NON_DECIMAL = re.compile(r"0(?:[xX](?P<x>[0-9a-fA-F]+)|[oO](?P<o>[0-7]+)|[bB](?P<b>[01]+))")


def _js_to_number(value: object) -> float:
    """ECMAScript ``Number(value)`` for a wire JSON value: null -> 0, booleans -> 0/1, numbers as themselves (an
    int past the double range -> Infinity), a string trimmed of StrWhiteSpaceChar (empty -> 0) and read as an
    unsigned 0x/0o/0b literal, a decimal literal or ``Infinity`` (ASCII digits only), an array through
    :func:`_js_string` (``[]`` -> 0, ``[x]`` -> ``Number(String(x))``), anything else NaN."""
    if value is None:
        return 0.0
    if isinstance(value, (bool, int, float)):
        try:
            return float(value)
        except OverflowError:
            return math.inf if value > 0 else -math.inf
    if isinstance(value, (list, tuple)):
        return _js_to_number(_js_string(value))
    if isinstance(value, str):
        text = value.strip(_JS_WHITESPACE)
        if not text:
            return 0.0
        if m := _JS_NON_DECIMAL.fullmatch(text):
            digits, base = next((d, {"x": 16, "o": 8, "b": 2}[k]) for k, d in m.groupdict().items() if d)
            try:
                return float(int(digits, base))
            except OverflowError:
                return math.inf
        return float(text.replace("Infinity", "inf")) if _JS_DECIMAL.fullmatch(text) else math.nan
    return math.nan


def _js_default_string(value: object, default: str) -> str:
    """``String(value ?? default)``: only a missing/null value takes ``default`` (``""``, 0 and false do not)."""
    return _js_string(default if value is None else value)


def release_outcome(op: dict) -> str:
    """releaseTask's ``String(op.outcome ?? "released")``: an explicit null is ``released``; ``""`` stays ``""``
    (refused ``invalid``) and ``["crashed"]`` is ``crashed``."""
    return _js_default_string(op.get("outcome"), "released")


class FakeService:
    def __init__(self, *, lease_runs: bool = False, machines: Optional[dict] = None, recover: bool = False) -> None:
        # Serve ``FakeHub.recover`` (placement recovery assigns the card); off = a Board without the route (404).
        self.recover = recover
        self.boards: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.offline = False
        # Like transitions.ts grantLease/releaseTask: a grant writes the card running plus its run row (setLease
        # mirrors lease_holder/lease_run_uid/lease_expires onto the card), a release is fenced on the holder's run
        # (lease_lost otherwise), ends that run, clears that mirror (clearLease) and returns a running card to its
        # retryStatusForRun. Off by default (the older tests count rows).
        self.lease_runs = lease_runs
        # Opt-in placement, like transitions.ts checkPlacement's runs_on rule: a grant needs every runs_on label
        # on the claiming machine ({name: {"labels": [...]}}; machine:<name> is implicit). None = no check.
        self.machines = machines
        # Session owners (worker/src/board.ts ``sessions``): {board: {session_id: owner}} plus the move audit.
        self.session_owners: dict[str, dict[str, dict]] = {}
        self.session_audit: list[dict] = []

    def board(self, bid: str) -> dict[str, Any]:
        return self.boards.setdefault(bid, {"seq": 0, "floor": 0, "changes": [], "rows": {}, "applied": {}, "leases": {}})

    def hub(self, gateway: str) -> FakeHub:
        return FakeHub(self, gateway)

    # ---- server-side helpers ----
    def _emit(self, b: dict, table: str, uid: str, op: str, row: Optional[dict], gw: str, op_id: Optional[str]) -> int:
        b["seq"] += 1
        b["changes"].append({"seq": b["seq"], "table": table, "uid": uid, "op": op,
                             "fields": copy.deepcopy(row), "hlc": (row or {}).get("hlc", ""),
                             "origin_gateway": gw, "client_op_id": op_id})
        return b["seq"]

    def _put(self, b: dict, table: str, uid: str, patch: dict, gw: str, op_id: Optional[str], hlc: str) -> dict:
        rows = b["rows"].setdefault(table, {})
        cur = rows.get(uid)
        if cur is None:
            cur = {"uid": uid, "version": 0}
            rows[uid] = cur
        cur.update(patch)
        cur.update(version=cur["version"] + 1, hlc=hlc, origin_gateway=gw, deleted_at=None)
        cur["seq"] = self._emit(b, table, uid, "upsert", None, gw, op_id)
        b["changes"][-1]["fields"] = copy.deepcopy(cur)
        return cur

    @staticmethod
    def child_fence(b: dict, table: str, uid: str, f: dict, gw: str) -> Optional[dict]:
        """As rows.ts rowFence for child rows: while another gateway holds the card's lease, a run upsert that changes
        a RUN_FENCED column (or creates the run) and a FENCED_EVENT_KINDS event are rejected ``live_claim`` with the
        Board's row (None for a row it never stored)."""
        if table not in ("task_runs", "task_events"):
            return None
        cur = b["rows"].get(table, {}).get(uid)
        if cur is not None and cur.get("deleted_at"):
            return None
        lease = b["leases"].get(f.get("task_id") or (cur or {}).get("task_id"))
        if not lease or lease["holder"] == gw:
            return None
        same = lambda a, z: (None if a is None else str(a)) == (None if z is None else str(z))
        if table == "task_runs":
            hit = any(k in f and (cur is None or not same(f[k], cur.get(k))) for k in RUN_FENCED)
        else:
            kind = f.get("kind", (cur or {}).get("kind"))
            hit = kind in FENCED_EVENT_KINDS and (cur is None or not same(kind, cur.get("kind")))
        if not hit:
            return None
        return {"status": "rejected", "reason": "live_claim",
                "detail": f"task is running under {lease['holder']}'s live lease", "row": cur}

    def row_op(self, b: dict, op: dict, gw: str) -> dict:
        table, uid, kind = op["table"], op["uid"], op["op"]
        rows = b["rows"].setdefault(table, {})
        if kind == "delete":
            if uid not in rows or rows[uid].get("deleted_at"):
                return {"status": "merged"}
            rows[uid]["deleted_at"] = 1
            self._emit(b, table, uid, "delete", None, gw, op["client_op_id"])
            return {"status": "applied"}
        f = dict(op["fields"])
        fenced = self.child_fence(b, table, uid, f, gw)
        if fenced is not None:
            return fenced
        if table == "tasks":
            cur = rows.get(uid)
            if cur is None:
                clocks = {k: op["hlc"] for k in f}
                return {"status": "applied", "row": self._put(b, table, uid, {"id": uid, **f, "created_on": gw,
                                                                                "field_hlc": clocks}, gw, op["client_op_id"], op["hlc"])}
            if op.get("import") and cur.get("created_on") not in (None, gw):
                return {"status": "rejected", "reason": "id_taken", "row": cur}
            lease = b["leases"].get(uid)
            if lease and lease["holder"] != gw and FENCED & set(f):
                return {"status": "rejected", "reason": "live_claim", "row": cur}
            clocks = cur.setdefault("field_hlc", {})
            won = {k: v for k, v in f.items() if k not in clocks or op["hlc"] > clocks[k]}
            if not won:
                return {"status": "merged", "row": cur}
            for k in won:
                clocks[k] = op["hlc"]
            row = self._put(b, table, uid, won, gw, op["client_op_id"], op["hlc"])
            # As rows.ts: the holder moving its card out of `running` ends the lease (opt-in with lease_runs).
            if self.lease_runs and lease and lease["holder"] == gw and "status" in won and won["status"] != "running":
                b["leases"].pop(uid, None)  # clearLease: the lease row and the task's lease mirror
                # endLeaseRun: a run the holder has not ended yet ends with a placeholder (the holder's own end
                # "normally follows in the same batch and overwrites this generic one").
                run = b["rows"].get("task_runs", {}).get(lease["run_uid"])
                if run is not None and run.get("ended_at") is None:
                    self._put(b, "task_runs", lease["run_uid"],
                              {"status": "released", "outcome": "released",
                               "error": f"card left running ({won['status']})", "ended_at": int(time.time())},
                              gw, op["client_op_id"], op["hlc"])
                row = self._put(b, table, uid, {"lease_holder": None, "lease_run_uid": None, "lease_expires": None}, gw,
                                op["client_op_id"], op["hlc"])
            return {"status": "applied" if len(won) == len(f) else "merged", "row": row}
        if table == "task_links":
            # rows.ts upsertLink: a link needs both of its cards on the Board.
            tasks = b["rows"].get("tasks", {})
            missing = [x for x in (f.get("parent_id"), f.get("child_id"))
                       if x not in tasks or tasks[x].get("deleted_at")]
            if missing:
                return {"status": "rejected", "reason": "not_found", "detail": f"unknown task(s): {', '.join(map(str, missing))}"}
        return {"status": "applied", "row": self._put(b, table, uid, f, gw, op["client_op_id"], op["hlc"])}

    def compact(self, bid: str) -> None:
        """Advance the retention floor to the head (every older cursor now gets 410)."""
        b = self.board(bid)
        b["floor"] = b["seq"]

    @staticmethod
    def retry_status_for_run(b: dict, run: Optional[str]) -> str:
        """As BoardTx.retryStatusForRun: ``review`` when the run's latest ``claimed`` event came out of review."""
        claims = [e for e in b["rows"].get("task_events", {}).values()
                  if run and e.get("kind") == "claimed" and e.get("run_uid") == run and not e.get("deleted_at")]
        latest = max(claims, key=lambda e: e["seq"], default=None)
        payload = json.loads(latest.get("payload") or "{}") if latest else {}
        return "review" if payload.get("source_status") == "review" else "ready"

    # transitions.ts NEUTRAL_RELEASES: release outcome -> (run outcome, event kind); never charged.
    NEUTRAL = {"rate_limited": ("rate_limited", "rate_limited"), "owner_restart": ("reclaimed", "owner_restart_lost"),
               "owner_unavailable": ("owner_unavailable", "owner_unavailable"), "owner_down": ("owner_down", "owner_down"),
               "reboot_bystander": ("host_reboot", "host_reboot")}
    # transitions.ts RELEASE_OUTCOMES: releaseTask rejects any other outcome as ``invalid``.
    RELEASE_OUTCOMES = frozenset({"released", *NEUTRAL, "crashed", "timed_out", "spawn_failed", "terminal_provider"})
    # transitions.ts MAX_DIAGNOSTIC_CHARS: a larger holder diagnostic is replaced by ``{diagnostic_truncated: true}``.
    MAX_DIAGNOSTIC_CHARS = 8000
    CANONICAL = frozenset({"crashed", "timed_out", "terminal_provider"})
    # releaseTask's policy checks: ``block_kind`` values and Number.MAX_SAFE_INTEGER for ``failure_limit``.
    BLOCK_KINDS = frozenset({"transient", "assignment", "capability"})
    MAX_SAFE_INTEGER = 2**53 - 1

    @classmethod
    def release_diagnostic(cls, value: object) -> Optional[dict]:
        """As transitions.ts releaseDiagnostic: absent -> ``{}``; anything but an object -> None (the caller
        refuses the release ``invalid``); past MAX_DIAGNOSTIC_CHARS of ``JSON.stringify`` (UTF-16 code units, no
        ASCII escaping, numbers as JS writes them: see :func:`_js_stringify`) -> ``{diagnostic_truncated: true}``;
        else a copy."""
        if value is None:
            return {}
        if not isinstance(value, dict):
            return None
        text = _js_stringify(value)
        return {"diagnostic_truncated": True} if len(text.encode("utf-16-le")) // 2 > cls.MAX_DIAGNOSTIC_CHARS \
            else dict(value)

    @classmethod
    def release_inputs(cls, op: dict) -> Optional[dict]:
        """releaseTask's post-fence validation, before any write. releaseTask's full order is the outcome
        (RELEASE_OUTCOMES), ``run_uid`` presence (``invalid`` when absent or null), fence(), then these, each
        ``invalid`` on failure: ``failure_limit`` when present (``Number(op.failure_limit)`` a positive safe
        integer, so an explicit null is 0 and refused: :func:`_js_to_number`), ``block_kind`` (``String(op.block_kind
        ?? "transient")`` one of BLOCK_KINDS, so ``["transient"]`` passes: :func:`_js_string`), ``op.extra ?? {}`` an
        object ("failure extra must be an object"), then
        :meth:`release_diagnostic`. The callers check the outcome, ``run_uid`` and the fence. Returns the
        diagnostic, or None when the caller refuses the release ``invalid``."""
        if "failure_limit" in op:
            limit = _js_to_number(op["failure_limit"])
            if not (math.isfinite(limit) and limit.is_integer() and 1 <= limit <= cls.MAX_SAFE_INTEGER):
                return None
        if _js_default_string(op.get("block_kind"), "transient") not in cls.BLOCK_KINDS:
            return None
        extra = op.get("extra")
        if extra is not None and not isinstance(extra, dict):
            return None
        return cls.release_diagnostic(op.get("diagnostic"))

    @classmethod
    def release_evidence(cls, outcome: str, op: dict, diagnostic: dict) -> tuple[dict, dict]:
        """releaseTask's (run metadata, event evidence) for ``outcome``, shared by the ops and leases routes (the
        real releaseTask is one function for both). Neutral: the holder's ``extra`` (+ ``owner_restart: true``)
        for both. Otherwise the diagnostic is evidence only for a canonical failure, and a ``crashed`` release
        with ``protocol_violation: true`` also marks the run metadata. ``unused_grant`` is the caller's."""
        if outcome in cls.NEUTRAL:
            meta = {**(op.get("extra") or {}), **({"owner_restart": True} if outcome == "owner_restart" else {})}
            return meta, meta
        evidence = diagnostic if outcome in cls.CANONICAL else {}
        protocol = outcome == "crashed" and op.get("protocol_violation") is True
        return {**evidence, **({"protocol_violation": True} if protocol else {})}, evidence

    def release_op(self, b: dict, op: dict, gw: str) -> dict:
        """As transitions.ts releaseTask: only the lease holder's run is released; the Board ends that run, emits
        its one outcome event (carrying :meth:`release_evidence`) and books a charged outcome exactly once.
        Not modelled: the protocol-violation streak (a ``protocol_violation`` crash is charged like any crash), and
        ``spawn_failed`` (endCustody + recordTaskFailure(releaseClaim, endRun)). As in releaseTask, every release is
        checked first in releaseTask's order: the outcome and ``run_uid`` presence (``invalid``), fence() (a
        missing, other-holder or stale lease is ``lease_lost``), then :meth:`release_inputs` (``invalid``), all
        before any write; a spawn_failed release that passes them raises NotImplementedError, as on the leases
        route."""
        task, run = op["task"], op.get("run_uid")
        outcome = release_outcome(op)
        if outcome not in self.RELEASE_OUTCOMES or run is None:
            return {"status": "rejected", "reason": "invalid"}
        lease = b["leases"].get(task)
        if not lease or lease["holder"] != gw or lease["run_uid"] != run:
            return {"status": "rejected", "reason": "lease_lost"}
        # releaseTask's policy, extra and releaseDiagnostic checks run after fence(), before any write, for every
        # outcome.
        diagnostic = self.release_inputs(op)
        if diagnostic is None:
            return {"status": "rejected", "reason": "invalid"}
        if outcome == "spawn_failed":
            raise NotImplementedError("FakeService.release_op: spawn_failed release (endCustody + "
                                      "recordTaskFailure) is not modelled")
        op_id = op["client_op_id"]
        clock = f"{9_999_999_999_999:013d}-0002-board"
        error = _js_default_string(op.get("error"), outcome)[:500]
        card = b["rows"]["tasks"][task]
        b["leases"].pop(task, None)
        ended, kind = self.NEUTRAL.get(outcome, ("crashed" if outcome == "terminal_provider" else outcome,) * 2)
        canonical = outcome in self.CANONICAL
        meta, evidence = self.release_evidence(outcome, op, diagnostic)
        self._put(b, "task_runs", run, {"status": ended, "outcome": ended, "ended_at": int(time.time()),
                                        "error": None if outcome == "released" else error,
                                        "metadata": json.dumps(meta) if meta else None}, "server", op_id, clock)
        payload = {**evidence, **({"failure_notice": "canonical"} if canonical else {}), "error": error,
                   "retry_status": "ready"}
        self._put(b, "task_events", f"ev-{op_id}-{kind}", {"task_id": task, "run_uid": run, "kind": kind,
                                                            "payload": json.dumps(payload), "created_at": int(time.time())},
                  "server", op_id, clock)
        fields: dict = {"status": "ready", "current_run_uid": None, "lease_holder": None, "lease_run_uid": None,
                        "lease_expires": None}
        if canonical:
            failures = int(card.get("consecutive_failures") or 0) + 1
            # recordTaskFailure: ``row.max_retries ?? (Number(op.failure_limit) ?? DEFAULT_FAILURE_LIMIT)``, so a card
            # max_retries of 0 trips at once.
            override, policy = card.get("max_retries"), op["failure_limit"] if "failure_limit" in op else 2
            limit = _js_to_number(override) if override is not None else _js_to_number(policy)
            tripped = bool(op.get("force_trip")) or outcome == "terminal_provider" or failures >= int(limit)
            fields.update(consecutive_failures=failures, last_failure_error=error,
                          **({"status": "triage"} if tripped else {}))
            final = {**(op.get("extra") or {}), "run_uid": run, "failures": failures, "effective_limit": int(limit),
                     "error": error, "trigger_outcome": outcome, "tripped": tripped}
            for event in (["gave_up"] if tripped else []) + ["failure_finalized"]:
                self._put(b, "task_events", f"ev-{op_id}-{event}", {"task_id": task, "run_uid": run, "kind": event,
                                                                     "payload": json.dumps(final),
                                                                     "created_at": int(time.time())},
                          "server", op_id, clock)
        clocks = {**card.get("field_hlc", {}), **dict.fromkeys(fields, clock)}
        row = self._put(b, "tasks", task, {**fields, "field_hlc": clocks}, "server", op_id, clock)
        return {"status": "applied", "row": row}


class FakeHub:
    """Duck-typed :class:`ksc.Hub` for one gateway."""

    def __init__(self, svc: FakeService, gateway: str) -> None:
        self.svc, self.gateway = svc, gateway
        self.ident = ksc.Identity(url="https://hub.test", key="k", gateway=gateway)

    def _check(self) -> None:
        if self.svc.offline:
            raise OSError("hub unreachable")

    def recover(self, board: str, *, task_id: str, expected_revision: int, mode: str, candidates: list) -> dict:
        """``POST /placement/recover`` (worker/src/board.ts): off by default, answered like a Board without the
        route (404, so the origin backs off recovery). With ``FakeService(recover=True)`` it assigns the card at
        ``expected_revision`` to the first candidate: ``promote`` sets ``runs_on=[machine:<it>]`` and the worker
        placement policy, ``stale_pin`` swaps the machine label; both write a ``moved`` event the feed carries."""
        self._check()
        if not self.svc.recover:
            raise ksc.HubError(404, "not found")
        from hermes_cli.kanban_worker_placement import POLICY
        self.svc.calls.append(("recover", board, {"task_id": task_id, "mode": mode, "candidates": candidates}))
        b = self.svc.board(board)
        row = b["rows"].get("tasks", {}).get(task_id)
        if row is None or row["version"] != expected_revision or not candidates:
            raise ksc.HubError(409, json.dumps({"error": "not_claimable"}))
        old = json.loads(row["runs_on"]) if isinstance(row.get("runs_on"), str) else (row.get("runs_on") or [])
        labels = [f"machine:{candidates[0]}"] + ([x for x in old if not x.startswith("machine:")]
                                                 if mode == "stale_pin" else [])
        hlc = f"{9_999_999_999_997:013d}-0000-board"
        updated = self.svc._put(b, "tasks", task_id, {"runs_on": json.dumps(labels), "placement_policy": (
            POLICY if mode == "promote" else row.get("placement_policy"))}, "board", None, hlc)
        self.svc._put(b, "task_events", f"recover-{task_id}-{updated['version']}", {
            "task_id": task_id, "kind": "moved", "created_at": int(time.time()),
            "payload": json.dumps({"repinned": True, "runs_on": labels, "reason": "recovery:" + mode})},
            "board", None, hlc)
        return {"state": "assigned", "machine": candidates[0], "task_version": updated["version"]}

    def reserve(self, board: str, *, task_id: str, expected_revision: int, candidates: list) -> dict:
        """``POST /placement/reserve`` for a worker-first card, served with ``recover`` (a Board that has one has
        both): assigns the first candidate at ``expected_revision`` without changing the row; off = 404."""
        self._check()
        if not self.svc.recover:
            raise ksc.HubError(404, "not found")
        self.svc.calls.append(("reserve", board, {"task_id": task_id, "candidates": candidates}))
        row = self.svc.board(board)["rows"].get("tasks", {}).get(task_id)
        if row is None or row["version"] != expected_revision or not candidates:
            raise ksc.HubError(409, json.dumps({"error": "revision_conflict"}))
        return {"state": "assigned", "machine": candidates[0], "reservation_id": f"r-{task_id}",
                "expires_at": int(time.time()) + 60, "reason": "worker_capacity_reserved"}

    def ops(self, board: str, ops: list[dict]) -> dict:
        self._check()
        self.svc.calls.append(("ops", board, ops))
        if len(ops) > 200:  # the real Board's request limit
            raise ksc.HubError(400, '{"error":"ops must be a list of <= 200"}')
        b = self.svc.board(board)
        results = []
        for op in ops:
            prior = b["applied"].get(op["client_op_id"])
            if prior is not None:
                results.append({**prior, "dup": True})
                continue
            res = {"client_op_id": op["client_op_id"],
                   **(self.svc.release_op(b, op, self.gateway) if op.get("op") == "release"
                      else self.svc.row_op(b, op, self.gateway))}
            row = res.get("row")
            res["version"] = row.get("version") if row else None
            b["applied"][op["client_op_id"]] = res
            results.append(res)
        return {"results": results, "head": b["seq"]}

    def changes(self, board: str, since: int, limit: int = 500) -> dict:
        self._check()
        b = self.svc.board(board)
        if since < b["floor"]:
            raise ksc.Resync("cursor below the retention floor")
        page = [c for c in b["changes"] if c["seq"] > since][:limit]
        more = len([c for c in b["changes"] if c["seq"] > since]) > len(page)
        return {"changes": copy.deepcopy(page), "next": page[-1]["seq"] if page else since, "head": b["seq"], "more": more}

    def row_evidence(self, board: str, table: str, uid: str) -> dict:
        self._check()
        b = self.svc.board(board)
        row = copy.deepcopy(b["rows"].get(table, {}).get(uid))
        state = ("deleted" if row.get("deleted_at") is not None else "live") if row else (
            "unknown" if b["floor"] else "never_existed")
        return {"table": table, "uid": uid, "state": state, "row": row,
                "tombstone": None, "floor": b["floor"], "head": b["seq"]}

    def snapshot_page(self, board: str, table: str, after: str = "", limit: int = 1000) -> dict:
        self._check()
        b = self.svc.board(board)
        live = sorted((u, r) for u, r in b["rows"].get(table, {}).items() if not r.get("deleted_at") and u > after)
        page = live[:limit]
        return {"head": b["seq"], "table": table, "rows": [copy.deepcopy(r) for _, r in page],
                "next": page[-1][0] if page else after, "more": len(live) > limit}

    def leases(self, board: str, body: dict) -> dict:
        self._check()
        b = self.svc.board(board)
        task, release = body["task"], body["action"] == "release"
        # releaseTask refuses a release with no run_uid ``invalid`` (below); grant and renew need one.
        run: Any = body.get("run_uid") if release else body["run_uid"]
        cur = b["leases"].get(task)
        op_id, hlc, gw = body.get("client_op_id"), f"{9_999_999_999_998:013d}-0000-board", self.gateway
        version = None
        if body["action"] == "grant":
            if cur and cur["holder"] != self.gateway:
                return {"status": "rejected", "reason": "lease_held", "lease": cur}
            card = b["rows"].get("tasks", {}).get(task) or {}
            if self.svc.machines is not None and card.get("runs_on"):
                want = json.loads(card["runs_on"]) if isinstance(card["runs_on"], str) else card["runs_on"]
                mine = set((self.svc.machines.get(self.gateway) or {}).get("labels") or []) | {f"machine:{self.gateway}"}
                if set(want or []) - mine:
                    return {"status": "rejected", "reason": "labels_mismatch", "lease": cur}
            b["leases"][task] = {"task_id": task, "holder": self.gateway, "run_uid": run, "expires": 9_999_999_999_999}
            if self.svc.lease_runs and task in b["rows"].get("tasks", {}):
                self.svc._put(b, "task_runs", run, {"task_id": task, "status": "running", "started_at": 1,
                                                    "ended_at": None, "outcome": None}, gw, op_id, hlc)
                # setLease mirrors the lease onto the card: lease_holder, lease_run_uid, lease_expires.
                version = self.svc._put(b, "tasks", task, {"status": "running", "current_run_uid": run,
                                                           "lease_holder": gw, "lease_run_uid": run,
                                                           "lease_expires": 9_999_999_999_999},
                                        gw, op_id, hlc)["version"]
                # As the Board's grantLease (hermes-hub worker/src/transitions.ts): the claimed event, tagged with
                # the lane the claim came from, so a reviewer run retries to ``review``.
                payload = {"holder": gw, "run_uid": run, "expires": 9_999_999_999_999,
                           **({"source_status": "review"} if body.get("lane") == "review" else {})}
                self.svc._put(b, "task_events", f"ev-{run}", {"task_id": task, "run_uid": run, "kind": "claimed",
                                                              "payload": json.dumps(payload),
                                                              "created_at": int(time.time())}, gw, op_id, hlc)
        elif body["action"] == "renew":
            if not cur or cur["holder"] != self.gateway or cur["run_uid"] != run:
                return {"status": "rejected", "reason": "lease_lost", "lease": cur}
        else:
            if release_outcome(body) not in self.svc.RELEASE_OUTCOMES or run is None:
                # releaseTask checks the outcome, then run_uid presence, before its fence: an unknown outcome (a
                # test typo) or a missing/null run_uid is ``invalid``.
                return {"status": "rejected", "reason": "invalid", "lease": cur}
            if self.svc.lease_runs and (not cur or cur["holder"] != gw or cur["run_uid"] != run):
                # releaseTask's fence(): a release that does not name this gateway's live lease run is refused and
                # changes nothing (the card keeps its lease and mirror).
                return {"status": "rejected", "reason": "lease_lost", "lease": cur}
            # releaseTask's policy, extra and releaseDiagnostic checks run after fence(), before any write: a bad
            # failure_limit or block_kind, or a non-object extra or diagnostic, is ``invalid`` for every outcome
            # and changes nothing.
            diagnostic = self.svc.release_inputs(body)
            if diagnostic is None:
                return {"status": "rejected", "reason": "invalid", "lease": cur}
            if release_outcome(body) == "spawn_failed":
                # As release_op: fenced and validated first (releaseTask's order), then refused loudly whatever
                # lease_runs says, before any write.
                raise NotImplementedError("FakeHub.leases: spawn_failed release (endCustody + recordTaskFailure) is "
                                          "not modelled; see _release_run")
            b["leases"].pop(task, None)
            runs, card = b["rows"].get("task_runs", {}), b["rows"].get("tasks", {}).get(task)
            if self.svc.lease_runs and card is not None:
                retry = self.svc.retry_status_for_run(b, card.get("current_run_uid"))
                if run in runs and runs[run].get("ended_at") is None:
                    self._release_run(b, task, run, body, retry, op_id, hlc, diagnostic)
                else:
                    # clearLease drops the lease mirror; a still-running card goes back to retryStatusForRun; endRun
                    # clears current_run_uid even when the holder already ended its run row.
                    self.svc._put(b, "tasks", task, {**({"status": retry} if card.get("status") == "running" else {}),
                                                     "current_run_uid": None, "lease_holder": None,
                                                     "lease_run_uid": None, "lease_expires": None}, gw, op_id, hlc)
            return {"status": "applied", "lease": None}
        return {"status": "applied", "lease": b["leases"][task], "version": version}

    def _release_run(self, b: dict, task: str, run: str, body: dict, retry: str, op_id: Optional[str],
                     hlc: str, diagnostic: dict) -> None:
        """As transitions.ts releaseTask (via board.ts ``leases`` release) for the holder's live run: the run ends,
        a running card goes back to ``retry`` (retryStatusForRun) and the run-outcome event always carries it as
        ``retry_status``.

        Modelled outcomes (anything outside ``FakeService.RELEASE_OUTCOMES``, a non-object ``extra`` or a
        non-object ``diagnostic``, is refused ``invalid`` before this); run metadata and event evidence come from
        :meth:`FakeService.release_evidence`, shared with :meth:`FakeService.release_op`:
        - neutral (``FakeService.NEUTRAL`` = NEUTRAL_RELEASES): the run ends as its mapped outcome and emits its
          mapped event; the holder's ``extra`` (plus ``owner_restart: true``) is merged into run metadata and payload.
        - ``released``, ``crashed``, ``timed_out``, ``terminal_provider`` (-> ``crashed``): the run ends as that
          outcome and emits that kind. For the canonical failures the holder's ``diagnostic`` is merged into run
          metadata and payload (``{diagnostic_truncated: true}`` past MAX_DIAGNOSTIC_CHARS), and a ``crashed``
          release with ``protocol_violation: true`` marks the run metadata. ``unused_grant`` (live
          t_01a1a1dea9c64d6a run 164239) marks the run metadata and the event only for a plain ``released``.

        Not modelled: failure accounting (recordTaskFailure: consecutive_failures, failure_finalized, the breaker,
        the protocol-violation streak); :meth:`FakeService.release_op` covers it for the ops path. ``spawn_failed``
        is refused with NotImplementedError by :meth:`leases` after its fence (with ``lease_runs``) and input checks,
        as releaseTask orders them: the real releaseTask hands it to endCustody + recordTaskFailure(releaseClaim,
        endRun), whose run/event shape is that accounting."""
        gw, svc = self.gateway, self.svc
        outcome = release_outcome(body)
        error = _js_default_string(body.get("error"), outcome)[:500]
        card = b["rows"]["tasks"][task]
        running = card.get("status") == "running"
        neutral = svc.NEUTRAL.get(outcome)
        meta, evidence = svc.release_evidence(outcome, body, diagnostic)
        if neutral:
            ended, kind = neutral
            run_fields = {"error": error, "metadata": json.dumps(meta, separators=(",", ":")) if meta else None}
            payload: dict = {**evidence, "error": error, "retry_status": retry}
            task_fields = {"status": retry, "last_failure_error": error} if running else {}
        else:
            ended = kind = "crashed" if outcome == "terminal_provider" else outcome
            unused = bool(body.get("unused_grant")) and outcome == "released" and running
            canonical = outcome in svc.CANONICAL
            err = None if outcome == "released" else error
            meta = {**meta, **({"unused_grant": True} if unused else {})}
            run_fields = {"error": err, **({"metadata": json.dumps(meta, separators=(",", ":"))} if meta else {})}
            payload = {**evidence, **({"failure_notice": "canonical"} if canonical else {}), "error": err,
                       "retry_status": retry, **({"terminal_provider": True} if outcome == "terminal_provider" else {}),
                       **({"unused_grant": True} if unused else {})}
            task_fields = {"status": retry, **({"last_failure_error": error} if outcome == "rate_limited" else {})} \
                if running else {}
        svc._put(b, "task_runs", run, {"status": ended, "outcome": ended, "ended_at": 2, **run_fields}, gw, op_id, hlc)
        svc._put(b, "task_events", f"ev-rel-{run}",
                 {"task_id": task, "run_uid": run, "kind": kind,
                  "payload": json.dumps(payload, separators=(",", ":")), "created_at": int(time.time())},
                 gw, op_id, hlc)
        svc._put(b, "tasks", task, {**task_fields, "current_run_uid": None, "lease_holder": None,
                                    "lease_run_uid": None, "lease_expires": None}, gw, op_id, hlc)

    def sessions(self, board: str, body: dict) -> dict:
        """As board.ts ``sessions``: ``get`` -> {owners}; ``move`` is a CAS signed by the current owner. The target
        must be an active, unheld registry machine (with ``machines`` None, any name) other than the caller.
        A refusal is the 409 body ``{reason, owner}`` (what :meth:`ksc.Hub.sessions` returns for a 409)."""
        self._check()
        self.svc.calls.append(("sessions", board, body))
        owners = self.svc.session_owners.setdefault(board, {})
        if body["action"] == "get":
            ids = body["session_ids"]
            assert isinstance(ids, list) and len(ids) <= 200
            return {"owners": {sid: copy.deepcopy(owners[sid]) for sid in ids if sid in owners}}
        sid, to, expect = body["session_id"], body["to"], body["expect_epoch"]
        cur = copy.deepcopy(owners.get(sid))

        def refuse(reason: str) -> dict:
            return {"reason": reason, "owner": cur}

        if cur and cur["gateway"] != self.gateway:
            return refuse("not_owner")
        if (cur["epoch"] if cur else 0) != expect:
            return refuse("epoch_mismatch")
        if to == self.gateway:
            return refuse("same_gateway")
        if self.svc.machines is not None:
            m = self.svc.machines.get(to)
            if not m or m.get("state", "active") != "active" or m.get("hold"):
                return refuse("unknown_target")
        elif not to:
            return refuse("unknown_target")
        owners[sid] = {"gateway": to, "epoch": expect + 1, "moved_at": int(time.time() * 1000),
                       "moved_by": self.gateway}
        self.svc.session_audit.append({"session_id": sid, "actor": self.gateway,
                                       "from_gateway": cur["gateway"] if cur else None, "to_gateway": to,
                                       "epoch": expect + 1, "reason": body.get("reason")})
        return {"status": "moved", "owner": copy.deepcopy(owners[sid])}
