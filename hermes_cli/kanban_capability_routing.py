"""Cross-gateway capability routing: a wall on one gateway opens an owner card where the
capability lives. [daveey]

A worker that hits a wall declares what it needs (``kanban_block(needs=[...])`` /
``kanban_schedule(needs=[...])``, CLI ``--need``, or a card's ``requires`` entries that are
namespaced capability names). When this gateway lacks a need and another gateway holds it
(fresh entry in the hermes-hub capability registry, :mod:`kanban_capability_registry`):

1. ONE owner card is created on the holder through ``hermes-hub create-task`` with request id
   ``capability-owner:<card>:<need>`` (exact retries are idempotent on both ends). Its body is
   the scoped action, the readback and the wake of the waiting card. If the action goes beyond
   the approved plan, the owner card -- not the worker -- files the real ``human_request``.
2. The waiting card parks ``scheduled`` on that owner card: wake_check
   ``hermes kanban wait-check capability-owner <request ids>`` exits 0 once every owner card is
   done (1 while the request or card is pending, 2 if the request was rejected).

Refusals are typed (:class:`CapabilityRouteError`), and nothing is written:

* ``no_capability_holder`` -- no gateway holds it: if only David can grant it, block with
  kind='needs_input' and a ``human_request`` naming the exact command.
* ``capability_registry_stale`` -- a holder exists but its record is more than ``RELAY_RETRY_S``
  past the freshness window; stale registry data never routes. (A record that lapsed more recently
  parks and retries, see Relay below.)
* ``capability_held_locally`` -- this gateway holds it: the wall is the worker's profile
  (kind='assignment') or a real failure, not a missing capability.
* ``capability_route_exhausted`` -- this card already routed that need once; the existing owner
  card must finish or report why. A REJECTED or EXPIRED owner request may be re-routed once more
  (generation 2 of the request id); a FAILED one, or a second-generation refusal, is exhausted.

Relay (gateways without a hermes-hub replica, and a holder that is briefly not live-checked):

* A Board worker node runs no hermes-hub sync: it has no registry to look the holder up in and no
  ``create-task``. When it runs a card copied from another gateway (``created_on``), routing parks the
  card on the same ``capability-owner:<card>:<need>`` ids and records a ``relay`` route to that home
  gateway. The park names the home as ``probe_host``, so the worker registers no probe and evicts its idle
  copy; the home's dispatcher registers the probe from the park event (``restore_missing_checks``), and
  :func:`owner_status` there routes the need with the home's own registry: ``create-task`` to a remote
  holder under the same request id, or, when the home holds the need itself, a local owner card keyed
  by that id (``idempotency_key``) and pinned to the home machine. A worker node's OWN card has no home
  to relay to: ``capability_registry_unavailable``.
* A holder whose record lapsed recently (a gateway restarting or asleep) is not a refusal: the card
  parks the same way, relayed to this gateway itself, and the probe retries routing for
  ``RELAY_RETRY_S``. A relay that still finds no holder after that window wakes the card with the
  typed error in the probe output, and the next ``route()`` for that need raises it.

Retries are safe: an id that was already sent is re-sent from its stored envelope (same gateway,
title, body and overlap override), so ``hermes-hub`` sees an exact retry, never a conflicting one.
A fresh envelope carries ``distinct_from`` for the waiting card and the cards it awaits: the owner
title quotes that work, and the target's create_overlap check would otherwise refuse it as a
duplicate of the card it exists to serve.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import hashlib
from datetime import UTC, datetime
from typing import Any, Callable, Optional

from agent.redact import redact_sensitive_text
from hermes_cli import kanban_capability_registry as reg

OWNER_PREFIX = "capability-owner:"
ROUTED_EVENT = "capability_routed"
WAKE_EVERY = 120
WAKE_HORIZON = 72 * 3600
HUB_TIMEOUT = 60
# How long a relayed route keeps retrying a holder that is missing or stale before it wakes the card
# with the typed error (a gateway restart or a laptop asleep must not fail the route). [daveey]
RELAY_RETRY_S = 1800
RELAY_HOME = "home"      # relayed by a worker node to the card's home gateway
RELAY_RETRY = "retry"    # parked on this gateway itself until a stale holder is live-checked again
# Stable: a resend of a fresh envelope must byte-match, and the target records it on the new card.
DISTINCT_REASON = (
    "capability owner card for the waiting card and the cards it awaits; "
    "it does the scoped action on this gateway, it is not a second owner of that work"
)


class CapabilityRouteError(ValueError):
    """No owner card can be routed; nothing was written. ``code`` is the typed reason."""

    def __init__(self, code: str, message: str, *, details: Optional[dict] = None):
        self.code = code
        self.details = details or {}
        super().__init__(f"{code}: {message}")


def request_id(task_id: str, need: str, generation: int = 1) -> str:
    """Stable hermes-hub request id (and owner idempotency) for one card's need.

    Readable ``capability-owner:<card>:<need>`` when the need fits the id alphabet and length;
    otherwise a digest suffix keeps distinct needs distinct (no truncation/substitution merges).
    """
    gen = f"::g{generation}" if generation > 1 else ""  # needs never contain '::' 
    plain = f"{OWNER_PREFIX}{task_id}:{need}{gen}"
    if re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", plain):
        return plain
    safe = re.sub(r"[^A-Za-z0-9._:-]", "-", need)[:48]
    digest = hashlib.sha256(need.encode("utf-8")).hexdigest()[:16]
    return f"{OWNER_PREFIX}{task_id}:{safe}:{digest}{gen}"[:128]


def needs_from_requires(requires) -> list[str]:
    """Namespaced capability needs declared in a card's ``requires``."""
    return [r for r in (requires or []) if reg.is_gateway_need(r)]


def _is_hub_gateway(name) -> bool:
    """[daveey] Hub-bound owner cards go through hub admission (kanban_hub_admission_placement)."""
    from hermes_cli.kanban_hub_admission_placement import is_hub
    from hermes_cli import kanban_placement as kp
    return bool(name) and is_hub(str(name), kp.config())


def _hub_cli() -> str:
    return reg.hub_cli_path(reg._config().get("hub_cli"))


def _owner_assignee() -> str:
    value = reg._config().get("owner_assignee")
    return value if isinstance(value, str) and value.strip() else "default"


