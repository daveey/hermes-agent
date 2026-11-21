"""Authenticated read-only Hub records for private card-answer adapters. [daveey]"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from hermes_cli.kanban_card_answer import AnswerRefused


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward the enrolled gateway credential to a redirect target.
        return None


class Records:
    """Configuration and credentials come from this machine, never an envelope."""

    def __init__(self):
        path = Path(os.environ.get('HERMES_MAIL_CONFIG') or Path.home() / '.config/hermes-mail/config.json')
        config = json.loads(path.read_text(encoding='utf-8-sig'))
        self.gateway = config.get('gateway')
        if not isinstance(self.gateway, str) or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}', self.gateway):
            raise AnswerRefused('local_gateway_invalid')
        self.url = (config.get('hub_url') or config.get('url') or 'https://hub.dbloom.in').rstrip('/')
        parsed = urllib.parse.urlsplit(self.url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise AnswerRefused('hub_endpoint_invalid')
        self._key = (path.parent / 'gateway.key').read_text(encoding='utf-8-sig').strip()
        if not self._key or '\n' in self._key or '\r' in self._key:
            raise AnswerRefused('hub_credential_invalid')
        self._opener = urllib.request.build_opener(_NoRedirect())

    def read(self, request_id: str) -> dict:
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.:-]{0,199}', request_id):
            raise AnswerRefused('courier_request_id_invalid')
        request = urllib.request.Request(self.url + '/v2/sync/requests/' + urllib.parse.quote(request_id, safe=''),
                                         headers={'Authorization': 'Bearer ' + self._key}, method='GET')
        try:
            with self._opener.open(request, timeout=10) as response:
                raw = response.read(1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise AnswerRefused('courier_record_missing') from None
            raise OSError('courier_record_unavailable') from None
        except (urllib.error.URLError, TimeoutError):
            raise OSError('courier_record_unavailable') from None
        if len(raw) > 1024 * 1024:
            raise AnswerRefused('courier_record_invalid')
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise AnswerRefused('courier_record_invalid') from None
        record = body.get('request') if isinstance(body, dict) else None
        if not isinstance(record, dict) or record.get('id') != request_id:
            raise AnswerRefused('courier_record_invalid')
        return record
