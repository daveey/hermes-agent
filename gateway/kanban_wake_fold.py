"""Kanban wakes that pile up behind a busy turn run as ONE turn, built from live card state. [daveey]

Each Kanban wake (producer ``kanban-wake``) used to commit its own FIFO row, so a chat whose turns
take 30-80 min fell further behind with every card event: on 2026-11-11 chat
20261026_170127_f07c32 was 19 wakes (3.5 h) behind, and each wake that finally ran told it about a
card "ready with no worker" that had been done for hours (t_01a1cec8510df47f).

A wake admitted while another Kanban wake for the same route is still the session's LAST queued
input joins that row instead, like process completions (``session_automation._admit_folded_completion``):
the waiting row is cancelled in the new admission's own transaction (``_fold_queued``), so a prompt
admitted after it, or a claim that started it first, keeps the two apart. The batch is rebuilt from
the cards each row carries (``kanban_cards``): several events for one card collapse to the latest,
and a card whose premise no longer holds (an unclaimed wait now claimed or done, a block now
resolved) is relabelled with its live status. Every card keeps its ``[kanban] Task <id> <kind>.``
line, which the follow-through and ask-gate plugins match.
"""
from __future__ import annotations

import contextlib
import json
import logging
import re

from hermes_state_runtime import RuntimeStoreError, list_session_admissions

logger = logging.getLogger(__name__)

PRODUCER = 'kanban-wake'
_CARD_KEYS = frozenset({'task_id', 'board', 'kinds', 'text', 'notices'})
_MAX_CARDS = 200
_MAX_CARD_TEXT = 32000

_FOLD_HEADER = ('[IMPORTANT: {count} Kanban task updates arrived while this session was busy. Each card '
                'below shows its state when this batch was built; an earlier premise that no longer holds '
                'is relabelled. Treat them as one batch and give one consolidated response.]')

# The live statuses under which a wake's premise still holds. A kind absent here reports an outcome
# (completed, ...) and is never relabelled. Review handoffs remain pending while a reviewer runs.
_PREMISE_HOLDS = {
    'park_progress_notice': frozenset({'scheduled'}),
    'review_requested': frozenset({'review'}),
    'spend_budget_decision_ready': frozenset({'triage'}),
    'chat_wait_unclaimed': frozenset({'triage', 'todo', 'ready'}),
    'blocked': frozenset({'blocked', 'triage'}),
    'block_loop_detected': frozenset({'blocked', 'triage'}),
    'crashed': frozenset({'triage', 'todo', 'ready', 'running', 'blocked', 'review', 'scheduled'}),
    'timed_out': frozenset({'triage', 'todo', 'ready', 'running', 'blocked', 'review', 'scheduled'}),
}
_STATUS_LABELS = {
    'done': 'completed', 'archived': 'archived', 'running': 'claimed by a worker (running)',
    'ready': 'ready', 'todo': 'todo', 'triage': 'in triage', 'scheduled': 'scheduled',
    'blocked': 'blocked; needs attention', 'review': 'in review',
}
_LEGACY_HEAD = re.compile(r'^\[kanban\] Task (\S+) ')
_LEGACY_BOARD = re.compile(r'^Board: (\S+)$', re.MULTILINE)


def card_metadata(metadata):
    """Validate a wake's ``kanban_cards`` (what a later wake folds from) for the stored descriptor,
    plus a remote notice's ``kanban_event_keys`` (:func:`event_keys_metadata`)."""
    keys = event_keys_metadata(metadata)
    cards = metadata.get('kanban_cards')
    if cards is None:
        return keys
    if not isinstance(cards, list) or not cards or len(cards) > _MAX_CARDS:
        raise RuntimeStoreError('invalid_params')
    for card in cards:
        if (not isinstance(card, dict) or set(card) - _CARD_KEYS or not isinstance(card.get('text'), str)
                or len(card['text']) > _MAX_CARD_TEXT
                or not all(isinstance(card.get(key, ''), str) for key in ('task_id', 'board'))
                or not isinstance(card.get('kinds', []), list)
                or not all(isinstance(kind, str) for kind in card.get('kinds', []))
                or not isinstance(card.get('notices', []), list)
                or not all(isinstance(line, str) for line in card.get('notices', []))):
            raise RuntimeStoreError('invalid_params')
    return {'kanban_cards': [dict(card) for card in cards], **keys}