def _default_runner(argv: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=HUB_TIMEOUT, check=False, stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or "").strip() + ("\n" + p.stderr.strip() if p.stderr else "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


# Tests replace this with a fake that records argv.
hub_runner: Callable[[list[str]], tuple[int, str]] = _default_runner


def overlap_exclusions(conn, task_id: str) -> list[str]:
    """The waiting card and the local cards it awaits (bounded), in walk order.

    The owner card's title embeds the waiting card's title, so the target's overlap check matches
    that card and any open card it awaits with a similar title. Those are the work this owner
    exists to serve, not a second owner. A remote awaiting ref (a request id) is not a task id
    and is not walked: the producer card id is not in it.
    """
    from hermes_cli.kanban_awaiting import _TASK_ID, _latest_park

    seen: list[str] = []
    stack = [task_id]
    while stack and len(seen) < 32:
        node = stack.pop(0)
        if not node or node in seen or not _TASK_ID.fullmatch(node):
            continue
        seen.append(node)
        awaiting, _reason = _latest_park(conn, node)
        if not isinstance(awaiting, dict) or awaiting.get("kind") != "card":
            continue
        ref = str(awaiting.get("ref") or "")
        if _TASK_ID.fullmatch(ref):
            stack.append(ref)
    return seen + [b for b in bound_cards(conn, task_id) if b not in seen]


# The footer kanban_placement_forward.remote_args writes on a card it places on another gateway. Any body
# line in this shape is trusted, author-written or not: acceptable because it only adds a heuristic
# create_overlap exemption (distinct_from) to this card's own capability-owner create, never a permission.
_PLACED_FROM = re.compile(r"^This card was \S+ card (t_[0-9a-f]{8,32}); it completes there when this one is done\.$",
                          re.MULTILINE)


def bound_cards(conn, task_id: str) -> list[str]:
    """Cards whose completion is bound to ``task_id``: they wait on it, so they never do its work.

    The origin placeholder of a placed card (named in the forward's footer; it lives on the gateway that
    placed it and is parked on this card) and local cards whose current park waits on it. The owner title
    quotes the waiting card's title, which these cards share, so the holder's create_overlap check matched
    the origin placeholder and refused the owner (hub t_01a1d49003c65ca4 -> macbook t_01a1d48f676074bc).
    """
    out: list[str] = []
    row = conn.execute("SELECT body FROM tasks WHERE id = ?", (task_id,)).fetchone()
    for tid in _PLACED_FROM.findall((row[0] if row else "") or ""):
        if tid != task_id and tid not in out:
            out.append(tid)
    # Matched by bare awaiting.ref, ignoring awaiting.gateway: task ids are globally unique, so the ref names
    # this card whichever gateway the park recorded.
    rows = conn.execute(
        "SELECT t.id FROM tasks t WHERE t.status = 'scheduled' AND t.id != ? AND (SELECT json_extract(e.payload, "
        "'$.awaiting.ref') FROM task_events e WHERE e.kind IN ('scheduled', 'continued') AND e.task_id = t.id "
        "ORDER BY e.id DESC LIMIT 1) = ? LIMIT 16", (task_id, task_id)).fetchall()
    out += [r[0] for r in rows if r[0] not in out]
    return out


def _strip_overlap_options(argv: list[str]) -> list[str]:
    """Drop distinct_from / distinct_reason options an older hermes-hub client rejects."""
    out: list[str] = []
    i = 0
    while i < len(argv):
        nxt = argv[i + 1] if i + 1 < len(argv) else ""
        if argv[i] == "--option" and nxt.startswith(("distinct_from=", "distinct_reason=")):
            i += 2
            continue
        out.append(argv[i])
        i += 1
    return out


def _create_task(argv: list[str]) -> tuple[int, str]:
    """Queue the owner card. An older client that does not know distinct_from still gets the create;
    the target's overlap check skips the waiting card itself (kanban_overlap.capability_owner_waiting_id).
    """
    rc, out = hub_runner(argv)
    if rc in (0, 3) or "create_option_unknown" not in out or "distinct_from" not in out:
        return rc, out
    return hub_runner(_strip_overlap_options(argv))


def _overlap_option_argv(stored_args: Optional[dict], exclusions: list[str]) -> list[str]:
    """``--option`` pairs for create-task. A stored envelope is replayed unchanged."""
    if stored_args and "distinct_from" in stored_args:
        distinct = stored_args["distinct_from"]
        reason = stored_args.get("distinct_reason") or DISTINCT_REASON
    else:
        distinct, reason = exclusions, DISTINCT_REASON
    return [
        "--option", "distinct_from=" + json.dumps(distinct, separators=(",", ":")),
        "--option", "distinct_reason=" + json.dumps(reason, separators=(",", ":")),
    ]


def _ago(at: float, now: float) -> str:
    s = int(max(0, now - at))
    return f"{s // 3600}h{(s % 3600) // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def plan(needs: list[str], *, now: Optional[float] = None, **lookup) -> dict:
    """Decide routing without writing. Raises CapabilityRouteError for the typed refusals.

    Returns ``{"routes": [{need, gateway, checked_at}], "local": [needs held here]}``.
    """
    now = time.time() if now is None else now
    found = [reg.locate(n, now=now, **lookup) for n in needs]
    local = [f["need"] for f in found if f["local"] == "fresh"]
    missing = [f for f in found if f["local"] != "fresh"]
    if not missing:
        raise CapabilityRouteError(
            "capability_held_locally",
            f"this gateway holds {', '.join(local)} (live-checked); the wall is not a missing gateway "
            "capability. If this worker's profile cannot use it, block with kind='assignment'; "
            "otherwise fix the failure yourself.", details={"local": local})
    routes = []
    for f in missing:
        if f["holders"]:
            gw, at = f["holders"][0]
            routes.append({"need": f["need"], "gateway": gw, "checked_at": at})
            continue
        if f["stale"]:
            gws = ", ".join(f"{gw} ({_ago(at, now)} ago)" for gw, at in f["stale"])
            raise CapabilityRouteError(
                "capability_registry_stale",
                f"{f['need']} was last confirmed on {gws}, older than the {reg.stale_after()}s freshness "
                "window, so it is not routed. That gateway's probe or hermes-hub sync is not running; "
                "fix that, or if only David can grant it, block with kind='needs_input' and a "
                "human_request naming the exact command.", details={"need": f["need"], "stale": f["stale"]})
        raise CapabilityRouteError(
            "no_capability_holder",
            f"no gateway holds {f['need']} (hermes-hub capability registry). If only David can grant it, "
            "block with kind='needs_input' and a human_request naming the exact command and scope. If an "
            "agent on another host has the access but does not advertise it, that is a registry gap, not a "
            "human ask: never ask David to relay a request to his chat (refused as agent_relay_ask); send it "
            "to that chat with cross_session or kanban_create the owner card there, and add the probe to "
            "that host's kanban.capabilities.probes.",
            details={"need": f["need"]})
    return {"routes": routes, "local": local}


def _sent(rid: str) -> Optional[dict]:
    """The stored hermes-hub envelope + state for ``rid`` on this gateway (None if never queued)."""
    try:
        db = reg._ro(reg.hub_db_path())
    except Exception:
        return None
    try:
        row = db.execute("SELECT body, state FROM sent WHERE id = ?", (rid,)).fetchone()
        if row is None:
            return None
        return {"body": json.loads(row[0] or "{}"), "state": row[1]}
    except Exception:
        return None
    finally:
        db.close()


def _previous_routes(conn, task_id: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for (payload,) in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id", (task_id, ROUTED_EVENT)):
        try:
            for r in json.loads(payload or "{}").get("routes") or []:
                if isinstance(r, dict) and r.get("need"):
                    out[r["need"]] = r
        except ValueError:
            continue
    return out


def owner_body(task, need: str, *, self_gw: str, board: str, reason: str) -> str:
    """Deterministic for (card, need, reason): retries resend an identical envelope."""
    ref = f"{self_gw}/default/{board}/{task.id}"
    excerpt = redact_sensitive_text((reason or "").strip(), force=True)
    title = redact_sensitive_text((task.title or "")[:160], force=True)
    if len(excerpt) > 3000:
        excerpt = excerpt[:3000] + "…"
    return (
        f"Capability owner card for waiting card {ref} (\"{title}\").\n"
        f"It needs `{need}`. This gateway holds it (live-checked in the hermes-hub capability registry); "
        f"{self_gw} does not.\n\n"
        "## Action (from the waiting card, inside its approved plan)\n"
        f"{excerpt or '(no reason given; read the waiting card first)'}\n\n"
        "## Do\n"
        f"1. Read the waiting card: `hermes-hub tasks --gw {self_gw} --json` (or cross_task read "
        f"@task:{board}@{self_gw}/{task.id}).\n"
        f"2. Perform exactly the scoped action above with this gateway's `{need}`. Keep it to the minimum "
        "the waiting card needs (least privilege, tag/resource conditions where they apply).\n"
        "3. If the action goes beyond the waiting card's approved plan or scope, do NOT do it: block THIS "
        "card with kind='needs_input' and a human_request naming the exact command. The waiting worker "
        "does not ask David; this card does.\n\n"
        "## Readback\n"
        "Verify the effect from the waiting card's side where you can (for example run its failing call "
        "again from that gateway) and record the evidence in your completion summary.\n\n"
        "## Wake\n"
        f"The waiting card is parked on this card: completing it wakes {ref} within its next probe "
        f"(about {WAKE_EVERY // 60} min). Also comment the result on it: "
        f"`hermes-hub request {self_gw} tasks default/{board}/{task.id} comment --arg text=...`.\n"
    )


def has_replica() -> bool:
    """True when this gateway runs hermes-hub sync (its ``hub.db`` replica exists). Board worker nodes don't."""
    try:
        return reg.hub_db_path().exists()
    except OSError:
        return False


def card_home(conn, task_id: str) -> Optional[str]:
    """The Board gateway that created ``task_id`` when this file holds a COPY of it (a worker node runs another
    gateway's card, :mod:`hermes_cli.kanban_worker_node`); None for this gateway's own cards or unsynced boards."""
    try:
        from hermes_cli import kanban_db_sync as sync
        st = sync.sync_state(conn)
        if not st or "created_on" not in sync.table_columns(conn, "tasks"):
            return None
        row = conn.execute("SELECT created_on FROM tasks WHERE id = ?", (task_id,)).fetchone()
    except Exception:  # health: allow BLE001 -- an unreadable sync state means "not a copy"
        return None
    home = row[0] if row else None
    return str(home) if home and home != st.get("gateway_id") else None


def _own_gateway(conn) -> Optional[str]:
    """This board file's Board gateway id (the ``machine:<name>`` this gateway leases as), None if unsynced."""
    try:
        from hermes_cli import kanban_db_sync as sync
        st = sync.sync_state(conn)
        return str(st["gateway_id"]) if st and st.get("gateway_id") else None
    except Exception:  # health: allow BLE001 -- an unreadable sync state means "unsynced board"
        return None


def _briefly_stale(exc: CapabilityRouteError, now: float) -> bool:
    """True when the newest stale holder record lapsed within the last ``RELAY_RETRY_S`` (a restart or a
    short sleep): worth a timed retry. Older data means the holder's probe is down; that still refuses."""
    ats = [float(at) for _gw, at in (exc.details.get("stale") or []) if at]
    return bool(ats) and now - max(ats) < reg.stale_after() + RELAY_RETRY_S


def _park_relay(conn, task_id: str, needs: list[str], *, reason: str, gateway: str, mode: str,
                generations: dict, now: float, expected_run_id: Optional[int], require_unclaimed: bool,
                self_gw: Optional[str] = None, why: str = "") -> dict:
    """Park ``task_id`` on owner request ids that ``gateway`` will route (:func:`owner_status` there).

    ``mode`` RELAY_HOME: a worker node hands the routing to the card's home gateway. RELAY_RETRY: this
    gateway retries a stale holder itself. Same ids, wake_check and event shape as a direct route, plus
    ``relay`` and the reason the probe needs to write the owner card.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_timed_park as ktp

    me = self_gw or reg.self_gateway() or _own_gateway(conn) or "this gateway"
    routes = [{"need": n, "gateway": gateway, "checked_at": None, "request_id": request_id(task_id, n, generations.get(n, 1)),
               "generation": generations.get(n, 1), "relay": mode, "relayed_at": int(now), "from_gateway": me}
              for n in needs]
    ids = ",".join(r["request_id"] for r in routes)
    # [daveey] An owner card lands on the hub only when the hub is the card's home and holds the need
    # (_local_owner there). Judge exactly that card's text now, never in the wait-check probe that
    # applies the relay (its 30 s timeout cannot hold a 30 s judge call), and store the judged text
    # on the route so the probe creates that very card. A non-hub home makes no model call (a hub
    # it routes to judges on its own create). The verdict event is recorded only where it lands.
    from hermes_cli.kanban_hub_admission_placement import owner_verdict
    waiting = kb.get_task(conn, task_id)
    excerpt = redact_sensitive_text((reason or "").strip(), force=True)[:3000]
    # RELAY_RETRY on the hub parks on the hub itself; a later capability_held_locally there reaches
    # _local_owner, so it needs the same relay-time verdict (else it loops to generation 5).
    hub_home = (mode == RELAY_HOME and _is_hub_gateway(gateway)) or (mode == RELAY_RETRY and _is_hub_gateway(me))
    if waiting is not None and hub_home and not conn.in_transaction:
        for r in routes:
            # Built from the stored excerpt, exactly as the hub rebuilds it from the route entry.
            r["owner_title"], r["owner_body"] = local_owner_text(waiting, r["need"], self_gw=gateway,
                                                                 from_gw=me, reason=excerpt)
            r["hub_admission"] = owner_verdict(r["owner_title"], r["owner_body"], need=r["need"])
    until = datetime.fromtimestamp(int(now) + WAKE_HORIZON, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if mode == RELAY_HOME:
        summary = (f"relayed {', '.join(needs)} to {gateway}, this card's home gateway: {me} is a Board worker "
                   f"node with no hermes-hub registry, so {gateway} opens the owner card(s) with its own")
    else:
        summary = (f"the holder of {', '.join(needs)} was not live-checked recently ({why[:300]}); this gateway "
                   f"retries routing every {WAKE_EVERY // 60} min for {RELAY_RETRY_S // 60} min")
    park_reason = (f"Waiting on capability owner card(s): {summary} (request {ids}). Wakes when they are done. "
                   f"SCHEDULED_UNTIL={until}\n{(reason or '').strip()[:1500]}")
    wake_check = f"hermes kanban wait-check capability-owner {ids}"
    with kb.write_txn(conn):
        cur = conn.execute("SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if require_unclaimed and (cur is None or cur[0] != "ready" or cur[1] is not None):
            raise CapabilityRouteError("capability_route_invalid_state",
                                       f"{task_id} was claimed meanwhile; not parked", details={"routes": routes})
        source = kb._retry_status_for_run(conn, task_id) if cur and cur[0] == "running" else "ready"
        # The event first: the park's producer check (kanban_awaiting) reads it through owner_status.
        kb._append_event(conn, task_id, ROUTED_EVENT, {
            "routes": routes, "local": [], "self_gateway": me, "relay": mode, "reason": excerpt,
            "awaiting": [{"kind": "card", "ref": f"{gateway}/request/{r['request_id']}",
                          "why": f"capability {r['need']} ({mode} relay)"} for r in routes],
        })
        ok = ktp.park(conn, task_id, reason=park_reason, requested_kind="capability", source_status=source,
                      expected_run_id=expected_run_id, wake_check=wake_check, check_every=WAKE_EVERY,
                      awaiting={"kind": "card", "ref": routes[0]["request_id"], "gateway": gateway,
                                "why": f"capability owner for {', '.join(needs)}, routed by {gateway}"},
                      # [daveey] A home relay is routed by the home's registry: the home runs the probe.
                      probe_host=gateway if mode == RELAY_HOME else None)
        if not ok:
            raise CapabilityRouteError("capability_route_invalid_state",
                                       f"{task_id} could not be parked (claim lost?)", details={"routes": routes})
    return {"status": "scheduled", "routes": routes, "wake_check": wake_check, "local": [], "relay": mode}


def _route_generations(conn, task_id: str, needs: list[str]) -> dict[str, int]:
    """Request-id generation per need; raises the typed refusal for a need already routed (or failed)."""
    previous = _previous_routes(conn, task_id)
    generations: dict[str, int] = {}
    for need in needs:
        p = previous.get(need)
        if p is None:
            continue
        if p.get("failed_code") == "hub_admission_unavailable" and int(p.get("generation") or 1) < 5:
            # [daveey] The judge was down when the route was relayed: a retry is a fresh relay
            # (judged again at relay time), never a permanent route answer.
            generations[need] = int(p.get("generation") or 1) + 1
            continue
        if p.get("failed_code"):
            # A relayed route that found no holder within RELAY_RETRY_S: the typed answer, not a re-relay.
            raise CapabilityRouteError(p["failed_code"], str(p.get("failed") or "")[:1500],
                                       details={"need": need, "request_id": p.get("request_id")})
        sent = _sent(p.get("request_id") or "")
        gen = int(p.get("generation") or 1)
        if sent is not None and sent["state"] in ("rejected", "expired") and gen < 2:
            generations[need] = gen + 1  # the owner never got a card: one fresh attempt
            continue
        raise CapabilityRouteError(
            "capability_route_exhausted",
            f"{task_id} already routed {need} to {p.get('gateway')} (hermes-hub request "
            f"{p.get('request_id')}, {sent['state'] if sent else 'state unknown'}). That owner card must "
            f"finish or report why; check it with `hermes kanban wait-check capability-owner "
            f"{p.get('request_id')}` and `hermes-hub requests`. If it is done and the grant was "
            "insufficient, comment the exact gap on it and reopen it there.",
            details={"need": need, **p})
    return generations


def _relay_home(conn, task_id: str, needs: list[str]) -> str:
    """The home gateway a worker node relays ``task_id`` to; typed refusal for the worker's own card."""
    home = card_home(conn, task_id)
    if home is None:
        raise CapabilityRouteError(
            "capability_registry_unavailable",
            f"this gateway has no hermes-hub replica (a Board worker node runs no hermes-hub sync), so it "
            f"cannot see which gateway holds {', '.join(needs)} or open an owner card there, and {task_id} "
            "was created here, so there is no home gateway to relay to. Create such work on a gateway "
            "with hermes-hub (hub, macbook) or pin it to the holder with runs_on=['machine:<holder>']. "
            "Never ask David to run the step: it is an agent route gap.",
            details={"needs": needs})
    return home


def route(conn, task_id: str, needs, *, reason: str, expected_run_id: Optional[int] = None,
          board: Optional[str] = None, now: Optional[float] = None, require_unclaimed: bool = False,
          **lookup) -> dict:
    """Route ``needs`` for a running/ready card: create owner cards, then park it on them.

    Raises CapabilityRouteError or ValueError. Refusals decided before any hermes-hub call write
    nothing. A failure after some owner requests were queued says so; retrying the same call is
    safe (exact resend of the stored envelopes). ``require_unclaimed`` (placement preflight) parks
    only a ``ready`` card nobody has claimed meanwhile.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_timed_park as ktp

    needs = reg.normalize_needs(needs)
    if not needs:
        raise ValueError("needs is empty")
    task = kb.get_task(conn, task_id)
    if task is None:
        raise ValueError(f"unknown task {task_id}")
    if task.status not in ("running", "ready"):
        raise CapabilityRouteError("capability_route_invalid_state",
                                   f"{task_id} is {task.status}; only a running or ready card can be routed")
    now = time.time() if now is None else now
    generations = _route_generations(conn, task_id, needs)
    if not has_replica():
        # A Board worker node (no hermes-hub sync): hand the routing to the card's home gateway.
        return _park_relay(conn, task_id, needs, reason=reason, gateway=_relay_home(conn, task_id, needs),
                           mode=RELAY_HOME, generations=generations, now=now, expected_run_id=expected_run_id,
                           require_unclaimed=require_unclaimed, self_gw=lookup.get("self_gw"))
    self_gw = lookup.pop("self_gw", None) or reg.self_gateway()
    if not self_gw:
        raise CapabilityRouteError("capability_hub_unavailable",
                                   "this gateway has no hermes-hub identity (~/.config/hermes-mail/config.json)")
    # A need whose request for this generation is already queued (an earlier attempt failed after
    # sending it) routes from the stored envelope, whatever the registry says now: the owner card
    # exists or will, and it must not be orphaned by a refusal on retry.
    queued: list[dict] = []
    fresh_needs: list[str] = []
    for need in needs:
        stored = _sent(request_id(task_id, need, generations.get(need, 1)))
        if stored is not None and stored["state"] not in ("rejected", "expired"):
            queued.append({"need": need, "gateway": stored["body"].get("to_gw"), "checked_at": None})
        else:
            fresh_needs.append(need)
    if fresh_needs:
        try:
            decided = plan(fresh_needs, now=now, self_gw=self_gw, **lookup)
        except CapabilityRouteError as exc:
            if exc.code == "capability_registry_stale" and not queued and _briefly_stale(exc, now):
                # The holder's record lapsed just now (restart, sleep): wait and retry routing on this
                # gateway's own probe instead of failing the worker. Hours-old data still refuses.
                return _park_relay(conn, task_id, fresh_needs, reason=reason, gateway=self_gw, mode=RELAY_RETRY,
                                   generations=generations, now=now, expected_run_id=expected_run_id,
                                   require_unclaimed=require_unclaimed, self_gw=self_gw, why=str(exc))
            if not (queued and exc.code == "capability_held_locally"):
                raise
            decided = {"routes": [], "local": exc.details.get("local", [])}
    else:
        decided = {"routes": [], "local": []}
    decided["routes"] = queued + decided["routes"]
    board_slug = board or kb.get_current_board() or "default"
    hub = _hub_cli()
    routes: list[dict] = []
    for r in decided["routes"]:
        gen = generations.get(r["need"], 1)
        rid = request_id(task_id, r["need"], gen)
        stored = _sent(rid)
        if stored is not None:
            # Exact resend of what was already queued: same gateway, title and body.
            env = stored["body"]
            args = env.get("args") or {}
            gw, title, body = env.get("to_gw") or r["gateway"], args.get("title") or "", args.get("body") or ""
            assignee = args.get("assignee") or _owner_assignee()
            overlap_opts = _overlap_option_argv(args, overlap_exclusions(conn, task_id))
        else:
            gw = r["gateway"]
            title = redact_sensitive_text(
                f"Capability owner: {r['need']} for {self_gw}/{task_id} ({(task.title or '')[:80]})", force=True)
            body = owner_body(task, r["need"], self_gw=self_gw, board=board_slug, reason=reason)
            assignee = _owner_assignee()
            overlap_opts = _overlap_option_argv(None, overlap_exclusions(conn, task_id))
        argv = [hub, "create-task", gw, title, "--body", body, "--assignee", assignee,
                "--origin-session", "", "--id", rid, *overlap_opts]
        if stored is None and _is_hub_gateway(gw):
            # [daveey] An owner card bound for the hub must convince the admission judge first;
            # a denied one is refused visibly (the waiting card's worker re-plans with hub-admin).
            from hermes_cli.kanban_hub_admission_placement import gate_owner
            audit = gate_owner(conn, task_id, title, body, need=r["need"])
            if not audit["admit"]:
                done = ", ".join(x["request_id"] for x in routes)
                raise CapabilityRouteError(
                    "hub_admission_denied",
                    f"owner card for {r['need']} was not sent to the hub: hub admission denied "
                    f"({audit['why']}). Pool workers have hub ssh + hub-admin (capability:hub-write): "
                    "do the action from a worker, or give the waiting card a hub_reason."
                    + (f" Already queued: {done}." if done else ""),
                    details={"need": r["need"], "gateway": gw, "hub_admission": audit,
                             "queued": [x["request_id"] for x in routes]})
        rc, out = _create_task(argv)
        # 0 = sent, 3 = queued locally (hub unreachable; sent on the next sync). Both are durable.
        if rc not in (0, 3):
            done = ", ".join(x["request_id"] for x in routes)
            raise CapabilityRouteError(
                "capability_hub_unavailable",
                f"hermes-hub create-task to {gw} failed (exit {rc}): {out[-400:]}"
                + (f". Already queued: {done}; retrying this call resends them unchanged." if done else ""),
                details={"need": r["need"], "gateway": gw, "queued": [x["request_id"] for x in routes]})
        routes.append({**r, "gateway": gw, "request_id": rid, "generation": gen, "queued_only": rc == 3})
    ids = ",".join(x["request_id"] for x in routes)
    until = datetime.fromtimestamp(int(now) + WAKE_HORIZON, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary = "; ".join(f"{x['need']} on {x['gateway']} (request {x['request_id']})" for x in routes)
    park_reason = (f"Waiting on capability owner card(s): {summary}. Wakes when they are done. "
                   f"SCHEDULED_UNTIL={until}\n{(reason or '').strip()[:1500]}")
    wake_check = f"hermes kanban wait-check capability-owner {ids}"
    with kb.write_txn(conn):
        cur = conn.execute("SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if require_unclaimed and (cur is None or cur[0] != "ready" or cur[1] is not None):
            raise CapabilityRouteError(
                "capability_route_invalid_state",
                f"{task_id} was claimed while its owner request(s) {ids} were queued; not parked. The "
                "worker will hit the wall and can block with needs (the same owner request is reused).",
                details={"routes": routes})
        source = kb._retry_status_for_run(conn, task_id) if cur and cur[0] == "running" else "ready"
        first = routes[0]
        ok = ktp.park(conn, task_id, reason=park_reason, requested_kind="capability", source_status=source,
                      expected_run_id=expected_run_id, wake_check=wake_check, check_every=WAKE_EVERY,
                      awaiting={"kind": "card", "ref": first["request_id"], "gateway": first["gateway"],
                                "why": f"capability owner for {', '.join(x['need'] for x in routes)}"})
        if not ok:
            raise CapabilityRouteError(
                "capability_route_invalid_state",
                f"owner request(s) {ids} were queued but {task_id} could not be parked (claim lost?)",
                details={"routes": routes})
        kb._append_event(conn, task_id, ROUTED_EVENT, {
            "routes": routes, "local": decided["local"], "self_gateway": self_gw,
            # A named live producer for the park (see the no-producer-no-park contract).
            "awaiting": [{"kind": "card", "ref": f"{x['gateway']}/request/{x['request_id']}",
                          "why": f"capability {x['need']}"} for x in routes],
        })
    return {"status": "scheduled", "routes": routes, "wake_check": wake_check, "local": decided["local"]}


def _access_labels() -> dict:
    from hermes_cli.kanban_placement_coordination import ACCESS_LABELS
    return ACCESS_LABELS


def _task_runs_on(conn, task) -> set:
    """The card's ``runs_on`` labels (empty when unreadable or the board has no such column)."""
    value = getattr(task, "runs_on", None)
    if value is None and conn is not None:
        try:
            row = conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (task.id,)).fetchone()
            value = row[0] if row else None
        except Exception:  # health: allow BLE001 -- no column / unreadable: no labels, registry check stays
            value = None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    return {str(v) for v in value} if isinstance(value, (list, tuple)) else set()


def preflight(conn, task_id: str, *, board: Optional[str] = None, **lookup) -> Optional[dict]:
    """Placement preflight for a just-created card whose ``requires`` declare gateway needs.

    Pre-routes needs this gateway lacks but another holds (owner card first, the card waits).
    Returns the route result, a ``{"warning": ...}`` dict when nothing can be routed, or None
    when there is nothing to do. Never raises.
    """
    from hermes_cli import kanban_db as kb
    try:
        task = kb.get_task(conn, task_id)
        if task is None:
            return None
        needs = needs_from_requires(task.requires)
        if getattr(task, "placement_policy", None) == "worker-first-v1":
            # Enrollment checked the pool; pickup/execution recheck the actual
            # worker. Do not create an origin-side owner for its remote access.
            from hermes_cli.kanban_placement_coordination import ACCESS_LABELS
            needs = [need for need in needs if need not in ACCESS_LABELS]
        elif any(need in _access_labels() for need in needs):
            # The Board enforces a carried access label at claim time and no gateway advertises these
            # needs in the registry, so a registry check would warn no_capability_holder falsely.
            carried = _task_runs_on(conn, task)
            needs = [need for need in needs if _access_labels().get(need) not in carried]
        if not needs:
            return None
        if task.status != "ready":
            return {"warning": f"declared needs {needs} not preflighted: card is {task.status}"}
        if _previous_routes(conn, task_id):
            return None  # an idempotent create retry: already pre-routed
        return route(conn, task_id, needs, reason=(task.body or task.title or "")[:3000],
                     board=board, require_unclaimed=True, **lookup)
    except CapabilityRouteError as exc:
        if exc.code == "capability_held_locally":
            return None
        return {"warning": str(exc), "code": exc.code}
    except Exception as exc:
        return {"warning": f"capability preflight failed: {exc}"}


# --- wait-check ----------------------------------------------------------------------------

_OUTPUT_ID_RE = re.compile(r'"id"\s*:\s*"(t_[0-9a-f]+)"')


def owner_task_id(result: Any) -> Optional[str]:
    """The owner card id in an applied create's result, whichever applier wrote it: ``task`` (current
    hermes-hub), ``task_id`` (placement creates), ``id``, or the ``hermes kanban create --json`` text an
    older applier stored in ``output``, clipped to 2000 chars and so often not valid JSON (hub card
    t_01a181a33c586660). None when no shape holds an id."""
    if not isinstance(result, dict):
        return None
    for key in ("task", "task_id", "id"):
        val = result.get(key)
        if isinstance(val, str) and val:
            return val
    out = result.get("output")
    if not isinstance(out, str) or not out:
        return None
    try:
        parsed = json.loads(out)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        for key in ("id", "task_id"):
            val = parsed.get(key)
            if isinstance(val, str) and val:
                return val
    m = _OUTPUT_ID_RE.search(out)
    return m.group(1) if m else None


def owner_status(rid: str, conn=None) -> tuple[int, str]:
    """(rc, message) for one owner request: 0 done, 1 pending, 2 cannot tell / rejected.

    The hermes-hub ``sent`` row decides when this gateway sent the request. Otherwise a relayed route for
    ``rid`` (see the module doc) is applied or tracked here: the home gateway routes it on its first probe.
    ``conn`` is the board holding the waiting card (default: the probe's ``HERMES_KANBAN_DB``). [daveey]
    """
    status, why = _sent_status(rid)
    if status is not None:
        return status
    relayed = _relay_status(rid, conn)
    return relayed if relayed is not None else (2, why)


def _sent_status(rid: str) -> tuple[Optional[tuple[int, str]], str]:
    """``((rc, message), "")`` from this gateway's hermes-hub ``sent`` row, or ``(None, why)`` without one."""
    try:
        db = reg._ro(reg.hub_db_path())
    except Exception as exc:  # health: allow BLE001 -- unreadable replica: fall through to the relay check
        return None, f"error: hermes-hub replica unreadable: {exc}"
    try:
        row = db.execute("SELECT body, state, result FROM sent WHERE id = ?", (rid,)).fetchone()
        if row is None:
            return None, f"error: no hermes-hub request {rid} on this gateway"
        body = json.loads(row[0] or "{}")
        state = row[1]
        if state in ("rejected", "expired", "failed"):
            return (2, f"error: request {rid} {state}: {str(row[2])[:300]}"), ""
        if state != "done":
            return (1, f"{rid} request {state}"), ""
        result = json.loads(row[2] or "{}") or {}
        tid = owner_task_id(result)
        if not tid:
            return (2, f"error: request {rid} done without a task id: {str(row[2])[:300]}"), ""
        gw = body.get("to_gw")
        board = (body.get("key") or "default/default/new").split("/")[1] if body.get("key") else "default"
        rec = db.execute("SELECT data FROM replica WHERE gw = ? AND coll = 'tasks' AND key = ?",
                         (gw, f"default/{board}/{tid}")).fetchone()
        if rec is None:
            return (1, f"{rid} -> {gw}/{tid} not replicated yet"), ""
        status = json.loads(rec[0]).get("status")
        return ((0 if status in ("done", "archived") else 1), f"{rid} -> {gw}/{tid} {status}"), ""
    except Exception as exc:  # health: allow BLE001 -- cannot tell, never "not yet"; reported as rc 2
        return (2, f"error: {exc}"), ""
    finally:
        db.close()


def _task_of(rid: str) -> Optional[str]:
    """The waiting card id inside ``capability-owner:<card>:<need>``."""
    if not rid.startswith(OWNER_PREFIX):
        return None
    tid = rid[len(OWNER_PREFIX):].split(":", 1)[0]
    return tid or None


def _route_entry(conn, task_id: str, rid: str) -> tuple[Optional[dict], Optional[dict]]:
    """The newest ``capability_routed`` route for ``rid`` and its event payload (None, None if never routed)."""
    entry = payload = None
    for (raw,) in conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
                               (task_id, ROUTED_EVENT)):
        try:
            p = json.loads(raw or "{}")
        except ValueError:
            continue
        for r in p.get("routes") or []:
            if isinstance(r, dict) and r.get("request_id") == rid:
                entry, payload = r, p
    return entry, payload


