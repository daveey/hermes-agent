"""Validated FD headroom defaults; invalid keys never disable safety or accelerate ticks."""
from collections.abc import Mapping
import math

DEFAULTS = {'enabled': True, 'warn_ratio': 0.5, 'raise_ratio': 0.75,
            'horizon_hours': 6, 'interval_seconds': 300, 'max_soft': 262144}


def validated(config):
    runtime = config.get('runtime', {}) if isinstance(config, Mapping) else None
    raw = runtime.get('fd_headroom', {}) if isinstance(runtime, Mapping) else None
    if not isinstance(raw, Mapping):
        return dict(DEFAULTS), ['fd_headroom']
    result, errors = dict(DEFAULTS), []
    for key, default in DEFAULTS.items():
        value = raw.get(key, default)
        if key == 'enabled':
            valid = isinstance(value, bool)
        else:
            valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                     and math.isfinite(value))
            if valid:
                if key in ('warn_ratio', 'raise_ratio'):
                    valid = 0 < value < 1
                elif key == 'interval_seconds':
                    valid = 30 <= value <= 600
                elif key == 'max_soft':
                    valid = isinstance(value, int) and value >= 1024
                else:
                    valid = value > 0
        if valid:
            result[key] = value
        else:
            errors.append(key)
    if result['warn_ratio'] >= result['raise_ratio']:
        errors.extend(['warn_ratio', 'raise_ratio'])
        result.update(warn_ratio=DEFAULTS['warn_ratio'], raise_ratio=DEFAULTS['raise_ratio'])
    return result, errors
