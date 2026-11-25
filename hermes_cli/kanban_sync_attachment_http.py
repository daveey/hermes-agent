"""Bounded authenticated binary transport for Board attachment custody. [daveey]"""
from __future__ import annotations

import json
import http.client
import urllib.error
import urllib.parse
import urllib.request

from hermes_cli.kanban_db import KANBAN_ATTACHMENT_MAX_BYTES


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # gateway credentials never follow a redirect, even on PUT


def _request(hub, method: str, path: str, data: bytes | None, cap: int, *,
             content_type: str = "application/octet-stream", timeout: float = 5.0) -> bytes:
    from hermes_cli.kanban_sync_client import HubError
    from hermes_cli.kanban_sync_attachment_deadline import Transfer
    headers = {**hub._headers(), "content-type": content_type}
    request = urllib.request.Request(hub.ident.url + path, data=data, headers=headers, method=method)
    budget = hub._remaining(timeout)
    transfer = Transfer(budget)

    def read():
        opener = urllib.request.build_opener(_NoRedirect(), *transfer.handlers())
        with opener.open(request, timeout=budget) as response:
            body = response.read(cap + 1)
            if len(body) > cap:
                raise ValueError("attachment response exceeds size limit")
            return body

    try:
        return transfer.run(read)
    except urllib.error.HTTPError as exc:
        # Do not persist response bodies, URLs or gateway keys in retry diagnostics.
        exc.close()
        raise HubError(exc.code, "attachment transfer refused") from exc
    except http.client.HTTPException as exc:
        raise OSError("incomplete attachment response") from exc


def _path(board: str, uid: str) -> str:
    return (f"/v2/boards/{urllib.parse.quote(board, safe='')}/attachments/"
            f"{urllib.parse.quote(uid, safe='')}")


def upload(hub, board: str, uid: str, digest: str, data: bytes) -> dict:
    if len(data) > KANBAN_ATTACHMENT_MAX_BYTES:
        raise ValueError("attachment exceeds size limit")
    body = _request(hub, "PUT", _path(board, uid) + "/blobs/" + digest, data, 4096)
    from hermes_cli.kanban_sync_client import HubError
    try:
        return json.loads(body)
    except (ValueError, RecursionError) as exc:
        raise HubError(502, "invalid JSON response") from exc


def download(hub, board: str, uid: str) -> bytes:
    return _request(hub, "GET", _path(board, uid) + "/blob", None, KANBAN_ATTACHMENT_MAX_BYTES)