def _relay_status(rid: str, conn=None) -> Optional[tuple[int, str]]:
    """Track or apply a relayed route for ``rid``; None when ``rid`` was never relayed on this board."""
    from hermes_cli import kanban_db as kb

    tid = _task_of(rid)
    if not tid:
        return None
    own = conn is None
    if own:
        try:
            from hermes_cli import kanban_db_connect as kbc
            conn = kbc.connect()
        except Exception:  # health: allow BLE001 -- no board: not a relay we can see
            return None
    try:
        local = conn.execute("SELECT id, status FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
                             "ORDER BY created_at DESC LIMIT 1", (rid,)).fetchone()
        if local is not None:
            return (0 if local[1] == "done" else 1), f"{rid} -> local owner {local[0]} {local[1]}"
        entry, payload = _route_entry(conn, tid, rid)
        if entry is None:
            return None
        if entry.get("failed_code"):
            return 0, f"{rid} capability route failed: {entry.get('failed')}"
        if entry.get("owner_task"):  # a local owner card that was since archived
            return 2, f"error: {rid} local owner {entry['owner_task']} is gone"
        if not entry.get("relay"):
            return None
        task = kb.get_task(conn, tid)
        if task is None or card_home(conn, tid) is not None or not has_replica():
            # Still the worker node's copy: the home gateway's probe routes it once the copy is handed back.
            return 1, f"{rid} relayed to {entry.get('gateway')}; it routes there"
        return _apply_relay(conn, task, entry, payload or {})
    finally:
        if own:
            conn.close()