def event_keys_metadata(metadata):
    """A remote Kanban notice's ``kanban_event_keys`` (``gateway.kanban_remote_owner``): its ledger claim
    is acked only once a transcript row carries them, so the turn's user row must (``kanban_notice``
    rows are what ``kanban_notice_keys_delivered`` reads). Only allowed on a ``kanban_notice`` card."""
    keys = metadata.get('kanban_event_keys')
    if keys is None:
        return {}
    if (metadata.get('display_kind') != 'kanban_notice' or not isinstance(keys, list) or not keys
            or len(keys) > _MAX_CARDS or not all(isinstance(key, str) and key for key in keys)):
        raise RuntimeStoreError('invalid_params')
    return {'kanban_event_keys': list(keys)}


def wake_lock(runner):
    """Serialises the read-tail-then-fold of Kanban wakes in this process: two wakes folding into the
    same tail would otherwise both read it, and the second (refused by the write guard) queue alone."""
    import asyncio
    lock = runner.__dict__.get('_kanban_wake_lock')
    if lock is None:
        lock = runner.__dict__['_kanban_wake_lock'] = asyncio.Lock()
    return lock


def _descriptor(payload):
    return payload.get('local_automation_v1') or (payload.get('native_text_v1') or {}).get('automation') or {}


def _producer(request_id):
    try:
        identity = json.loads(request_id)
    except (TypeError, ValueError):
        return None
    return identity[0] if isinstance(identity, list) and identity else None


def waiting_wake(authority, owner, route):
    """The route's own Kanban wake row that is the LAST queued input of session ``owner``, or None.
    Only the tail folds (the rule of ``_waiting_completion``): a wake never jumps ahead of, or
    folds across, a prompt or any other input queued after it."""
    queued = [row for row in list_session_admissions(authority.db, session_id=owner) if row['status'] == 'queued']
    tail = queued[-1] if queued else None
    if tail is None or tail['principal_id'] != 'automation:' + route or _producer(tail['request_id']) != PRODUCER:
        return None
    descriptor = _descriptor(tail['payload'])
    if descriptor.get('display_kind') and not _remote_batch(descriptor):
        return None
    return tail


def _remote_batch(descriptor):
    """A wake carrying remote cards' notices (``gateway.kanban_remote_owner``): a ``kanban_notice`` card
    with its custody keys and its cards, which folds like any Kanban wake (review t_01a1d013e0411032)."""
    return (descriptor.get('display_kind') == 'kanban_notice' and bool(descriptor.get('kanban_event_keys'))
            and bool(descriptor.get('kanban_cards')))


def _legacy_cards(text):
    """A row queued before cards were stored: one card parsed from its text (opaque when unparsable)."""
    head = _LEGACY_HEAD.match(text)
    board = _LEGACY_BOARD.search(text)
    return [{'task_id': head.group(1) if head else '', 'board': board.group(1) if board else '',
             'kinds': [], 'text': text.rstrip()}]


def _merge(old_cards, new_cards):
    """Cards in first-seen order; a later event for the same card replaces the earlier one, keeping the
    earlier card's notice lines (:func:`_keep_notices`)."""
    merged, order = {}, []
    for index, card in enumerate([*old_cards, *new_cards]):
        key = card.get('task_id') or f'#opaque-{index}'
        if key not in merged:
            order.append(key)
            merged[key] = dict(card)
        else:
            merged[key] = _keep_notices(merged[key], card)
    return [merged[key] for key in order]


