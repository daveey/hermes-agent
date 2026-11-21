"""Fixed root observer: no payload execution, Board client or credential lookup.

Exec this module (do not spawn beneath a free-form shell) with one base64 JSON
request identifying the current latch. The receipt echoes its canonical hash.
Only this fixed process may exclude itself from its own host census.
"""
from __future__ import annotations

import base64
import json
import os
import sys

from hermes_cli import worker_drain_census as census
from hermes_cli import worker_drain_fence as fence

FIELDS = {'version', 'op', 'iid', 'boot', 'node_epoch', 'generation', 'attempt', 'phase'}


def _binding(ident, latch):
    return {'version': 1, 'op': 'census', 'iid': ident.iid, 'boot': ident.boot,
            'node_epoch': ident.node_epoch, **{k: latch[k] for k in ('generation', 'attempt', 'phase')}}


def observe_bound(request: dict) -> dict:
    if os.geteuid() != 0:  # windows-footgun: ok -- root-only Linux observer
        raise fence.DrainRefused('unknown-state', 'host census needs root')
    if (set(request) != FIELDS or type(request['version']) is not int or request['version'] != 1
            or request['op'] != 'census' or type(request['generation']) is not int
            or not isinstance(request['attempt'], str)):
        raise fence.DrainRefused('unknown-state', 'invalid census request')
    act = fence.load_activation()
    if act is None:
        raise fence.DrainRefused('unknown-state', 'fence not activated')
    ident = fence.current_identity(act)
    root, _ = fence.census_root()
    before = fence.read_latch(root, ident)
    if request != _binding(ident, before):
        raise fence.DrainRefused('unknown-state', 'census request differs from live latch')
    observations = census.observe_all()
    # A transition during the scan invalidates the entire receipt.
    after = fence.read_latch(root, fence.current_identity(act))
    if after != before:
        raise fence.DrainRefused('unknown-state', 'latch changed during census')
    positive = sum(o.count for o in observations.values() if o.state == census.POSITIVE)
    zero = all(o.state == census.ZERO for o in observations.values())
    return {'version': 1, 'request_hash': fence.request_hash(request), 'binding': request,
            'complete': zero, 'count': positive if positive else (0 if zero else None),
            'adapters': {k: {'state': o.state, 'count': o.count, 'reason': o.reason}
                         for k, o in observations.items()}}


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if __name__ != '__main__':
            raise ValueError('observer must execute as the fixed module entry point')
        if len(args) != 1 or len(args[0]) > 16384:
            raise ValueError('one bounded census request required')
        request = json.loads(base64.b64decode(args[0], validate=True))
        if not isinstance(request, dict):
            raise ValueError('request must be an object')
        census._fixed_observer_pid = os.getpid()
        try:
            receipt = observe_bound(request)
        finally:
            census._fixed_observer_pid = None
        print(json.dumps(receipt, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, fence.DrainRefused) as exc:
        print(json.dumps({'complete': False, 'count': None, 'reason': str(exc)}, sort_keys=True))
        return 75


if __name__ == '__main__':
    raise SystemExit(main())
