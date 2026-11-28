"""SessionState v2 reducer: pure ``inputs -> SessionState`` (design items 1, 2, 7). [daveey]

No I/O and no implicit clock: ``now`` is an argument, and every value derives from the collector's inputs
(``hermes_session_state_collect.collect``) and the store's persisted resolutions. Equal inputs and ``now`` give an
equal object, so the store's digest gate never churns ``rev``.

P1 ships :func:`legacy_adapter`: the session verdict is today's ``hermes_state_session_status.reduce_status`` over
the v1 snapshot assembled by the same code the v1 producer uses (``assemble``), and v2's card map, blocked-on
classification, counts/pips, renderer rule (design item 7) and ``next_change_at`` are derived from the same inputs.
P2 replaces it with the ported panel classifier through :func:`register_reducer`.

``next_change_at`` is the only timer the store runs: the earliest future instant at which this object changes with
no new event (a triage card crossing the 600 s red threshold, a clock wait's due time).
"""
from __future__ import annotations

import math
from typing import Any, Callable, Optional

from hermes_session_state_schema import (
    SECTIONS, Ask, Card, Header, Identity, Session, SessionState,
)

Reducer = Callable[..., SessionState]
_reducer: list[Optional[Reducer]] = [None]
_DONE = frozenset({"done", "archived"})
_STATUS_COLOUR = {"needs_you": "red", "stuck": "red", "attention": "amber", "running": "blue", "waiting": "blue",
                  "done": "green", "unknown": "grey", "muted": "grey", "idle": "grey"}
_WAIT_COLOUR = {"ci": "light_blue", "clock": "light_blue", "deploy": "purple"}


def register_reducer(fn: Optional[Reducer]) -> None:
    """Replace the reducer (``fn(inputs, *, now, resolutions) -> SessionState``); ``None`` restores the adapter."""
    if fn is not None and not callable(fn):
        raise ValueError("register_reducer needs a callable")
    _reducer[0] = fn


def reduce(inputs: dict[str, Any], *, now: float, resolutions: list[dict] | None = None) -> SessionState:
    return (_reducer[0] or legacy_adapter)(inputs, now=now, resolutions=list(resolutions or ()))


def _str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) else None


def _request(card: dict) -> dict:
    import json

    value = card.get("human_request")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _blocked_on(row: dict, *, human: bool, parents_open: bool) -> Optional[str]:
    status = str(row.get("status") or "unknown")
    wait = row.get("wait") if isinstance(row.get("wait"), dict) else {}
    if row.get("stale") or row.get("partial_source_unknown") or status == "unknown":
        return "unknown"
    if status in _DONE or status == "running":
        return None
    if human:
        return "human"
    if wait.get("wait_kind") in ("ci", "deploy", "clock"):
        return str(wait["wait_kind"])
    if status in ("todo", "blocked") and parents_open:
        return "card"
    if status == "ready":
        return "capacity"
    if status in ("triage", "review", "blocked", "scheduled"):
        return "agent"
    return "unknown"


def _detail(row: dict, blocked_on: Optional[str], parents: list[dict]) -> str:
    status = str(row.get("status") or "unknown")
    if blocked_on == "human":
        return _str(_request(row).get("action")) or "needs your answer"
    if blocked_on == "card":
        names = [_str(p.get("title")) or str(p.get("id")) for p in parents if p.get("status") not in _DONE]
        return "waiting on " + ", ".join(names[:3]) if names else "waiting on a dependency"
    if blocked_on in ("ci", "deploy", "clock"):
        return f"waiting on {blocked_on}"
    if blocked_on == "capacity":
        return "queued for a worker"
    if blocked_on == "unknown":
        return _str(row.get("partial_source_unknown")) or "status unknown"
    if blocked_on == "agent":
        return f"{status}: waiting on its agent"
    return status


