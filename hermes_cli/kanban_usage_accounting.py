"""Run-scoped accounting debt is independent of lifecycle/spend-window resets. [daveey]

The event's run_id is a LOCAL foreign key: sync transports it as immutable run_uid.
Never join a remote payload's integer run id, or add session observations to receipts.
A missing receipt is unknown, including a short interrupted admitted turn. Recovery is
arrival of the original bound worker's receipt through the ordinary event outbox/feed;
this gate neither writes history nor releases any independent decision.
"""
from __future__ import annotations

import json
import math


def valid_counter(value):
    return type(value) is int and value >= 0


def receipts(conn, task_id, run_ids):
    """Cumulative lower bounds per local FK, independent of receipt delivery order.

    These are NOT invented finalized totals. Duplicate/older observations cannot
    subtract consumption. Missing keys stay absent; only evidenced zero is zero.
    """
    if not run_ids:
        return {}
    marks = ','.join('?' for _ in run_ids)
    rows = conn.execute(
        f"SELECT run_id,payload FROM task_events WHERE task_id=? AND kind='worker_result' "
        f"AND run_id IN ({marks})", (task_id, *run_ids))
    result = {}
    for run_id, raw in rows:
        try:
            payload = json.loads(raw or '{}')
        except (ValueError, TypeError):
            continue
        usage = payload.get('usage') if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            continue
        known = {k: v for k, v in usage.items() if k in (
            'input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens') and valid_counter(v)}
        cost = usage.get('cost_usd')
        if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
            known['cost_usd'] = cost
        if known:
            dest = result.setdefault(int(run_id), {})
            for key, value in known.items():
                dest[key] = max(dest.get(key, value), value)
    return result


def no_turn(conn, task_id, run_id, usage):
    """Source-interpreter proof that construction failed before any turn, not a zero estimate.

    Never apply this to a missing receipt, a worker-loss/restart marker or a midrun
    exception. Conflicting positive usage or another result without the proof wins.
    """
    if any(value > 0 for value in usage.values()):
        return False
    bound = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='worker_bound' "
        "ORDER BY id DESC LIMIT 1", (task_id, run_id)).fetchone()
    try:
        identity = json.loads(bound[0]) if bound else {}
        results = [json.loads(row[0]) for row in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='worker_result'",
            (task_id, run_id))]
        return bool(identity.get('pid') and identity.get('claim_lock') and results and all(
            result.get('usage_no_turn') is True and 'usage' not in result
            and result.get('pid') == identity['pid']
            and result.get('claim_lock') == identity['claim_lock'] for result in results))
    except (ValueError, TypeError, AttributeError):
        return False


def settled(conn, task_id, run_ids, usages):
    """Legacy numeric receipts retain compatibility; modern partials need a final.

    Conflicting finals or a final below any observed lower bound remain pending.
    Neither arrival order nor an unbound session snapshot resolves a conflict.
    """
    if not run_ids:
        return set()
    marks = ','.join('?' for _ in run_ids)
    modern, finals, legacy = set(), {}, {}
    keys = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens')
    for rid, raw in conn.execute(
            f"SELECT run_id,payload FROM task_events WHERE task_id=? AND kind='worker_result' "
            f"AND run_id IN ({marks})", (task_id, *run_ids)):
        try:
            payload = json.loads(raw or '{}')
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        usage = payload.get('usage')
        if 'usage_final' not in payload:
            # A failed/interrupted legacy request's last numeric snapshot is
            # also not evidence that every request returned its usage.
            if (payload.get('exit_code') in (None, 0) and not payload.get('continuable')
                    and isinstance(usage, dict) and valid_counter(usage.get('cache_read_tokens'))):
                legacy.setdefault(rid, set()).add(usage['cache_read_tokens'])
            continue
        modern.add(rid)
        if (payload['usage_final'] is True and payload.get('exit_code') == 0
                and not payload.get('continuable') and isinstance(usage, dict)
                and all(valid_counter(usage.get(k)) for k in keys)):
            finals.setdefault(rid, set()).add(tuple(usage[k] for k in keys))
    good = {rid for rid, values in legacy.items() if rid not in modern and len(values) == 1
            and next(iter(values)) >= usages.get(rid, {}).get('cache_read_tokens', 0)}
    for rid, values in finals.items():
        if len(values) != 1:
            continue
        final = next(iter(values))
        if all(value >= usages.get(rid, {}).get(key, 0) for key, value in zip(keys, final)):
            good.add(rid)
    good.update(rid for rid in run_ids if rid not in good and no_turn(conn, task_id, rid, usages.get(rid, {})))
    return good


