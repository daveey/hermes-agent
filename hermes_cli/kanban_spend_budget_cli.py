"""``hermes kanban spend-budget <id>``: show or set one card's own spend budget (kanban_spend_budget). [daveey]

Without a change flag it prints the card's effective limits, its override (who set it, why) and its current
spend. ``--runs N`` / ``--cache-read-tokens N`` (0 = no limit), ``--exempt`` (both 0) or ``--clear`` (back to
the board's limits) record a ``spend_budget_set`` event and need ``--reason``. Worker and delegate_task shells
are fenced from this verb like every other board write, so a looping card cannot exempt itself.
For a spend-stop episode, an existing budget decision must be read before replacement (``--seen COMMENT_ID``).
This acknowledgment is checked under the writer lock, survives promotion, and is not a retry instruction:
keep the first decision unless an explicit reconciliation is warranted. Historical spend values are retained.
An ordinary pending spend stop uses a fresh window starting at promote. Until then
the printed spend is historical, not deducted from that fresh allowance. Other
limits include recorded spend in the current window, so one at or below that spend is refused (it would
stop the card at once); restricted history never resets.
"""
from __future__ import annotations

import argparse
import json
import sys

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_budget as ksb


def _err(msg: str, rc: int = 1) -> int:
    print(msg, file=sys.stderr)
    return rc


def _actor() -> str:
    from hermes_cli.profiles import current_profile_name
    return current_profile_name("user") or "user"


def _spent_already(conn, tid: str, runs, cache) -> str:
    """Why the limits this change leaves would stop the card at once, or ''.

    In the current window a limit counts the spend already recorded, so ``--runs 3`` on a card with 7 runs
    trips the budget at once instead of granting 3 more (t_01a205c88aff2581, 2026-11-24). A field not given
    falls back to the board's limit (the override is replaced whole), so it is checked too. A pending
    ordinary stop opens a fresh window at promote, so nothing is counted there yet; a restricted one keeps
    its history, so it is checked like the current window."""
    if ksb.limit_window(conn, tid) != 'current':
        return ''
    spent = ksb.spend(conn, tid)
    board = ksb.limits()
    rows = (('runs', runs, board.runs, spent.runs),
            ('cache-read-tokens', cache, board.cache_read_tokens, spent.tokens['cache_read_tokens']))
    tripped = []
    for flag, given, board_limit, used in rows:
        limit = board_limit if given is None else given
        if limit and used >= limit:
            source = f'--{flag} {limit}' if given is not None else f"the board's {flag} limit {limit}"
            tripped.append(f"{used} {flag} already spent in this window reach {source}; to allow {limit} more, "
                           f"pass --{flag} {used + limit}")
    if not tripped:
        return ''
    return (f"kanban spend-budget: {tid}: " + '; '.join(tripped) + ". A limit counts spend already recorded, "
            "so this would trip the budget at once. Use --exempt for no limit; to stop the card, block or archive it.")


_PERSON_NOTE = "Spend budget for this card:"  # the start of the comment set_override writes with each override


def _person_note(conn, tid: str, event: dict):
    """The ``{id, who, time, body}`` of the comment ``set_override`` wrote with override ``event``, else None.

    A note is accepted only when its text is this event's own: ``set_override`` writes the event's values and
    ``set by {by}: {reason}.`` into it, so another override's note (an earlier one in the same minute, or a
    later agent's) never stands for this event. Candidates are the decision comment the event names and the
    budget writer's notes of the event's own transaction window; with none matching, the caller prints the event
    itself (:func:`_event_item`)."""
    from datetime import UTC, datetime
    payload = json.loads(event.get("payload") or "{}")
    if not isinstance(payload, dict):
        return None
    expected = (f"{_PERSON_NOTE} {ksb._override_text(payload)}, set by {str(payload.get('by') or '').strip()}: "
                f"{str(payload.get('reason') or '').strip()}. ")
    named = (payload.get("decision_comment") or {}).get("comment_id")
    at = int(event.get("created_at") or 0)
    rows = []
    if isinstance(named, int):
        rows += conn.execute("SELECT id, author, created_at, body FROM task_comments WHERE task_id=? AND id=?",
                             (tid, named)).fetchall()
    rows += conn.execute("SELECT id, author, created_at, body FROM task_comments WHERE task_id=? AND author=? "
                         "AND body LIKE ? AND created_at BETWEEN ? AND ? ORDER BY id DESC",
                         (tid, ksb.AUTHOR, _PERSON_NOTE + "%", at - 60, at + 1)).fetchall()
    row = next((r for r in rows if str(r[3] or "").startswith(expected)), None)
    if row is None:
        return None
    return {"id": int(row[0]), "who": f"{row[1]} (the limit being changed)", "body": str(row[3] or "")[:600],
            "time": datetime.fromtimestamp(int(row[2] or 0), UTC).isoformat()}


def _event_item(event: dict) -> dict:
    """The person's override event itself as a read item, when its note comment is not readable here (written
    before notes, or not synced yet): who set it, why and the values, required in ``--seen`` like a comment and
    recorded as shown by its event id and uid (``kanban_release.read_first``)."""
    from datetime import UTC, datetime
    payload = json.loads(event.get("payload") or "{}")
    payload = payload if isinstance(payload, dict) else {}
    return {"id": int(event["id"]), "kind": "event", "uid": event.get("uid"),
            "who": f"{payload.get('by') or 'unknown'} (the limit being changed; its note is not readable here)",
            "time": datetime.fromtimestamp(int(event.get("created_at") or 0), UTC).isoformat(),
            "body": f"Spend budget: {ksb._override_text(payload)}, set by {payload.get('by') or 'unknown'}: "
                    f"{payload.get('reason') or '(no reason recorded)'}"}