def _relay_fail(conn, task_id: str, entry: dict, code: str, message: str) -> tuple[int, str]:
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn):
        kb._append_event(conn, task_id, ROUTED_EVENT, {"routes": [{**entry, "failed_code": code,
                                                                   "failed": message[:1500]}]})
    return 0, f"{entry['request_id']} capability route failed: {code}: {message[:600]}"


def _apply_relay(conn, task, entry: dict, payload: dict, *, now: Optional[float] = None) -> tuple[int, str]:
    """On the gateway that holds the waiting card: route one relayed need with this gateway's registry."""
    from hermes_cli import kanban_db as kb

    now = time.time() if now is None else now
    need, rid = entry["need"], entry["request_id"]
    self_gw = reg.self_gateway() or _own_gateway(conn) or "this gateway"
    from_gw = entry.get("from_gateway") or payload.get("self_gateway") or self_gw
    reason = payload.get("reason") or ""
    board = kb.get_current_board() or "default"
    age = now - float(entry.get("relayed_at") or now)
    try:
        decided = plan([need], now=now, self_gw=self_gw)
    except CapabilityRouteError as exc:
        if exc.code == "capability_held_locally":
            return _local_owner(conn, task, entry, need=need, rid=rid, self_gw=self_gw, from_gw=from_gw,
                                reason=reason)
        if exc.code in ("capability_registry_stale", "no_capability_holder") and age < RELAY_RETRY_S:
            return 1, f"{rid}: {exc.code}, retrying for {int((RELAY_RETRY_S - age) // 60)} more min"
        return _relay_fail(conn, task.id, entry, exc.code, str(exc))
    gw = decided["routes"][0]["gateway"]
    title = redact_sensitive_text(f"Capability owner: {need} for {self_gw}/{task.id} ({(task.title or '')[:80]})",
                                  force=True)
    body = owner_body(task, need, self_gw=self_gw, board=board, reason=reason)
    argv = [_hub_cli(), "create-task", gw, title, "--body", body, "--assignee", _owner_assignee(),
            "--origin-session", "", "--id", rid, *_overlap_option_argv(None, overlap_exclusions(conn, task.id))]
    # [daveey] An owner sent to the hub is judged there, on this exact text, by its own create
    # (hermes kanban create's admission preflight), outside this probe's timeout.
    rc, out = _create_task(argv)
    if rc not in (0, 3):
        if age < RELAY_RETRY_S:
            return 1, f"{rid}: hermes-hub create-task to {gw} failed (exit {rc}), retrying: {out[-200:]}"
        return _relay_fail(conn, task.id, entry, "capability_hub_unavailable",
                           f"hermes-hub create-task to {gw} failed (exit {rc}): {out[-400:]}")
    with kb.write_txn(conn):
        kb._append_event(conn, task.id, ROUTED_EVENT, {
            "routes": [{"need": need, "gateway": gw, "request_id": rid, "generation": entry.get("generation", 1),
                        "queued_only": rc == 3, "relayed_from": from_gw}],
            "local": [], "self_gateway": self_gw,
            "awaiting": [{"kind": "card", "ref": f"{gw}/request/{rid}", "why": f"capability {need}"}],
        })
    return 1, f"{rid} sent to {gw} (relayed from {from_gw})"