def _card(row: dict, *, key: str, gateway: str, linked: bool, parents: list[str], parent_rows: list[dict],
          episode: Optional[str], resolutions: list[dict], now: float) -> Card:
    from hermes_cli.kanban_open_asks import card_is_open_human_ask
    from hermes_cli.kanban_triage_attention import overdue

    expiry = _num(row.get("source_expires_at"))
    if expiry is not None and now >= expiry:
        row = {**row, "stale": True}
    status = str(row.get("status") or "unknown")
    human = card_is_open_human_ask(status, row.get("block_kind"), row.get("human_request")) \
        and not row.get("retained_wait")
    blocked_on = _blocked_on(row, human=human, parents_open=any(p.get("status") not in _DONE for p in parent_rows))
    resolution = next((r for r in reversed(resolutions) if r.get("card") == key and episode is not None
                       and r.get("episode") == episode and r.get("delivery") == "committed"), None)
    stale = bool(row.get("stale") or row.get("partial_source_unknown"))
    red = (blocked_on == "human" or (status == "triage" and overdue(row, now=now))) and resolution is None \
        and not stale and row.get("via") != "subscribed"
    detail = _detail(row, blocked_on, parent_rows)
    if resolution is not None:
        detail = f"resolved by {resolution.get('by') or 'you'} · {resolution.get('label') or resolution.get('action')}"
    wait = row.get("wait") if isinstance(row.get("wait"), dict) else {}
    run = {"id": row["current_run_id"], "started_at": _num(row.get("current_run_started_at"))} \
        if row.get("current_run_id") else None
    return Card(key=key, id=str(row.get("id") or ""), board=str(row.get("board") or "default"), gateway=gateway,
                title=_str(row.get("title")) or str(row.get("id") or ""), lifecycle=status, blocked_on=blocked_on,
                detail=detail, parents=parents, linked=linked, via=row.get("via") if linked else None, run=run,
                source={"freshness": "unknown" if stale else ("stale" if row.get("stale") else "live"),
                        "synced_at": _num(row.get("synced_at"))},
                block_episode=episode, resolution=dict(resolution) if resolution else None, red=red,
                eta=_num(wait.get("until") or wait.get("eta")))


def _cards(inputs: dict, resolutions: list[dict], now: float) -> dict[str, Card]:
    gateway = str(inputs["parts"]["identity"].get("gateway") or "local")
    graph = inputs["dependency_graph"]
    rows = graph["rows"]
    out: dict[str, Card] = {}
    for key, row in sorted(rows.items()):
        parents = graph["parents"].get(key, [])
        out[key] = _card(row, key=key, gateway=str(row.get("gateway") or gateway),
                         linked=row["linked"], parents=parents,
                         parent_rows=[rows.get(p) or {"id": p, "status": "unknown"} for p in parents],
                         episode=graph["episodes"].get(key), resolutions=resolutions, now=now)
    return out


def _section(card: Card) -> Optional[str]:
    if card.red:
        return "needs_you" if card.blocked_on == "human" else "stuck"
    if card.lifecycle == "running":
        return "running"
    if card.lifecycle in _DONE:
        return "done"
    if card.blocked_on == "capacity":
        return "ready"
    return "waiting"


def _header(status_v1: dict, cards: dict[str, Card], asks: list[Ask], muted: bool) -> Header:
    counts = {name: 0 for name in SECTIONS}
    for card in cards.values():
        if card.linked and (section := _section(card)):
            counts[section] += 1
    counts["needs_you"] += sum(1 for a in asks if a.card is None)
    kind = status_v1.get("kind")
    presentation = status_v1.get("presentation") or {}
    reasons = list(presentation.get("reason_codes") or ())
    if muted or kind == "muted":
        status = "muted"
    elif counts["needs_you"]:
        status = "needs_you"
    elif counts["stuck"]:
        status = "stuck"
    elif kind == "running":
        status = "running"
    elif kind in ("waiting", "decision"):  # a decision every resolution answered is a wait (design item 7)
        status = "waiting"
    elif kind == "done":
        status = "done"
    elif kind == "unresolved":
        status = "unknown" if status_v1.get("reason") == "source_unknown" else (
            "idle" if status_v1.get("reason") == "empty" else "attention")
    else:
        status = "unknown"
    colour = _STATUS_COLOUR[status]
    if status == "waiting":
        kinds = {c.blocked_on for c in cards.values() if c.linked and _section(c) == "waiting"}
        if len(kinds) == 1 and (only := next(iter(kinds))) in _WAIT_COLOUR:
            colour = _WAIT_COLOUR[only]
    pips = [{"section": name, "count": counts[name]} for name in SECTIONS if counts[name]]
    return Header(status=status, colour=colour, stripe=str(presentation.get("stripe") or "none"),
                  reason_codes=reasons, counts=counts, pips=pips)


