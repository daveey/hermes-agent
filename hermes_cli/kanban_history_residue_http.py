"""Bounded authenticated historical observation I/O, never op settlement. [daveey]

observe is a low-level producer, not a live-input authorization gate. Callers must
validate local source/fences separately before creating an intent. Once any intent
exists, retries are GET-only, including an intent left before the first send.
Missing readback therefore remains unresolved; this API never replaces/replays it.
"""
from __future__ import annotations

import http.client
import urllib.error
import urllib.parse
import urllib.request

from hermes_cli import kanban_history_residue_custody as custody
from hermes_cli.kanban_history_residue_source import Refused, _require, _text, strict_object

MAX_RESPONSE = 2 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def scope(hub, board):
    """Bind custody to the actual credential-bearing Hub, never a caller-supplied scope."""
    endpoint = hub.ident.url
    parts = urllib.parse.urlsplit(endpoint)
    _require(parts.scheme == 'https' or (parts.scheme == 'http' and parts.hostname in
             ('localhost', '127.0.0.1', '::1')), 'endpoint_transport')
    _require(bool(parts.hostname) and not parts.username and not parts.password
             and not parts.query and not parts.fragment, 'endpoint_identity')
    host = f'[{parts.hostname.lower()}]' if ':' in parts.hostname else parts.hostname.lower()
    port = parts.port
    default = 443 if parts.scheme == 'https' else 80
    canonical = f'{parts.scheme}://{host}' + (f':{port}' if port is not None and port != default else '')
    # Reject aliases rather than silently rewriting the operator's custody coordinates.
    _require(endpoint == canonical, 'endpoint_noncanonical')
    _require(_text(board) and _text(hub.ident.gateway) and _text(hub.ident.key)
             and all(33 <= ord(c) <= 126 for c in hub.ident.key), 'endpoint_identity')
    return endpoint, board, hub.ident.gateway


def _path(board, op_id=None, *, current=False):
    path = f'/v2/boards/{urllib.parse.quote(board, safe="")}/history-residue'
    if op_id is not None:
        path += '?' + urllib.parse.urlencode({'op_id': op_id, **({'current': '1'} if current else {})})
    return path


def _request(hub, method, path, body):
    """No redirects, implicit retries or unbounded response decoding, even for errors."""
    from hermes_cli.kanban_sync_attachment_deadline import Transfer
    from hermes_cli.kanban_sync_client import HubError

    budget = hub._remaining(min(5.0, hub.timeout))
    transfer = Transfer(budget)
    req = urllib.request.Request(hub.ident.url + path, method=method, data=body,
                                 headers=hub._headers())

    def read():
        local = urllib.parse.urlsplit(hub.ident.url).hostname in ('localhost', '127.0.0.1', '::1')
        proxies = (urllib.request.ProxyHandler({}),) if local else ()
        opener = urllib.request.build_opener(_NoRedirect(), *proxies, *transfer.handlers())
        with opener.open(req, timeout=budget) as response:
            raw = response.read(MAX_RESPONSE + 1)
            _require(len(raw) <= MAX_RESPONSE, 'response_size')
            return raw

    try:
        return transfer.run(read)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise HubError(exc.code, 'history observation refused') from exc
    except http.client.HTTPException as exc:
        raise OSError('incomplete history observation') from exc


def reconcile(conn, hub, board, op_id):
    """Exact readback only. No POST, source mutation or current-authority claim."""
    identity = scope(hub, board)
    old = custody.lookup(conn, identity, op_id)
    _require(old is not None, 'intent_missing')
    raw = _request(hub, 'GET', _path(board, op_id), None)
    _require(raw.strip() != b'null', 'observation_unconfirmed')
    text = raw.decode('utf-8', 'strict')
    # seal also checks exact source, reason, original parent version and false authority flags.
    custody.seal(conn, identity, op_id, text)
    return {'recorded': True, 'op_id': op_id, 'current_authority_verified': False}


def observe(conn, hub, board, request_json):
    """New intent sends at most once; existing intent can only reconcile by GET.

    Even successful POST replies require a separate authenticated readback. A lost
    reply or malformed answer cannot manufacture custody; missing GET stays open.
    """
    from hermes_cli.kanban_sync_client import HubError

    identity = scope(hub, board)
    _, source = custody._request(request_json)
    op_id = source['op_id']
    if not custody.stage_once(conn, identity, request_json):
        return reconcile(conn, hub, board, op_id)
    try:
        # Send the exact durable request bytes, not a re-encoded Python object.
        _request(hub, 'POST', _path(board), request_json.encode('utf-8'))
    except (OSError, HubError, Refused, ValueError):
        # No replacement POST: only exact GET may resolve this uncertain outcome.
        pass
    return reconcile(conn, hub, board, op_id)


def current(hub, board, op_id):
    """Raw fresh read for a future cut-bound consumer; this alone authorizes nothing."""
    scope(hub, board)
    _require(_text(op_id), 'op_id_invalid')
    raw = _request(hub, 'GET', _path(board, op_id, current=True), None)
    text = raw.decode('utf-8', 'strict')
    strict_object(text, max_bytes=MAX_RESPONSE)
    return text