def _agent_gate(conn, tid: str, writer: tuple, runs, cache, seen, board, args):
    """For a non-human caller: ``(seen for the budget fence, carried attribution fields)``, or a refusal text.

    Restating or tightening a limit a person may have set keeps that limit's attribution (``agent_change``), so
    the restatement cannot make it look agent-set for a later release. Raising or removing one (a larger value,
    0, or ``--clear`` back to a larger board limit) follows ``kanban_release.read_first``: the refusal prints the
    override's own comment, human comments since it and any budget decision, and records them as shown to this
    session; ``--seen`` is then the newest id it printed, on a rerun from the same session."""
    from hermes_cli import kanban_release as kr
    loosened, carried = ksb.agent_change(ksb.card_override(conn, tid), runs, cache, ksb.limits())
    if not loosened:
        return seen, carried
    row = conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                       (tid, ksb.OVERRIDE_EVENT)).fetchone()
    anchor = dict(row)
    note = _person_note(conn, tid, anchor) or _event_item(anchor)  # no readable note: the budget event itself
    flags = ("--exempt" if args.exempt else "--clear" if args.clear else
             " ".join(f"--{name} {value}" for name, value in (("runs", runs), ("cache-read-tokens", cache))
                      if value is not None))
    rerun = f"hermes kanban {'--board ' + board + ' ' if board else ''}spend-budget {tid} {flags} --reason ..."
    try:
        seen = kr.read_first(conn, tid, anchor, writer, seen, board, verb="spend-budget",
                            extra=[note], rerun=rerun)
    except kr.Refusal as exc:
        return (f"kanban spend-budget: {tid}: refused: {exc.code}: raising or removing a {' and '.join(loosened)} "
                f"limit a person may have set needs a read first. {exc}")
    return seen, carried  # the limits it keeps or tightens keep their attribution; a lifted one is this writer's


def command(args: argparse.Namespace) -> int:
    tid = args.task_id
    runs, cache = args.runs, args.cache_read_tokens
    exempt, clear = bool(args.exempt), bool(args.clear)
    if ((exempt or clear) and (runs is not None or cache is not None)) or (exempt and clear):
        return _err("kanban spend-budget: --exempt and --clear stand alone (no --runs / --cache-read-tokens)", 2)
    changing = exempt or clear or runs is not None or cache is not None
    if changing and not (args.reason or "").strip():
        return _err("kanban spend-budget: a change needs --reason (it is recorded on the card)", 2)
    if exempt:
        runs = cache = 0
    with kbc.connect_closing() as conn:
        if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (tid,)).fetchone() is None:
            return _err(f"kanban spend-budget: unknown task {tid}")
        if changing and (refusal := _spent_already(conn, tid, runs, cache)):
            return _err(refusal, 2)
        if changing:
            from hermes_cli.kanban_comment_writer import cli_comment_writer
            from hermes_cli.kanban_release_comments import UNREAD_EXIT, UnreadComments
            writer = cli_comment_writer()
            seen, board = getattr(args, 'seen_comment_id', None), getattr(args, 'board', None)
            carried: dict = {}
            if writer[0] != "human":
                gate = _agent_gate(conn, tid, writer, runs, cache, seen, board, args)
                if isinstance(gate, str):
                    return _err(gate, UNREAD_EXIT)
                seen, carried = gate
            try:
                ksb.set_override(conn, tid, runs=runs, cache_read_tokens=cache, by=_actor(), reason=args.reason,
                                 seen=seen, board=board, writer_kind=writer[0], **carried)
            except UnreadComments as exc:
                return _err(str(exc), UNREAD_EXIT)
        lim = ksb.card_limits(conn, tid, ksb.limits())
        override = ksb.card_override(conn, tid)
        spent = ksb.spend(conn, tid)
        window = ksb.limit_window(conn, tid)
    out = {"task_id": tid, "limits": lim._asdict(), "override": override, "window": window,
           "spent": {"runs": spent.runs, "since": spent.since, "tokens": spent.tokens, "excluded": spent.excluded}}
    if getattr(args, "json", False):
        print(json.dumps(out, sort_keys=True))
        return 0
    if not lim.runs and not lim.cache_read_tokens:
        print(f"{tid}: no spend budget (exempt)")
    else:
        shown = ", ".join(f"{k} {'no limit' if v == 0 else v}" for k, v in lim._asdict().items())
        print(f"{tid}: spend budget {shown}")
    if override is not None and ksb.override_set(override):
        print(f"  override set by {override.get('by')}: {override.get('reason')}")
    print(f"  {ksb.window_text(window)}")
    skipped = f" ({spent.excluded} owner-refusal runs excluded)" if spent.excluded else ""
    label = 'historical spend' if window == 'next_promote' else 'spent'
    print(f"  {label}: {spent.runs} runs{skipped}, {spent.tokens['cache_read_tokens']} cache-read tokens in this window")
    return 0
