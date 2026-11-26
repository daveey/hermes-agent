"""Private binding source policy and strict authenticated record decoding. [daveey]"""
from __future__ import annotations

import json
import math
import re

from hermes_cli.kanban_card_answer import AnswerRefused
from hermes_cli.kanban_card_answer_transport import Records


def configured_source() -> dict:
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli.config_backend import ConfigBackendUnavailable

    try:
        config = load_user_config_effective(fail_closed=True)
    except ConfigBackendUnavailable:
        raise OSError('bb_binding_source_unavailable') from None
    section = config.get('bb_owner_response')
    source = section.get('source') if isinstance(section, dict) else None
    if (not isinstance(source, dict) or set(source) != {'gateway', 'profile'}
            or any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', v)
                   for v in source.values())):
        raise OSError('bb_binding_source_unavailable')
    return source


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate key')
        value[key] = item
    return value


def _finite(value):
    raise ValueError('nonfinite constant')


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('nonfinite number')
    return number


class BindingRecords(Records):
    def read(self, request_id: str) -> dict:
        raw = self.raw(request_id)
        try:
            body = json.loads(raw, object_pairs_hook=_unique, parse_constant=_finite, parse_float=_float)
        except (ValueError, UnicodeError, RecursionError):
            raise AnswerRefused('courier_record_invalid') from None
        record = body.get('request') if isinstance(body, dict) else None
        fields = {'id', 'from', 'to_gw', 'coll', 'key', 'action', 'args', 'base_version',
                  'state', 'claims', 'result', 'created_at', 'done_at'}
        if (not isinstance(body, dict) or set(body) != {'request'}
                or not isinstance(record, dict) or not set(record) <= fields
                or record.get('id') != request_id):
            raise AnswerRefused('courier_record_invalid')
        return record
