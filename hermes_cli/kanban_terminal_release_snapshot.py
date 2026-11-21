"""Keep a proven terminal release across transport age, never across owner changes. [daveey]"""
from hermes_cli.kanban_origin_ledger import _owner_evidence

_IDENTITY = ('key', 'session', 'gateway', 'profile', 'board', 'id', 'origin_gateway', 'origin_profile', 'via')


def observation(task: dict, key: tuple) -> dict:
    """A snapshot token only for an exactly qualified, hash-verified terminal publication."""
    profile = key[1]
    evidence = _owner_evidence(task)
    if (profile is None or task.get('status') not in ('done', 'archived')
            or any(task.get(k) != v for k, v in zip(('gateway', 'profile', 'board', 'id'), key))
            or evidence.get('verified') is not True):
        return {}
    return {'_release_observation': evidence['task_hash']}


def preserve(cards: list[dict], previous: list[dict] | None) -> list[dict]:
    """Staleness cannot undo the SAME observed release. New/unproven/reopened records still wait.

    The token is computed from the raw owner record before projection changes its status to unknown.
    Persisted origin-work snapshots carry it across process restarts; old snapshots without it prove nothing.
    """
    released = {tuple(c.get(k) for k in _IDENTITY): c for c in previous or ()
                if c.get('released') is True and c.get('status') in ('done', 'archived')
                and c.get('_release_observation')}
    out = []
    for card in cards:
        prior = released.get(tuple(card.get(k) for k in _IDENTITY))
        if (card.get('stale') and prior is not None
                and card.get('_release_observation') == prior['_release_observation']):
            card = {**card, 'status': prior['status'], 'released': True, 'unfinished': False}
        out.append(card)
    return out