def _keep_notices(old, new):
    """``new`` (a later wake for the same card) with ``old``'s ``notices`` it does not show yet, ahead of its own.

    A card's ``notices`` are Kanban notice lines its wake turn carries for the chat's ``tui`` row, which was settled
    past them when the wake was admitted (``kanban_wake_sibling.settle_tui_sibling``). Replacing the card dropped
    them for good: a ``status → ready`` line carried by a queued wake vanished when the completion's wake folded
    into it (review of PR #1465 at b0fdf842f2). The earlier premise text itself still collapses to the latest."""
    card = dict(new)
    have = [str(line) for line in card.get('notices') or []]
    text = str(card.get('text') or '')
    missing = [line for line in old.get('notices') or [] if line not in have and line not in text]
    while missing and len(text) + sum(len(line) + 1 for line in missing) > _MAX_CARD_TEXT:
        missing.pop(0)  # card_metadata refuses an oversized card; the oldest notice goes first
    if not missing:
        return card
    block = '\n'.join(missing)
    at = text.find(have[0]) if have else -1
    if at > 0 and text[at - 1] == '\n':
        text = text[:at] + block + '\n' + text[at:]
    else:
        text = text.rstrip() + '\n' + block
    card['text'], card['notices'] = text, [*missing, *have]
    return card


def _live_task(card):
    """``(status, latest handoff summary)`` of the card on its board, or ``(None, '')``. The board slug
    came from the notifier's board enumeration, so it resolves like the notifier read it
    (``pin_first_board_resolution``): a ``HERMES_KANBAN_DB`` pin outranks it. When the board's pulled
    replica holds a newer Board revision of the card than the live row (a live copy lagging after a sync
    stall), the replica's status wins (``kanban_replica_freshness``, t_01a1d390d19239dd)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_replica_freshness as fresh
    with kb.pin_first_board_resolution(), contextlib.closing(kbc.connect(board=card.get('board') or None)) as conn:
        task = kb.get_task(conn, card['task_id'])
        newer = fresh.newer_rows(conn, [card['task_id']]).get(card['task_id'])
        if newer is not None and newer['status']:
            if newer['status'] == 'running' and 'review_requested' in card.get('kinds', []):
                return None, ''  # no replica run lane: a running reviewer may still be deciding
            summary = (newer.get('summary') or newer.get('result') or '') if newer['status'] == 'done' else ''
            return newer['status'], summary.strip()
        if task is None:
            return None, ''
        if (task.status == 'running' and 'review_requested' in card.get('kinds', [])
                and kb._retry_status_for_run(conn, card['task_id'], task.current_run_id) == 'review'):
            return 'review', ''  # the handoff is still pending, not returned to implementation
        summary = (kb.latest_summary(conn, card['task_id']) or task.result or '') if task.status == 'done' else ''
        return task.status, summary.strip()


def _without_superseded_ask(lines, task_id=''):
    """Remove the formatter's whole ask block, including multiline field values."""
    start = next((i for i, line in enumerate(lines) if line.startswith('Next action for the user:')), None)
    # Prefer this card's closing link over embedded links and unrelated trailing context.
    ends = [i for i, line in enumerate(lines) if line.startswith('Open task:')
            and start is not None and i > start]
    matching = [i for i in ends if task_id and lines[i].rstrip().endswith('/' + task_id)]
    end = (matching or ends)[-1] if ends else None
    if start is not None and end is not None:
        lines = lines[:start] + lines[end + 1:]
    return [line for line in lines if not line.startswith((
        'Next action for the user:', 'Why only the user:', 'Scope:',
        'Automatic detection stopped', 'Automatic detection had stopped for an earlier version of this ask;',
        'Your step is still needed. Filed ask, historical wording',
        "Use the owner Desktop session's user-terminal flow;"))]


