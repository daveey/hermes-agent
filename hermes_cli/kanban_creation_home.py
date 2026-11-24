"""Separate a pool worker's administrative creation home from execution labels.

An execution pin is not a reason to entrust parked work to an ephemeral gateway.
The receiving hub still owns all admission, dependency and origin-custody gates.
"""
from __future__ import annotations


def _worker_config(cfg, gateway):
    """Extend an opted-in hub pool to dynamic worker slots without editing config.

    Production lists intake gateways (hub/Macs), not autoscaled worker names.
    An explicit worker route still wins; an unconfigured install stays unchanged.
    """
    from hermes_cli import kanban_placement as placement
    policy = placement._Cfg(cfg)
    label = policy.queue_label('hub')
    if (isinstance(gateway, str) and gateway.startswith('worker-')
            and not policy.queue_label(gateway) and label):
        return {**cfg, 'worker_queue': {**cfg['worker_queue'], gateway: label}}
    return cfg


def plan(args, *, cfg, gateway, machine_ok):
    """Return a hub-bound view/decision, or None for an ordinary non-pool gateway.

    Unsupported host-local fields refuse rather than silently dropping metadata or
    falling back to a worker-owned card. No freshness/capacity failure authorizes a
    change of administrative home.
    """
    from hermes_cli import kanban_placement as placement
    from hermes_cli import kanban_worker_placement as workers

    cfg = _worker_config(cfg, gateway)
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
    if (args.get('workspace_kind') not in (None, 'scratch')
            or args.get('workspace_path') or args.get('project_id') or args.get('project')):
        return args, {'where': 'refuse', 'pin': 'hub',
                      'why': 'publish host-local source before forwarding to the canonical creation home'}
    from hermes_cli.kanban_worker_node import normalize_runs_on
    labels = normalize_runs_on(args.get('runs_on'))
    pin = placement.canonical_pin(args.get('placement') or 'auto', gateway, cfg)
    if labels is None and pin not in ('', 'auto'):
        execution = gateway if pin in ('local', 'here') else pin
        if not machine_ok or not machine_ok(execution):
            return args, {'where': 'refuse', 'pin': execution, 'why': 'execution pin is not an enrolled pickup machine'}
        labels = placement.route_labels(execution)
    if labels is None:
        # Determine execution independently: a real creator-local artifact or
        # operator/this-host requirement remains pinned, but never owns storage.
        from hermes_cli import kanban_home_marker_placement as markers
        local = markers.classify(args, cfg, gateway)
        if local and local.get('decision') == 'keep':
            if not machine_ok or not machine_ok(gateway):
                return args, {'where': 'refuse', 'pin': gateway, 'why': 'local execution need has no enrolled pickup machine'}
            labels = placement.route_labels(gateway)
        else:
            # These fields are carried by canonical-home-v1, but not the legacy
            # courier. Exclude only from execution-route classification, keeping
            # them in the forwarded view and its normal receiving validation.
            portable = {k: v for k, v in args.items() if k not in (
                'placement', 'runs_on', 'max_retries', 'initial_status', 'human_request')}
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
    cfg = _worker_config(cfg, gateway)
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
    if rc or not all(token in help_text for token in ('canonical-home-v2', '--canonical-home', '--option')):
        raise ValueError('canonical_home_transport_unavailable: update the courier client before forwarding')
    from hermes_cli.kanban_creation_contract import payload
    return payload(args)


def result(args, fields, *, tid, status, target):
    """Report pending creation honestly without calling the hub an execution pin."""
    labels = args['runs_on']
    request = fields['placed']['request_id']
    state = f'Created on {target} as {tid}.' if tid else f'Creation request {request} is queued for {target}; task ID pending.'
    return {'task_id': tid, 'status': status or ('created' if tid else 'queued'), **fields,
            'runs_where': 'board', 'runs_on': labels,
            'note': f'{state} Administrative home is shared; execution is constrained by runs_on={labels}. '
                    'Do not repeat creation; an identical retry uses the same request.'}