def local_owner_text(task, need: str, *, self_gw: str, from_gw: str, reason: str) -> tuple[str, str]:
    """Title and body of the owner card the waiting card's own gateway opens for ``need``.

    Shared by the relay-time judge and :func:`_local_owner`, so the judged text is the card's text.
    """
    excerpt = redact_sensitive_text((reason or "").strip(), force=True)[:3000]
    title = redact_sensitive_text(f"Capability owner: {need} for {task.id} ({(task.title or '')[:80]})", force=True)
    body = (
        f"Capability owner card for waiting card {task.id} (\"{redact_sensitive_text((task.title or '')[:160], force=True)}\") "
        f"on this board. It was running on {from_gw}, which does not hold `{need}`; this gateway ({self_gw}) "
        "holds it (live-checked).\n\n"
        "## Action (from the waiting card, inside its approved plan)\n"
        f"{excerpt or '(no reason given; read the waiting card first)'}\n\n"
        "## Do\n"
        f"1. Read the waiting card: kanban_show(task_id='{task.id}').\n"
        f"2. Perform exactly the scoped action above on this machine with `{need}`. Keep it to the minimum the "
        "waiting card needs.\n"
        "3. If the action goes beyond the waiting card's approved plan or scope, do NOT do it: block THIS card "
        "with kind='needs_input' and a human_request naming the exact command. The waiting worker does not ask "
        "David; this card does.\n\n"
        "## Readback\n"
        f"Record the evidence in your completion summary AND kanban_comment(task_id='{task.id}', body=<result>) "
        "so the waiting worker sees it.\n\n"
        "## Wake\n"
        f"{task.id} is parked on this card: completing it wakes {task.id} within about {WAKE_EVERY // 60} min, "
        f"and it resumes on {from_gw}.\n"
    )
    return title, body


