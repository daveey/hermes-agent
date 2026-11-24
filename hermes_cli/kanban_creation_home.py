"""Separate a pool worker's administrative creation home from execution labels.

An execution pin is not a reason to entrust parked work to an ephemeral gateway.
The receiving hub still owns all admission, dependency and origin-custody gates.
"""
from __future__ import annotations


def plan(args, *, cfg, gateway, machine_ok):
    """Return a hub-bound view/decision, or None for an ordinary non-pool gateway.

    Unsupported host-local fields refuse rather than silently dropping metadata or
    falling back to a worker-owned card. No freshness/capacity failure authorizes a
    change of administrative home.
    """
    from hermes_cli import kanban_placement as placement
    from hermes_cli import kanban_worker_placement as workers

    queued_gateway = bool(placement._Cfg(cfg).queue_label(gateway))
    if not queued_gateway:
        return None  # explicit opt-in to the fork's managed worker queue policy
    # Enrolled worker slot names remain identifiable while registry refresh is
    # unavailable. Discovery failure must not recreate worker-local ownership.
    worker_slot = isinstance(gateway, str) and gateway.startswith('worker-')
    if not worker_slot and not placement.linux_pool_gateway(gateway, cfg):
        return None
    if args.get('board') not in (None, '', 'default'):
        return args, {'where': 'refuse', 'pin': 'hub', 'why': 'worker creation home supports only the default board'}
    if ((args.get('workspace_kind') or 'scratch') != 'scratch'
            or args.get('workspace_path') or args.get('project_id') or args.get('project')):
        return args, {'where': 'refuse', 'pin': 'hub',
                      'why': 'publish host-local source before forwarding to the canonical creation home'}
    labels = args.get('runs_on')
    pin = placement.canonical_pin(args.get('placement') or 'auto', gateway, cfg)
    if labels is None and pin not in ('', 'auto', 'local', 'here', gateway):
        if not machine_ok(pin):
            return args, {'where': 'refuse', 'pin': pin, 'why': 'execution pin is not an enrolled pickup machine'}
        labels = placement.route_labels(pin)
    if not labels:
        # Local means caller affinity, not evidence of a machine-only need. The
        # queue planner supplies marker/coordination capabilities without a pin.
        portable = {k: v for k, v in args.items() if k not in ('placement', 'runs_on')}
        queued = workers.queue_decision(portable, cfg, gateway, 'auto', machine_ok)
        if not queued or not queued.get('runs_on'):
            return args, {'where': 'refuse', 'pin': 'hub',
                          'why': 'cannot establish a claimable execution route for canonical creation'}
        labels = queued['runs_on']
    view = {**args, 'placement': 'auto', 'runs_on': list(labels)}
    return view, {'where': 'hub', 'canonical_home': True,
                  'why': 'shared administrative home; execution remains constrained by runs_on'}


def prepare(args, *, cfg, gateway, machine_ok):
    from hermes_cli import kanban_placement as placement
    try:
        home = plan(args, cfg=cfg, gateway=gateway, machine_ok=machine_ok)
    except Exception as exc:  # health: allow BLE001 -- opted-in pool creates fail closed, never worker-local
        if not placement._Cfg(cfg).queue_label(gateway):
            raise
        home = args, {'where': 'refuse', 'pin': 'hub', 'why': f'canonical home planning failed: {exc}'}
    view, decision = home if home is not None else (args, placement.decide(args, machine_ok=machine_ok))
    return home, view, decision


def transport_options(args, *, hub_cli):
    """Expand/contract preflight against the installed client, before any request write."""
    from hermes_cli import kanban_placement as placement
    rc, help_text, _ = placement.hub_runner([hub_cli, 'create-task', '--help'])
    if rc or 'canonical-home-v1' not in help_text or '--option' not in help_text:
        raise ValueError('canonical_home_transport_unavailable: update the courier client before forwarding')
    if args.get('pin_mandatory'):
        raise ValueError('canonical_home_transport_unavailable: mandatory model pin cannot be forwarded')
    keys = ('runs_on', 'parents', 'tenant', 'max_retries', 'initial_status', 'human_request')
    # Preserve zero retries and empty label/dependency lists. Core's normal owner
    # entrypoint validates typed values; never stringify structured safety fields.
    return {key: args[key] for key in keys if args.get(key) is not None}


def result(args, fields, *, tid, status, target):
    """Report pending creation honestly without calling the hub an execution pin."""
    labels = args['runs_on']
    request = fields['placed']['request_id']
    state = f'Created on {target} as {tid}.' if tid else f'Creation request {request} is queued for {target}; task ID pending.'
    return {'task_id': tid, 'status': status or ('created' if tid else 'queued'), **fields,
            'runs_where': 'board', 'runs_on': labels,
            'note': f'{state} Administrative home is shared; execution is constrained by runs_on={labels}. '
                    'Do not repeat creation; an identical retry uses the same request.'}