def refresh_card(card, *, strict=False):
    """``card`` with its live status applied: a wake whose premise no longer holds (an unclaimed wait
    since claimed or done, a block since resolved) is relabelled with the card's current status. An
    unreadable board leaves the card as delivered, or raises when ``strict``."""
    kinds = card.get('kinds') or []
    premises = [kind for kind in kinds if kind in _PREMISE_HOLDS]
    if not card.get('task_id') or not premises:
        return card
    try:
        status, result = _live_task(card)
    except Exception:  # health: allow BLE001 -- a live read is best effort: the wake still goes in as delivered
        logger.debug('kanban wake fold: live state of %s unreadable', card['task_id'], exc_info=True)
        if strict:
            raise
        return card
    if status is None:
        return card
    if all(status in _PREMISE_HOLDS[kind] for kind in premises):
        return card
    if status == 'done' and 'completed' in kinds:
        return card
    lines = card['text'].split('\n')
    head = lines[0]
    earlier = head.removeprefix(f"[kanban] Task {card['task_id']} ").rstrip('.') or ', '.join(premises)
    label = _STATUS_LABELS.get(status, status)
    out = [f"[kanban] Task {card['task_id']} {label}.",
           f'Note: an earlier wake for this card said "{earlier}"; that no longer holds. Live status: {status}.']
    rest = lines[1:] if _LEGACY_HEAD.match(head) else lines
    rest = _without_superseded_ask(rest, card['task_id'])
    out.extend(rest)
    if status == 'done' and result and not any(line.startswith('Result:') for line in rest):
        out.insert(2, 'Result: ' + result.splitlines()[0][:400])
    return {**card, 'kinds': [{'done': 'completed'}.get(status, status)], 'text': '\n'.join(out).rstrip()}


def _compose(cards, guidance, *, strict=False):
    """The turn text of a batch of ``cards`` (each live-refreshed), with the trailing ``guidance``."""
    parts = [refresh_card(card, strict=strict)['text'] for card in cards]
    if len(cards) > 1:
        parts.insert(0, _FOLD_HEADER.format(count=len(cards)))
    if guidance:
        parts.append(guidance)
    return '\n\n'.join(parts)


def _head_at(text, task_id, pos):
    """Offset of card ``task_id``'s ``[kanban] Task <id> `` line at or after ``pos``, or -1."""
    head = f'[kanban] Task {task_id} '
    at = text.find(head, pos)
    while at > 0 and text[at - 1] != '\n':
        at = text.find(head, at + 1)
    return at


def _guidance(text, cards):
    """What follows the last card in ``text`` (a row built by ``_compose`` from ``cards``, each card
    rendered as delivered or relabelled), or ``None`` when ``text`` does not have that shape."""
    if len(cards) > 1 and not text.startswith(_FOLD_HEADER.format(count=len(cards))):
        return None
    pos = 0
    for card in cards:  # every card's head line, in order: the batch these cards built
        if card.get('task_id') and _LEGACY_HEAD.match(card['text']):
            pos = _head_at(text, card['task_id'], pos)
            if pos < 0:
                return None
    last = cards[-1]
    lines = last['text'].rstrip().split('\n')
    rest = lines[1:] if _LEGACY_HEAD.match(lines[0]) else lines
    tail = '\n'.join(rest).rstrip()
    if tail and text.find(tail, pos) < 0 and f"[kanban] Task {last.get('task_id')} " in text:
        # A relabel removes the obsolete ask; an ask-only tail becomes a one-line card.
        tail = '\n'.join(_without_superseded_ask(rest, last['task_id'])).rstrip()
    if tail:
        end = text.find(tail, pos)
        if end < 0:
            return None
        end += len(tail)
    else:  # a one-line card: its head, then a relabel's Note and Result lines when present
        end = text.find('\n', pos)
        end = len(text) if end < 0 else end
        for prefix in ('Note: an earlier wake for this card said', 'Result: '):
            if text.startswith(prefix, end + 1):
                nxt = text.find('\n', end + 1)
                end = len(text) if nxt < 0 else nxt
    if text[end:end + 1] not in ('', '\n'):
        return None
    return text[end:].strip()


