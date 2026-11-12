"""SSRF-checked, size-capped URL download for ``kanban_attach_url`` (split from kanban_tools). [daveey]"""
from __future__ import annotations

from typing import Optional

_MAX_ATTACH_URL_REDIRECTS = 5


def _download_url_with_cap(url: str, max_bytes: int) -> tuple[bytes, Optional[str]]:
    """Fetch ``url`` over http(s) capped at ``max_bytes`` -> ``(data, content_type)``.
    Every hop is SSRF-checked (redirects followed manually) so a model-controlled URL, or a
    public host 302ing, cannot reach loopback/private/cloud-metadata ranges. ``ValueError``
    for bad scheme, blocked target, too many redirects, or a body over the cap (checked
    while streaming, so nothing oversize is buffered)."""
    from urllib.parse import urljoin, urlparse
    import httpx
    from tools.url_safety import is_safe_url
    current_url = url
    for _ in range(_MAX_ATTACH_URL_REDIRECTS + 1):
        scheme = (urlparse(current_url).scheme or "").lower()
        if scheme not in ("http", "https"):
            raise ValueError(f"unsupported URL scheme {scheme!r}; only http/https are allowed")
        if not is_safe_url(current_url):
            raise ValueError(
                f"URL blocked by SSRF protection (private/internal address): {current_url}")
        chunks: list[bytes] = []
        total = 0
        with httpx.stream("GET", current_url, headers={"User-Agent": "hermes-kanban/attach"},
                          timeout=30, follow_redirects=False) as resp:
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    raise ValueError(f"redirect without Location header from {current_url}")
                current_url = urljoin(current_url, location)
                continue
            resp.raise_for_status()
            content_type = (resp.headers.get("content-type") or "").split(";")[0].strip() or None
            for chunk in resp.iter_bytes(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"attachment exceeds {max_bytes // (1024 * 1024)} MB limit")
                chunks.append(chunk)
        return b"".join(chunks), content_type
    raise ValueError(f"too many redirects fetching {url}")
