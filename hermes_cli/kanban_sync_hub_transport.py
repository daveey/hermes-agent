"""Socket observation and response decoding for Board HTTP transports. [daveey]"""
import json


def decode_json(raw):
    """Malformed reply bytes are a board-local failure, not a retry signal.

    Keep decoding separate from application validation: arbitrary ValueError
    raised by a caller must not be mistaken for a broken HTTP response.
    """
    from hermes_cli.kanban_sync_client import HubError
    try:
        return json.loads(raw or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # Do not include raw bytes: replies may carry private Board contents.
        raise HubError(502, 'invalid JSON response') from exc

import urllib.request


class BoundedResponseError(ValueError):
    """A capped evidence response is too large or cannot be decoded safely."""


class BoundedResponseTooLarge(BoundedResponseError):
    """The Board's answer is deterministically over the cap (wire or decoded bytes).

    Unlike a malformed/truncated body this recurs on every retry for the same row.
    """


class BoundedRedirects(urllib.request.HTTPRedirectHandler):
    """Cap urllib's internal redirect-body drain as well as the final response."""

    def __init__(self, max_response_bytes):
        self.max_response_bytes = max_response_bytes

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.get_method() not in ("GET", "HEAD"):
            return None  # never follow an uncertain write, even by changing its method
        return super().redirect_request(req, fp, code, msg, headers, newurl)

    def http_error_302(self, req, fp, code, msg, headers):
        try:
            read_response(fp, self.max_response_bytes, deterministic=False)  # a redirect body is never evidence
        except (ValueError, OSError, EOFError):
            fp.close()
            raise
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def read_response(resp, max_response_bytes=None, *, deterministic=True) -> bytes:
    """Bound wire bytes before parsing, and decoded bytes for gzip/deflate replies.

    Uncapped callers retain the historical transport behavior. A capped caller must
    discard the connection on failure: the response may not have been drained.
    Content-Length is deliberately not trusted, including for compressed replies.
    Both caps always apply, but only a ``deterministic`` (2xx evidence) answer over a cap raises the typed
    :class:`BoundedResponseTooLarge`; an HTTP error or redirect body over the cap is transient and raises the
    generic :class:`BoundedResponseError`, so it can never quarantine a card.
    """
    too_large = BoundedResponseTooLarge if deterministic else BoundedResponseError
    if max_response_bytes is None:
        return resp.read()
    if type(max_response_bytes) is not int or max_response_bytes <= 0:
        raise ValueError("max_response_bytes must be a positive integer")
    raw = resp.read(max_response_bytes + 1)
    if len(raw) > max_response_bytes:
        raise too_large("Board response exceeds wire byte limit")
    encoding = (resp.headers.get("Content-Encoding") or "identity").strip().lower()
    if encoding in ("", "identity"):
        return raw
    import gzip
    import io
    import zlib
    try:
        if encoding == "gzip":
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                decoded = stream.read(max_response_bytes + 1)
        elif encoding == "deflate":
            decoder = zlib.decompressobj()
            decoded = decoder.decompress(raw, max_response_bytes + 1)
            if len(decoded) <= max_response_bytes and (not decoder.eof or decoder.unused_data):
                raise BoundedResponseError("Invalid deflate Board response")
        else:
            raise BoundedResponseError("Unsupported Board response content encoding")
    except (OSError, EOFError, zlib.error) as exc:
        raise BoundedResponseError("Invalid compressed Board response") from exc
    if len(decoded) > max_response_bytes:
        raise too_large("Board response exceeds decoded byte limit")
    return decoded


def peer_closed(conn) -> bool:
    """True when an idle kept-alive socket is readable (the server sent FIN) or already gone."""
    import select
    sock = getattr(conn, "sock", None)
    if sock is None:
        return True
    try:  # poll, not select: select raises for fds >= 1024, which a busy gateway has
        if not hasattr(select, "poll"):  # Windows: select has no fd ceiling there
            return bool(select.select([sock], [], [], 0)[0])
        poller = select.poll()
        poller.register(sock, select.POLLIN | select.POLLPRI)
        return bool(poller.poll(0))
    except (OSError, ValueError):
        return True
