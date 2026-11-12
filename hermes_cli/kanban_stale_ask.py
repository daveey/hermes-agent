"""Refresh a queued block notice's ask against the card's state at delivery time. [daveey]

A Kanban wake is rendered when the card blocks, then waits in the chat's FIFO until the running
turn ends, which can be hours. By then the card may be unblocked or done, yet the frozen text still
said "Next action for the user: ...". ``refresh_stale_ask`` re-reads the card (read-only) when the
text is delivered: a card that moved past the notice's block keeps the notice, with the
"Next action for the user" / "Why only the user" lines (and the user-terminal hint) replaced by one
``Since then: ...`` line. A card still blocked on the same block leaves the text byte-identical, and
an unreadable board or a remote card (``@task:<board>@<gateway>/...``) leaves it unchanged too.
"""
from __future__ import annotations

import contextlib
import itertools
import json
import logging
import re
import sqlite3
from datetime import datetime
from typing import Optional

ASK_MARK = "Next action for the user: "
TERMINAL_HINT = ("\nUse the owner Desktop session's user-terminal flow; opening this task or unblocking it does not "
                 "execute a command or grant terminal authority.")
HEAD_MARK = "blocked — actor: "
LINK_MARK = "\nOpen task: @task:"
# A notice's fields are free text (a scope may quote another notice, blank lines, actor lines, ask
# marks), so no text stop can delimit them (review F1-F5). Instead the notice is matched to its card:
# a card owns the ask only if one of its events' human_request renders, in block_notice()'s exact
# layout, to the text from the ask mark to that card's "Open task:" link. Everything is str.find and
# startswith, so the cost is linear in the text (review N2/F4/F6).
_LINK = re.compile(r"(?P<board>[^/@\s]+)(?:@(?P<gw>[^/\s]+))?/(?P<tid>[A-Za-z0-9_-]+)")
_TID_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
_MAX_LINKS = 32  # more "Open task:" links after one ask than this: leave the text unchanged
logger = logging.getLogger(__name__)
_PT = "America/Los_Angeles"
# Transitions worth naming; claims, spawns, heartbeats and comments are noise here.
_LABELS = {"unblocked": "unblocked", "blocked": "blocked again", "block_loop_detected": "blocked again",
           "scheduled": "scheduled",
           "review_requested": "sent to review", "completed": "completed", "archived": "archived",
           "request_withdrawn": "ask withdrawn", "gave_up": "gave up"}
_LAST_STATUS = {"completed": "done", "archived": "archived", "request_withdrawn": "archived",
                "blocked": "blocked", "block_loop_detected": "triage", "scheduled": "scheduled",
                "review_requested": "review"}
# A re-block at the loop limit records ``block_loop_detected`` (status triage) instead of ``blocked``;
# both carry the ask, as kanban_block_policy/kanban_block_routing already treat them (review F8).
_BLOCK_KINDS = frozenset({"blocked", "block_loop_detected"})


def _norm(text) -> str:
    return " ".join(str(text or "").split())


def _payload(raw) -> dict:
    with contextlib.suppress(TypeError, ValueError):
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    return {}


