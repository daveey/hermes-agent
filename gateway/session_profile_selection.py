"""Owner-committed selections of a managed session that follows its profile. [daveey]"""
import json
from dataclasses import asdict, replace

from hermes_state_runtime import RuntimeStoreError

PREFIX = 'gateway.profile_selection.v1.'


def released_pick(policy, model):
    """*policy* without its recorded pick (``model.selection_profile``), running *model*: what a pick
    that ended on a profile edit leaves. The followed ``model:`` block already replaces the frozen one
    once the pick no longer holds, so dropping the marker changes nothing this turn runs; it only stops
    the pick from holding again when the profile later returns to the target it diverged from."""
    config = json.loads(policy.config_json)
    block = config.get('model')
    if isinstance(block, dict):
        config['model'] = {k: v for k, v in block.items() if k != 'selection_profile'}
    return replace(policy, model=model, config_json=json.dumps(config))


def _frozen_pick(saved_policy):
    """The receipt's own route record (launch model + frozen ``model:`` block). An explicit
    ``session.mutate model`` rewrites it in the same transaction that records ITS switch intent, so a
    change here means another writer moved the allocation-directing intent since this selection last
    committed; a ``/reasoning`` edit or rewind (which bump the generation too) leave it alone."""
    block = json.loads(saved_policy['config_json']).get('model')
    return [saved_policy.get('model'), block if isinstance(block, dict) else None]


def _memo(row):
    """The remembered ``{'selection': [model, provider, frozen pick], 'physical': id}``, or None when
    nothing (or only an unreleased earlier format of this branch) is remembered."""
    try:
        memo = json.loads(row[0]) if row is not None else None
    except (TypeError, ValueError):
        return None
    if not isinstance(memo, dict) or not isinstance(memo.get('selection'), list) \
            or not isinstance(memo.get('physical'), str):
        return None
    return memo


def _already_allocated(physical, model):
    """No memo yet (first commit after upgrade): whether the lineage already runs a class allocation for
    the class *model* selects, so an intent would only re-roll it. An exact pin there (the class change
    this session never committed) or an exact selection (re-pinning the same tuple, never a reroll)
    still records the intent."""
    from agent.model_routing import is_class_selector
    if not is_class_selector(model):
        return False
    import sqlite3
    from agent.model_routing_state import RoutingState
    from hermes_constants import get_hermes_home
    try:
        current = RoutingState(get_hermes_home() / 'model-routing.sqlite3').peek(
            physical, model.removeprefix('class:'))
    except sqlite3.Error:
        return False
    return bool(current) and not current.get('exact_pin')


def commit_selection(authority, ref, policy, config, model, generation, *, ended=False):
    """Authorize this effective selection, not arbitrary exact-pin failover. Returns the policy the
    owner now publishes for the session (*policy*, or its released copy when *ended*).

    Only the owner calls this with the single profile read the turn builds from (in process or in the
    child). Under the owner's write transaction: the owner epoch is current, the durable launch policy
    is still this following policy, and the logical runtime generation is the admission's; otherwise
    ``revision_conflict`` / the epoch fence refuse and nothing is written. The switch intent names the
    session's PHYSICAL transcript (``local_physical_target``), the id the agent is built on in process
    and that ``reserve_admission_worker`` assigns a worker after compression, through the canonical
    writer (``record_in_transaction``), as ``model_in_transaction`` does for an explicit pick.

    The selection (tuple + the receipt's frozen pick) is remembered beside the physical target it was
    committed on, independently of runtime usage stamps. Only a SELECTION change records a new intent:
    a profile edit, or an explicit pick another build consumed (frozen pick moved). A compaction (new
    physical target, same selection) is a relocation: a still-pending intent moves to the new id as the
    same event and a consumed one stays consumed, so a class allocation never re-rolls on it. With
    nothing remembered (first commit after upgrade) the intent is recorded unless the lineage already
    runs a class allocation for the selected class (``_already_allocated``).
    *ended*: the policy's recorded pick no longer holds on that read
    (``LocalSessionPolicy.effective_selection``); the same transaction commits its end to the receipt,
    without a generation bump (nothing this generation runs changes), and the owner's live policy is
    republished from it. A concurrent prepared mutation then fails its snapshot check
    (``revision_conflict``) rather than writing over the end.
    """
    if not policy.follows_profile or policy.ignore_user_config:
        return policy
    from agent.model_routing_switch_intent import record_in_transaction, relocate_in_transaction
    from hermes_state_local import POLICY_PREFIX
    from hermes_state_local_lineage import local_physical_target
    from hermes_state_runtime import _epoch
    from hermes_state_policy_config import decode_receipt, replace_receipt
    provider = (config.get('model') or {}).get('provider') or ''
    expected = json.loads(json.dumps(asdict(policy)))
    released = json.loads(json.dumps(asdict(released_pick(policy, model)))) if ended else None

    def write(conn):
        _epoch(conn, authority.epoch)
        receipt = conn.execute('SELECT value FROM state_meta WHERE key=?',
                               (POLICY_PREFIX + ref.session_id,)).fetchone()
        row = conn.execute('SELECT runtime_generation FROM sessions WHERE id=?', (ref.session_id,)).fetchone()
        saved = decode_receipt(conn, receipt[0]) if receipt is not None else None
        if saved is None or saved['policy'] != expected or row is None or row[0] != generation:
            raise RuntimeStoreError('revision_conflict')
        physical = local_physical_target(conn, ref.session_id)
        if released is not None:
            saved['policy'] = released
            replace_receipt(conn, ref.session_id, saved)
        # Remember the pick the receipt now holds: after an end that is the RELEASED policy, which the
        # next turn's *expected* is, so an unchanged selection there is not mistaken for a new one.
        current = released if released is not None else expected
        selection = json.loads(json.dumps([model, provider, _frozen_pick(current)]))
        key = PREFIX + ref.session_id
        old = _memo(conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone())
        if old is not None and old['selection'] == selection:
            if old['physical'] == physical:
                return
            # Relocation, not a selection change: carry a still-pending intent as the same event.
            relocate_in_transaction(conn, old['physical'], physical, model, provider)
        elif old is not None or not _already_allocated(physical, model):
            record_in_transaction(conn, physical, model, provider, generation)
        conn.execute('INSERT INTO state_meta (key,value) VALUES (?,?) '
                     'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (key, json.dumps({'selection': selection, 'physical': physical}, sort_keys=True)))
    authority.db._execute_write(write)
    if released is None:
        return policy
    from gateway.session_local import publish_local_policy
    # The agent this build replaces already ran the followed route: nothing cached to evict.
    publish_local_policy(authority, ref.session_id, evict=False)
    from gateway.session_local_profile import live_policy
    return live_policy(authority, ref)
