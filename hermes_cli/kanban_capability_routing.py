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
* ``capability_registry_stale`` -- a holder exists but was not live-checked recently; stale
  registry data never routes.
* ``capability_held_locally`` -- this gateway holds it: the wall is the worker's profile
  (kind='assignment') or a real failure, not a missing capability.
* ``capability_route_exhausted`` -- this card already routed that need once; the existing owner
  card must finish or report why. A REJECTED owner request may be re-routed once more (a new
  generation of the request id).

Retries are safe: the owner envelope is a pure function of (card, need, reason), and an id that
was already sent is re-sent from its stored envelope (same gateway), so ``hermes-hub`` sees an
exact retry, never a conflicting one.
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
            "block with kind='needs_input' and a human_request naming the exact command and scope.",
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
    previous = _previous_routes(conn, task_id)
    generations: dict[str, int] = {}
    for need in needs:
        p = previous.get(need)
        if p is None:
            continue
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
        else:
            gw = r["gateway"]
            title = redact_sensitive_text(
                f"Capability owner: {r['need']} for {self_gw}/{task_id} ({(task.title or '')[:80]})", force=True)
            body = owner_body(task, r["need"], self_gw=self_gw, board=board_slug, reason=reason)
            assignee = _owner_assignee()
        argv = [hub, "create-task", gw, title, "--body", body, "--assignee", assignee,
                "--origin-session", "", "--id", rid]
        rc, out = hub_runner(argv)
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


def owner_status(rid: str) -> tuple[int, str]:
    """(rc, message) for one owner request: 0 done, 1 pending, 2 cannot tell / rejected."""
    try:
        db = reg._ro(reg.hub_db_path())
    except Exception as exc:
        return 2, f"error: hermes-hub replica unreadable: {exc}"
    try:
        row = db.execute("SELECT body, state, result FROM sent WHERE id = ?", (rid,)).fetchone()
        if row is None:
            return 2, f"error: no hermes-hub request {rid} on this gateway"
        body = json.loads(row[0] or "{}")
        state = row[1]
        if state in ("rejected", "expired", "failed"):
            return 2, f"error: request {rid} {state}: {str(row[2])[:300]}"
        if state != "done":
            return 1, f"{rid} request {state}"
        result = json.loads(row[2] or "{}") or {}
        tid = owner_task_id(result)
        if not tid:
            return 2, f"error: request {rid} done without a task id: {str(row[2])[:300]}"
        gw = body.get("to_gw")
        board = (body.get("key") or "default/default/new").split("/")[1] if body.get("key") else "default"
        rec = db.execute("SELECT data FROM replica WHERE gw = ? AND coll = 'tasks' AND key = ?",
                         (gw, f"default/{board}/{tid}")).fetchone()
        if rec is None:
            return 1, f"{rid} -> {gw}/{tid} not replicated yet"
        status = json.loads(rec[0]).get("status")
        return (0 if status in ("done", "archived") else 1), f"{rid} -> {gw}/{tid} {status}"
    except Exception as exc:  # cannot tell, never "not yet"
        return 2, f"error: {exc}"
    finally:
        db.close()


def wait_check(target: str) -> tuple[int, str]:
    rids = [r for r in (target or "").split(",") if r.strip()]
    if not rids:
        return 2, "error: no request id"
    results = [owner_status(r.strip()) for r in rids]
    rc = 2 if any(r[0] == 2 for r in results) else (0 if all(r[0] == 0 for r in results) else 1)
    return rc, "; ".join(m for _, m in results)