def _read_card(board: str, task_id: str):
    """``(status, [(id, kind, created_at, payload)], current human_request)`` read-only, else ``None``."""
    from hermes_cli.kanban_db import kanban_db_path
    path = kanban_db_path(board=board)
    if not path.exists():
        return None
    from hermes_cli import kanban_lockguard
    conn = kanban_lockguard.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    try:
        row = conn.execute("SELECT status, human_request FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return None
        events = conn.execute("SELECT id, kind, created_at, payload FROM task_events WHERE task_id=? ORDER BY id",
                              (task_id,)).fetchall()
        return row[0], [(i, k, c, _payload(p)) for i, k, c, p in events], _payload(row[1])
    finally:
        conn.close()


def _render(request, board: str, tid: str):
    """The ask block_notice() renders for ``request``, from the ask mark to the end of the link."""
    try:
        return (f"{ASK_MARK}{request['action']}\nWhy only the user: {request['why_human']}\n"
                f"Scope: {request['scope']}\nOpen task: @task:{board}/{tid}")
    except (KeyError, TypeError):
        return None


def _owner_event(events, text: str, pos: int, end: int, board: str, tid: str) -> Optional[int]:
    """Index of the newest event whose human_request renders exactly to ``text[pos:end]``, else ``None``."""
    for i in range(len(events) - 1, -1, -1):
        request = events[i][3].get("human_request")
        if isinstance(request, dict):
            rendered = _render(request, board, tid)
            if rendered is not None and len(rendered) == end - pos and text.startswith(rendered, pos):
                return i
    return None


def _anchor(events, owner: int) -> Optional[int]:
    """The ``blocked``/``block_loop_detected`` event at or before the owner event (a metadata-only
    amendment is itself a ``blocked`` event, so an amended ask anchors on the amendment)."""
    return next((j for j in range(owner, -1, -1) if events[j][1] in _BLOCK_KINDS), None)


def _clock(ts: int, base_day) -> tuple[str, str]:
    from zoneinfo import ZoneInfo
    when = datetime.fromtimestamp(int(ts), ZoneInfo(_PT))
    day = "" if when.date() == base_day else when.strftime("%b %-d ")
    return f"{day}{when:%H:%M}", when.strftime("%Z")


def since_line(status: str, events, anchor: int, current=None) -> Optional[str]:
    """The replacement line, or ``None`` while the card still waits on the same block.

    ``current`` is the card's live ``tasks.human_request``. Only a live ask counts as "a newer one"
    (review F10): an old blocked event is no proof one exists, e.g. after unblock then gave_up.
    Callers drop the case where ``current`` renders to this very ask before calling."""
    from zoneinfo import ZoneInfo
    later = [ev for ev in events[anchor + 1:] if ev[1] in _LABELS and not ev[3].get("metadata_only")]
    if status in {"blocked", "triage"} and not later:
        return None  # still the same block: the ask stands
    base_day = datetime.fromtimestamp(int(events[anchor][2]), ZoneInfo(_PT)).date()
    stamped = [(_LABELS[kind], *_clock(created_at, base_day)) for _id, kind, created_at, _p in later]
    zones = {zone for _l, _c, zone in stamped}
    if len(zones) > 1:  # the window crossed a DST change: label each time
        text = ", ".join(f"{label} {clock} {zone}" for label, clock, zone in stamped)
    else:
        text = ", ".join(f"{label} {clock}" for label, clock, _z in stamped) + (f" {zones.pop()}" if zones else "")
    if not later or _LAST_STATUS.get(later[-1][1]) != status:
        text = f"{text}; now {status}" if text else f"now {status}"
    if status in {"blocked", "triage"}:
        if isinstance(current, dict) and current:
            return f"Since then: {text}. This ask was replaced by a newer one; answer that, not this."
        return f"Since then: {text}. This ask no longer applies; the card is blocked on something else."
    return f"Since then: {text}. No action needed."


def _links(text: str, pos: int):
    """``(link_end, board, gw, tid)`` for each "Open task:" link after ``pos``, at most ``_MAX_LINKS + 1``."""
    out = []
    at = text.find(LINK_MARK, pos)
    while at != -1 and len(out) <= _MAX_LINKS:
        start = at + len(LINK_MARK)
        m = _LINK.match(text, start)
        if m and (m.end() == len(text) or text[m.end()] not in _TID_CHARS):
            out.append((m.end(), m["board"], m["gw"], m["tid"]))
        at = text.find(LINK_MARK, start)
    return out


def _resolve(text: str, pos: int, read):
    """``(link_end, end, board, tid, card, owner)`` for the ask at ``pos`` if exactly one card owns it, else ``None``.

    ``text[pos:link_end]`` is the owned ask (through its Open task link); ``end`` adds the
    user-terminal hint when one follows."""
    links = _links(text, pos)
    if len(links) > _MAX_LINKS:
        return None
    owners = []
    for end, board, gw, tid in links:
        if gw:
            return None  # another gateway's card may own this ask; this host cannot read it
        card = read(board, tid)
        if card is None:
            return None  # unreadable or unknown card: it could be the owner
        owner = _owner_event(card[1], text, pos, end, board, tid)
        if owner is not None:
            owners.append((end, board, tid, card, owner))
    if len(owners) != 1:
        return None  # no owner, or a field quotes another card's whole ask: ambiguous, leave it
    link_end, board, tid, card, owner = owners[0]
    if text.find(LINK_MARK, pos) != text.rfind(LINK_MARK, pos, link_end):
        return None  # the owner's own fields quote another "Open task:" link: never guess
    end = link_end + len(TERMINAL_HINT) if text.startswith(TERMINAL_HINT, link_end) else link_end
    return link_end, end, board, tid, card, owner


def _stale_line(text: str, pos: int, link_end: int, board: str, tid: str, card, owner: int) -> Optional[str]:
    """The ``Since then:`` line for an owned ask whose card moved on, else ``None``."""
    status, events, current = card[0], card[1], (card[2] if len(card) > 2 else {})
    if status in {"blocked", "triage"} and isinstance(current, dict) and current:
        rendered = _render(current, board, tid)
        if rendered is not None and len(rendered) == link_end - pos and text.startswith(rendered, pos):
            return None  # the card's live ask is this very ask (review F8): it stands
    anchor = _anchor(events, owner)
    return since_line(status, events, anchor, current) if anchor is not None else None


def _ask_marks(text: str):
    """Positions of ask marks at a line start directly under a line holding block_notice()'s actor mark."""
    at = text.find(ASK_MARK)
    while at != -1:
        # The cheap line-start test runs first, so a long line of marks costs one pass (review F9).
        if at > 0 and text[at - 1] == "\n":
            line_start = text.rfind("\n", 0, at - 1) + 1
            if text.find(HEAD_MARK, line_start, at - 1) != -1:
                yield at
        at = text.find(ASK_MARK, at + 1)


def refresh_stale_ask(text):
    """``text`` with each stale ask swapped for a ``Since then:`` line; unchanged on any doubt.

    Asks are resolved in text order. An ask inside an earlier owned ask's span is a quotation in that
    ask's fields and is never touched (review F7). The first ask no card verifiably owns ends the scan:
    its fields could run anywhere, so nothing after it is trusted.

    Limits, by intent: only quotes inside an owned ask's span are protected; a stale ask quoted
    elsewhere (e.g. in another notice's Reason) is refreshed, which stays truthful (review O1). A
    remote or unreadable link anywhere in the scanned links leaves the whole text unchanged, even an
    earlier stale ask: fail closed (review O2)."""
    if not isinstance(text, str) or ASK_MARK not in text:
        return text
    cache = {}

    def read(board, tid):
        if (board, tid) not in cache:
            try:
                cache[board, tid] = _read_card(board, tid)
            except (sqlite3.Error, OSError, ValueError):
                cache[board, tid] = None
        return cache[board, tid]

    swaps, asks, covered = [], 0, -1
    for at in _ask_marks(text):
        if at < covered:
            continue  # quoted inside the previous owned ask
        asks += 1
        if asks > _MAX_LINKS:
            return text
        resolved = _resolve(text, at, read)
        if resolved is None:
            break
        link_end, end, board, tid, card, owner = resolved
        covered = end
        line = _stale_line(text, at, link_end, board, tid, card, owner)
        if line is not None:
            request = card[1][owner][3]["human_request"]
            scope_at = at + len(f"{ASK_MARK}{request['action']}\nWhy only the user: {request['why_human']}\n")
            swaps.append((at, scope_at, link_end, end, line))
    if any(later[0] < earlier[3] for earlier, later in itertools.pairwise(swaps)):
        return text  # spans overlap: ambiguous, leave it all
    for pos, scope_at, link_end, end, line in reversed(swaps):
        text = text[:pos] + line + "\n" + text[scope_at:link_end] + text[end:]
    return text


def _is_producer_row(payload) -> bool:
    """A producer wake (Kanban notifier, automation), never text a person typed or pasted."""
    native = payload.get("native_text_v1")
    return bool(payload.get("local_automation_v1")) or (isinstance(native, dict) and bool(native.get("automation")))


def refresh_payload(payload) -> None:
    """Refresh a claimed producer wake's text in place, before it becomes a turn or a note.

    Human-typed rows are never touched. Any failure leaves the text as it was: a refresh must never
    cost the wake itself."""
    try:
        if (isinstance(payload, dict) and isinstance(payload.get("text"), str) and ASK_MARK in payload["text"]
                and _is_producer_row(payload)):
            payload["text"] = refresh_stale_ask(payload["text"])
    except Exception:  # unchanged on any doubt
        logger.warning("stale block-ask refresh failed; delivering the wake unchanged", exc_info=True)