def refresh_claimed(payload):
    """Rebuild a claimed Kanban wake's text in place from its cards' live state, before it becomes a
    turn (``session_ingress.execute_admission``). A lone wake that no later wake folded into was
    otherwise drained with the premise it was built with, however long it waited (row 3605 on
    2026-11-12 told its chat a card claimed 15 min earlier had no worker). Only the in-memory claim
    payload changes, never the stored row. Any other row, a row whose text no longer has the shape
    its cards built, and an unreadable board leave the text as it was."""
    try:
        if not isinstance(payload, dict) or not isinstance(payload.get('text'), str):
            return
        descriptor = _descriptor(payload)
        if not descriptor.get('kanban_cards') or _producer(descriptor.get('identity')) != PRODUCER:
            return
        cards = card_metadata(descriptor)['kanban_cards']
        if not any(kind in _PREMISE_HOLDS for card in cards for kind in card.get('kinds') or []):
            return  # outcome-only batch: nothing can have gone stale, so no board read
        guidance = _guidance(payload['text'], cards)
        if guidance is not None:
            # Strict: an unreadable card would fall back to its as-delivered text and undo the
            # relabel the fold already made, so any unreadable card leaves the whole text as it was.
            payload['text'] = _compose(cards, guidance, strict=True)
            if descriptor.get('display_kind'):
                from gateway.session_automation import _DISPLAY_TEXT_LIMIT
                text = payload['text']
                descriptor['display_text'] = text if len(text) <= _DISPLAY_TEXT_LIMIT else text[:_DISPLAY_TEXT_LIMIT - 1] + '…'
    except Exception:  # health: allow BLE001 -- a refresh must never cost the wake itself
        logger.warning('kanban wake: claim-time relabel skipped; delivering the wake unchanged', exc_info=True)


def fold_into(event, waiting, identity):
    """Rewrite ``event`` (the new wake) as the batch of ``waiting``'s cards and its own, live-refreshed.
    Blocking: it reads the Kanban board, so callers run it off the event loop."""
    new_cards = event.metadata.get('kanban_cards') or []
    new_body = '\n\n'.join(card['text'] for card in new_cards)
    guidance = event.text[len(new_body):].strip() if new_body and event.text.startswith(new_body) else ''
    descriptor = _descriptor(waiting['payload'])
    old_cards = descriptor.get('kanban_cards') or _legacy_cards(str(waiting['payload'].get('text') or ''))
    if not descriptor.get('kanban_cards') and guidance and old_cards[0]['text'].endswith(guidance):
        old_cards[0]['text'] = old_cards[0]['text'][:-len(guidance)].rstrip()
    if not guidance and descriptor.get('kanban_cards'):
        # [daveey] A remote notice carries no guidance: keep the waiting batch's, or a local wake folded under
        # it loses its "inspect the board, don't re-decompose" line (review t_01a1d03acceb95c1).
        guidance = _guidance(str(waiting['payload'].get('text') or ''), old_cards) or ''
    # The stored cards stay as delivered; the live relabel is redone each time the batch is rebuilt.
    cards = _merge(old_cards, new_cards)
    event.text = _compose(cards, guidance)
    event.metadata['kanban_cards'] = cards
    event.metadata['automation_identities'] = sorted({
        *(descriptor.get('identities') or [waiting['request_id']]),
        *(event.metadata.get('automation_identities') or [identity])})
    # [daveey] Remote notices' custody keys ride the batch: their ledger claims are acked when a row carries
    # them, and that row must be a kanban_notice (``kanban_notice_keys_delivered``), whichever side folded.
    keys = list(dict.fromkeys([*(descriptor.get('kanban_event_keys') or []),
                               *(event.metadata.get('kanban_event_keys') or [])]))
    if keys:
        from gateway.session_automation import _DISPLAY_TEXT_LIMIT
        shown = event.text if len(event.text) <= _DISPLAY_TEXT_LIMIT else event.text[:_DISPLAY_TEXT_LIMIT - 1] + '…'
        event.metadata.update({'kanban_event_keys': keys, 'display_kind': 'kanban_notice', 'display_text': shown})
    # The batch mutes only when every wake in it did.
    if descriptor.get('notification_category', 'result') == 'result':
        event.metadata.pop('notification_category', None)