MODES = ('observe', 'enforce')
DEFAULT_MODE = 'observe'


def gate_policy(cfg=None):
    """``(mode, enforce_after)`` from ``kanban.usage_accounting``; unreadable config fails closed.

    ``observe`` (default until exits publish finals): debt stays visible through :func:`pending`
    and spend reports but admission does not refuse on it. On 2026-11-26 the gate held 37/37
    ready cards (286 runs) because continue/block/schedule/retain/reclaim exits never produce a
    final receipt and nothing can settle them, so the pool ran 2 cards with 28 free slots.
    ``enforce`` refuses on debt from runs closed at or after ``enforce_after`` (epoch seconds;
    unset = every run). Budget and decision holds are independent and never relaxed here.
    """
    if cfg is None:
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
        except Exception:  # health: allow BLE001 -- unreadable policy fails closed
            return 'enforce', None
    section = ((cfg or {}).get('kanban') or {}).get('usage_accounting') or {}
    mode = section.get('mode', DEFAULT_MODE) if isinstance(section, dict) else DEFAULT_MODE
    if mode not in MODES:
        mode = 'enforce'
    after = section.get('enforce_after') if isinstance(section, dict) else None
    if isinstance(after, bool) or not isinstance(after, (int, float)) or not math.isfinite(after):
        after = None
    return mode, after


def enforced(conn, task_id, cfg=None):
    """The :func:`pending` runs admission refuses on under :func:`gate_policy`."""
    mode, after = gate_policy(cfg)
    if mode != 'enforce':
        return []
    runs = pending(conn, task_id)
    if not runs or after is None:
        return runs
    marks = ','.join('?' for _ in runs)
    ended = dict(conn.execute(
        f"SELECT id,ended_at FROM task_runs WHERE task_id=? AND id IN ({marks})", (task_id, *runs)))
    # An unclosed row has no end time yet: it is new by definition.
    return [rid for rid in runs if ended.get(rid) is None or ended[rid] >= after]


def pending(conn, task_id):
    """Closed admitted/bound turns and ambiguous releases without cache accounting.

    Do not use the renewable spend window or duration threshold here. A release
    arriving before its admission event is still a debt unless explicitly unused.
    Pre-admission owner refusals remain healthy; an admission always wins over an
    unused marker. NULL ended_at closed outcomes are ambiguous, never proof of zero.
    """
    rows = conn.execute(
        "SELECT r.id,r.outcome,r.ended_at FROM task_runs r JOIN tasks t ON t.id=r.task_id WHERE r.task_id=? "
        "AND (r.ended_at IS NOT NULL OR r.status!='running' OR t.status!='running' "
        "OR t.current_run_id IS NULL OR t.current_run_id!=r.id)",
        (task_id,)).fetchall()
    usages = receipts(conn, task_id, [r[0] for r in rows])
    admitted = {r[0] for r in conn.execute(
        "SELECT DISTINCT run_id FROM task_events WHERE task_id=? AND kind IN ('owner_admitted','worker_bound')",
        (task_id,))}
    unused, used, released_runs = set(), set(), set()
    for run_id, raw in conn.execute(
            "SELECT run_id,payload FROM task_events WHERE task_id=? AND kind='released'", (task_id,)):
        released_runs.add(run_id)
        try:
            payload = json.loads(raw or '{}')
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get('unused_grant') is True:
            unused.add(run_id)
        if isinstance(payload, dict) and payload.get('unused_grant') is False:
            used.add(run_id)
    unused.difference_update(used)
    good = settled(conn, task_id, [r[0] for r in rows], usages)
    # A final event alone cannot authorize while the run-row closure is still
    # in flight: spend() excludes unclosed rows and would omit these known tokens.
    return [rid for rid, outcome, ended in rows if (rid in admitted or (
        (outcome == 'released' or rid in released_runs) and rid not in unused))
        and (rid not in good or ended is None)]
