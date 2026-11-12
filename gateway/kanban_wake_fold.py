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
_CARD_KEYS = frozenset({'task_id', 'board', 'kinds', 'text'})
_MAX_CARDS = 200
_MAX_CARD_TEXT = 32000

_FOLD_HEADER = ('[IMPORTANT: {count} Kanban task updates arrived while this session was busy. Each card '
                'below shows its state when this batch was built; an earlier premise that no longer holds '
                'is relabelled. Treat them as one batch and give one consolidated response.]')

# The live statuses under which a wake's premise still holds. A kind absent here reports an outcome
# (completed, review_requested, ...) and is never relabelled.
_PREMISE_HOLDS = {
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
    """Validate a wake's ``kanban_cards`` (what a later wake folds from) for the stored descriptor."""
    cards = metadata.get('kanban_cards')
    if cards is None:
        return {}
    if not isinstance(cards, list) or not cards or len(cards) > _MAX_CARDS:
        raise RuntimeStoreError('invalid_params')
    for card in cards:
        if (not isinstance(card, dict) or set(card) - _CARD_KEYS or not isinstance(card.get('text'), str)
                or len(card['text']) > _MAX_CARD_TEXT
                or not all(isinstance(card.get(key, ''), str) for key in ('task_id', 'board'))
                or not isinstance(card.get('kinds', []), list)
                or not all(isinstance(kind, str) for kind in card.get('kinds', []))):
            raise RuntimeStoreError('invalid_params')
    return {'kanban_cards': [dict(card) for card in cards]}


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
    if _descriptor(tail['payload']).get('display_kind'):
        return None
    return tail


def _legacy_cards(text):
    """A row queued before cards were stored: one card parsed from its text (opaque when unparsable)."""
    head = _LEGACY_HEAD.match(text)
    board = _LEGACY_BOARD.search(text)
    return [{'task_id': head.group(1) if head else '', 'board': board.group(1) if board else '',
             'kinds': [], 'text': text.rstrip()}]


def _merge(old_cards, new_cards):
    """Cards in first-seen order; a later event for the same card replaces the earlier one."""
    merged, order = {}, []
    for index, card in enumerate([*old_cards, *new_cards]):
        key = card.get('task_id') or f'#opaque-{index}'
        if key not in merged:
            order.append(key)
        merged[key] = dict(card)
    return [merged[key] for key in order]


def _live_task(card):
    """``(status, latest handoff summary)`` of the card on its board, or ``(None, '')``. The board slug
    came from the notifier's board enumeration, so it resolves like the notifier read it
    (``pin_first_board_resolution``): a ``HERMES_KANBAN_DB`` pin outranks it."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kb.pin_first_board_resolution(), contextlib.closing(kbc.connect(board=card.get('board') or None)) as conn:
        task = kb.get_task(conn, card['task_id'])
        if task is None:
            return None, ''
        summary = (kb.latest_summary(conn, card['task_id']) or task.result or '') if task.status == 'done' else ''
        return task.status, summary.strip()


def refresh_card(card):
    """``card`` with its live status applied: a wake whose premise no longer holds (an unclaimed wait
    since claimed or done, a block since resolved) is relabelled with the card's current status. An
    unreadable board leaves the card as delivered."""
    kinds = card.get('kinds') or []
    premises = [kind for kind in kinds if kind in _PREMISE_HOLDS]
    if not card.get('task_id') or not premises:
        return card
    try:
        status, result = _live_task(card)
    except Exception:  # health: allow BLE001 -- a live read is best effort: the wake still goes in as delivered
        logger.debug('kanban wake fold: live state of %s unreadable', card['task_id'], exc_info=True)
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
    out.extend(rest)
    if status == 'done' and result and not any(line.startswith('Result:') for line in rest):
        out.insert(2, 'Result: ' + result.splitlines()[0][:400])
    return {**card, 'kinds': [{'done': 'completed'}.get(status, status)], 'text': '\n'.join(out).rstrip()}


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
    # The stored cards stay as delivered; the live relabel is redone each time the batch is rebuilt.
    cards = _merge(old_cards, new_cards)
    parts = [refresh_card(card)['text'] for card in cards]
    if len(cards) > 1:
        parts.insert(0, _FOLD_HEADER.format(count=len(cards)))
    if guidance:
        parts.append(guidance)
    event.text = '\n\n'.join(parts)
    event.metadata['kanban_cards'] = cards
    event.metadata['automation_identities'] = sorted({
        *(descriptor.get('identities') or [waiting['request_id']]),
        *(event.metadata.get('automation_identities') or [identity])})
    # The batch mutes only when every wake in it did.
    if descriptor.get('notification_category', 'result') == 'result':
        event.metadata.pop('notification_category', None)