def _local_owner(conn, task, entry: dict, *, need: str, rid: str, self_gw: str, from_gw: str,
                 reason: str) -> tuple[int, str]:
    """The waiting card's own gateway holds the need: the owner card is a local card pinned to this machine."""
    from hermes_cli import kanban_db as kb

    title, body = local_owner_text(task, need, self_gw=self_gw, from_gw=from_gw, reason=reason)
    own = _own_gateway(conn)
    if _is_hub_gateway(self_gw) or (own and _is_hub_gateway(own)):
        # [daveey] The hub holds the need: the owner stays here only if admitted; a denied owner
        # enrolls in the worker pool with the hub-write requirement (Board: capability:hub-write).
        # The card is created with exactly the text judged at relay time.
        from hermes_cli.kanban_hub_admission_placement import gate_owner, mismatched_owner_verdict, \
            stored_owner_verdict
        from hermes_cli.kanban_worker_placement import POLICY
        stored = entry.get("owner_title"), entry.get("owner_body")
        if all(isinstance(s, str) for s in stored):
            # Never trust relayed text verbatim: rebuild it from the entry's own fields (redaction,
            # caps and the least-privilege template included) and require equality; a mismatch is a
            # final deny to the pool, created with the rebuilt text.
            title, body = local_owner_text(task, need, self_gw=str(entry.get("gateway") or self_gw),
                                           from_gw=str(entry.get("from_gateway") or from_gw), reason=reason)
            audit = (stored_owner_verdict(entry, title, body) if stored == (title, body)
                     else mismatched_owner_verdict(entry, need))
        else:
            audit = stored_owner_verdict(entry, title, body)
        if audit is None:  # never judged inside this probe; never a permanent route either
            return _relay_fail(conn, task.id, entry, "hub_admission_unavailable",
                               f"owner card for {need} was not opened on the hub: no hub admission verdict was "
                               "recorded at relay time (judge unavailable). Route the need again to retry.")
        gate_owner(conn, task.id, title, body, need=need, audit=audit)
        if not audit["admit"]:
            owner = kb.create_task(conn, title=title, body=body, assignee=_owner_assignee(),
                                   created_by="capability-relay", idempotency_key=rid,
                                   requires=["hub:write"], placement_policy=POLICY,
                                   placement_audit={"hub_admission": audit})
            with kb.write_txn(conn):
                kb._append_event(conn, task.id, ROUTED_EVENT, {
                    "routes": [{"need": need, "gateway": "worker-pool", "request_id": rid,
                                "generation": entry.get("generation", 1), "owner_task": owner,
                                "relayed_from": from_gw, "hub_admission": "denied"}],
                    "local": [need], "self_gateway": self_gw,
                    "awaiting": [{"kind": "card", "ref": owner, "why": f"capability {need}"}],
                })
            return 1, f"{rid} -> pool owner {owner} (hub admission denied: {audit['why']})"
    try:
        owner = kb.create_task(conn, title=title, body=body, assignee=_owner_assignee(),
                               created_by="capability-relay", idempotency_key=rid,
                               runs_on=[f"machine:{own}"] if own else None)
    except ValueError:  # an unsynced board has no runs_on: the card is local anyway
        owner = kb.create_task(conn, title=title, body=body, assignee=_owner_assignee(),
                               created_by="capability-relay", idempotency_key=rid)
    with kb.write_txn(conn):
        kb._append_event(conn, task.id, ROUTED_EVENT, {
            "routes": [{"need": need, "gateway": self_gw, "request_id": rid, "generation": entry.get("generation", 1),
                        "owner_task": owner, "relayed_from": from_gw}],
            "local": [need], "self_gateway": self_gw,
            "awaiting": [{"kind": "card", "ref": owner, "why": f"capability {need}"}],
        })
    return 1, f"{rid} -> local owner {owner} (this gateway holds {need})"


