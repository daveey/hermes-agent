"""Shadow comparison of legacy JSON with one pinned fact generation. [daveey]

This harness never invokes action executors or reads live sources. The capture
owner supplies legacy responses from the SAME captured source state and clock.
Passing this comparison alone is not source-coverage or live-parity evidence.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from hermes_cli.fact_contract import Ready, freeze
from hermes_cli.fact_store import Snapshot


@dataclass(frozen=True)
class ExpectedView:
    name: str
    selector: Mapping
    encoded: bytes

    def __post_init__(self):
        object.__setattr__(self, "selector", freeze(self.selector))


@dataclass(frozen=True)
class ParityFailure:
    case: int
    reason: str


@dataclass(frozen=True)
class ParityReport:
    epoch: str
    rev: int
    checked: int
    failures: tuple[ParityFailure, ...]

    @property
    def passed(self) -> bool:
        return self.checked > 0 and not self.failures


def _difference(expected, actual) -> str | None:
    # Python equality conflates true/1/1.0, unlike the typed wire contract.
    if type(expected) is not type(actual):
        return "json_type"
    if isinstance(expected, dict):
        if expected.keys() != actual.keys():
            return "json_fields"
        pairs = ((value, actual[key]) for key, value in expected.items())
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            return "json_length"
        pairs = zip(expected, actual)
    else:
        return None if expected == actual else "json_value"
    for left, right in pairs:
        reason = _difference(left, right)
        if reason:
            return reason
    return None


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("non-finite JSON number")


def _decode(encoded):
    return json.loads(encoded, object_pairs_hook=_object, parse_constant=_constant)


def _selector_value(value):
    if isinstance(value, Mapping):
        return {key: _selector_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_selector_value(item) for item in value]
    return value


def compare_views(snapshot: Snapshot, expected: Sequence[ExpectedView]) -> ParityReport:
    """No hydration, fallback, clock reads or encoding; diagnostics omit payloads."""
    failures = []
    for index, case in enumerate(expected):
        result = snapshot.view(case.name, _selector_value(case.selector))
        if not isinstance(result, Ready):
            reason = "view_not_ready"
        else:
            try:
                reason = _difference(_decode(case.encoded), _decode(result.encoded))
            except (ValueError, UnicodeError, TypeError, RecursionError):
                reason = "invalid_json"
        if reason:
            failures.append(ParityFailure(index, reason))
    return ParityReport(snapshot.epoch, snapshot.rev, len(expected), tuple(failures))
