"""Resolved-versus-held classification of private stop history in Board custody. [daveey]

Pure function over ordered events (Board ``ord``, or a caller's explicit order). It only decides
whether an administrative observation may be recorded; it never releases, promotes, renews or
reruns anything, and a recorded deadline grants no execution. Fail closed:

* safety evidence is never resolved, whatever follows it;
* a card in ``triage`` with private history is currently held;
* an ordinary budget stop (no ``cause``) is resolved only by a later ``promoted_manual``/``archived``;
* a scope stop needs a later renewal that froze its uid AND a caller-verified mandate digest
  (``verified_cover``). Event bytes alone cannot re-read the mandate comment, so an unverified
  renewal stays ``mandate_unverifiable``;
* anything unparseable or unattributable stays held.
"""
from __future__ import annotations

import json

STOPS = frozenset({'spend_budget_triaged', 'decision_hold_restricted'})
RENEWAL = 'scope_renewed'
PRIVATE = STOPS | {RENEWAL}
DECISIONS = frozenset({'promoted_manual', 'archived'})


class PrivateHold(ValueError):
    """Private stop history is still in force: an explicit blocker, not a missing deadline."""

    def __init__(self, task_id: str, reason: str):
        super().__init__(f'private stop currently held on {task_id}: {reason}')
        self.task_id, self.reason = task_id, reason


def _payload(event):
    raw = event.get('payload')
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


def held(status, events, *, verified_cover=frozenset()):
    """Reason the private history still holds, or ``None`` when there is none or all of it is resolved."""
    private = [e for e in events if e['kind'] in PRIVATE]
    if not private:
        return None
    if status == 'triage':
        return 'card_in_triage'
    payloads = {}
    for event in private:
        payload = _payload(event)
        if payload is None:
            return 'private_payload_unverifiable'
        if payload.get('cause') == 'safety_refusal' or payload.get('superseded_by_safety_stop') is True:
            return 'safety_restriction'
        payloads[event['uid']] = payload
    decisions = sorted(e['ord'] for e in events if e['kind'] in DECISIONS)
    renewals = []
    for event in private:
        if event['kind'] != RENEWAL:
            continue
        covered = payloads[event['uid']].get('covered_restriction_uids')
        if not isinstance(covered, list) or not covered or not all(isinstance(u, str) and u for u in covered):
            return 'renewal_unverifiable'
        renewals.append((event['ord'], frozenset(covered)))
    cover = verified_cover if isinstance(verified_cover, frozenset) else frozenset(verified_cover)
    for event in private:
        if event['kind'] not in STOPS:
            continue
        floor = event['ord']
        cause = payloads[event['uid']].get('cause')
        if cause == 'scope_stopped':
            after = [o for o, covered in renewals if o > floor and event['uid'] in covered]
            if not after:
                return 'scope_stop_not_renewed'
            if event['uid'] not in cover:
                return 'mandate_unverifiable'
            floor = min(after)
        elif cause is not None or event['kind'] != 'spend_budget_triaged':
            return 'restriction_unattributable'
        if not any(o > floor for o in decisions):
            return 'stop_undecided'
    # A renewal whose stop is not in this stream still carries an unchecked mandate: fail closed.
    if any(not covered <= cover for _ord, covered in renewals):
        return 'mandate_unverifiable'
    return None