def _rejected(rid: str) -> Optional[str]:
    """The holder's refusal when request ``rid`` ended in a terminal state other than done, else None.

    A rejected owner request will never make a card, so the waiting card must not keep probing it: wait-check
    wakes the card at once with this text (exit 0), never the "cannot tell" exit 2 that only woke it after three
    strikes as ``wake_check_broken`` (hub t_01a1d49003c65ca4: create_overlap on macbook, woken 4 min later with
    nothing done and no rejection shown)."""
    from hermes_cli.kanban_placement import FAILED_STATES, remote_rejection
    why = remote_rejection(rid)
    if not why or why.get("state") not in FAILED_STATES:
        return None
    matches = ", ".join(f"{m.get('id')} on {m.get('gateway') or '?'} ({m.get('status') or '?'})"
                        for m in (why.get("matches") or [])[:3])
    gw = ((_sent(rid) or {}).get("body") or {}).get("to_gw") or "the holder"
    state = why["state"]
    outcome = {"rejected": f"was rejected by {gw}", "expired": f"expired before {gw} answered it"}.get(
        state, f"failed on {gw}")
    head = (f"capability owner request {rid} {outcome} ({why.get('code') or state}"
            + (f": matches {matches}" if matches else "") + f"): {str(why.get('error') or '')[:400].rstrip('.')}. "
            "No owner card exists, so this card was woken to re-plan. ")
    gen = _rid_generation(rid)
    if state in ("rejected", "expired") and gen < 2:
        # Mirrors _route_generations: only a first-generation rejected/expired request earns a fresh one.
        return head + (f"Blocking again with the same needs routes one fresh request (generation {gen + 1}); "
                       "if the refusal names a card that really owns this work, continue that card instead.")
    return head + ("This need's route is exhausted: blocking again with the same needs is refused as "
                   "capability_route_exhausted" + (f" (generation {gen} was the retry)" if gen >= 2 else "")
                   + ". If the refusal names a card that really owns this work, continue that card or link it "
                   "as a parent; otherwise fix the cause the refusal names (comment it on this card), or, if "
                   "only David can clear it, block kind='needs_input' with a human_request.")


def _rid_generation(rid: str) -> int:
    """The generation :func:`request_id` encoded in ``rid`` (its ``::g<N>`` suffix; 1 without one)."""
    m = re.search(r"::g(\d+)$", rid or "")
    return int(m.group(1)) if m else 1


def wait_check(target: str) -> tuple[int, str]:
    rids = [r for r in (target or "").split(",") if r.strip()]
    if not rids:
        return 2, "error: no request id"
    rejected = [msg for msg in (_rejected(r.strip()) for r in rids) if msg]
    if rejected:
        # Terminal: wake now with the refusal (a pending sibling request does not hold the card on a dead one).
        return 0, "; ".join(rejected)
    results = [owner_status(r.strip()) for r in rids]
    rc = 2 if any(r[0] == 2 for r in results) else (0 if all(r[0] == 0 for r in results) else 1)
    return rc, "; ".join(m for _, m in results)
