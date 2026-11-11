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


def _request(hub, method: str, path: str, data: bytes | None, cap: int) -> bytes:
    from hermes_cli.kanban_sync_client import HubError
    headers = {**hub._headers(), "content-type": "application/octet-stream"}
    request = urllib.request.Request(hub.ident.url + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=hub._remaining(5.0)) as response:
            body = response.read(cap + 1)
            if len(body) > cap:
                raise ValueError("attachment response exceeds size limit")
            return body
    except urllib.error.HTTPError as exc:
        # Do not persist response bodies, URLs or gateway keys in retry diagnostics.
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
    return json.loads(body)


def download(hub, board: str, uid: str) -> bytes:
    return _request(hub, "GET", _path(board, uid) + "/blob", None, KANBAN_ATTACHMENT_MAX_BYTES)