def _next_change(cards: dict[str, Card], rows: list[dict], now: float) -> Optional[float]:
    from hermes_cli.kanban_triage_attention import next_transition

    due = [t for row in rows if (t := next_transition(row, now=now)) is not None]
    due += [t for row in rows if (t := _num(row.get("source_expires_at"))) is not None and t > now]
    due += [c.eta for c in cards.values() if c.eta is not None and c.eta > now and c.blocked_on == "clock"]
    return min(due) if due else None


def _session(status_v1: dict, parts: dict) -> Session:
    turn = dict(parts.get("turn") or {})
    processes = [{"id": p["id"], "state": p["state"]} for p in parts.get("processes") or ()]
    subagents = [{"id": s["id"], "state": s["state"]} for s in parts.get("subagents") or ()]
    if turn.get("state") == "running" or any(i["state"] == "running" for i in processes + subagents):
        mode = "running"
    elif turn.get("state") == "failed" or status_v1.get("reason") == "turn_failed":
        mode = "failed"
    else:
        mode = "idle"
    return Session(mode=mode, turn=turn, processes=processes, subagents=subagents)


def legacy_adapter(inputs: dict[str, Any], *, now: float, resolutions: list[dict]) -> SessionState:
    """P1 reducer over today's ``reduce_status`` (see module doc)."""
    from hermes_state_session_status import reduce_status
    from hermes_state_session_status_snapshot import assemble

    parts = inputs["parts"]
    snapshot, base = assemble(parts, now=now)
    status_v1 = reduce_status(snapshot, previous=base)
    ident = parts["identity"]
    identity = Identity(gateway=str(ident.get("gateway") or "local"), profile=str(ident.get("profile") or "default"),
                        lineage=str(inputs["lineage"]), tip=str(inputs["tip"]))
    cards = _cards(inputs, resolutions, now)
    asks = [Ask(id=str(a.get("id") or ""), question=_str(a.get("question")),
                card=(f"{_board_of(cards, a['card_id'])}/{a['card_id']}" if a.get("card_id") else None),
                choices=list(a.get("choices") or ()), state=str(a.get("state") or "open"))
            for a in snapshot.get("asks") or ()]
    # A card ask a committed resolution answered is history, not a pending ask (design item 7).
    asks = [a for a in asks if a.card is None or (a.card in cards and cards[a.card].resolution is None)]
    closure_rows = list(inputs["dependency_graph"]["rows"].values())
    sources = {name: {"coverage": s.get("coverage", "unknown"), "reason": s.get("reason")}
               for name, s in sorted((snapshot.get("sources") or {}).items())}
    for name, reason in sorted((inputs.get("errors") or {}).items()):
        sources[name] = {"coverage": "unknown", "reason": reason}
    linked_rows = [r for r in parts.get("card_rows") or () if r.get("status") == "triage"]
    return SessionState(
        identity=identity, session=_session(status_v1, parts),
        header=_header(status_v1, cards, asks, bool(parts.get("muted"))), cards=cards, asks=asks,
        resolutions=[dict(r) for r in resolutions][-50:], sources=sources,
        next_change_at=_next_change(cards, linked_rows + closure_rows, now),
        legacy={"kind": status_v1.get("kind"), "reason": status_v1.get("reason"),
                "stripe": (status_v1.get("presentation") or {}).get("stripe")})


def _board_of(cards: dict[str, Card], card_id: str) -> str:
    for card in cards.values():
        if card.id == card_id:
            return card.key.rsplit("/", 1)[0]
    return "default"
