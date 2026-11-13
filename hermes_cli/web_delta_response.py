"""[daveey] Delta responses: answer a large JSON read with the difference from the copy the client holds.

Measured 2026-11-13 on the hub: six list/poll reads (sessions sidebar, projects tree, kanban board, sessions list,
cron jobs, runners fleet) carry almost all the bytes the Desktop pulls through its SSH tunnel, 2,052 KB a round.
Fetched again 20 s later and encoded against the first copy they are 12 KB (gzip: 286 KB) for under 8 ms of CPU
each. On a 60-175 KB/s link the polls filled the link; as deltas they do not.

Nothing here changes what a request returns or when: the handler runs as before and its exact bytes reach the
client. Only the encoding on the wire changes, and only for a client that asks (Desktop half:
apps/desktop/electron/delta-reads.ts).

- Request: ``X-Hermes-Delta-Accept: zstd`` and, optionally, ``X-Hermes-Delta-Base: <hash>`` of a body it holds.
- A GET whose answer is a 200 ``application/json`` body with a Content-Length in [MIN_BYTES, MAX_BYTES] and no
  Content-Encoding is remembered by its hash and answered with ``X-Hermes-Body-Hash``; when the named base is still
  remembered and the encoding is smaller, the body is a zstd frame using that base as its prefix, marked
  ``X-Hermes-Delta: zstd`` + ``X-Hermes-Delta-Base``. The client decodes, checks the hash, and on any mismatch asks
  again without a base.
- Everything else (no ask, other methods and statuses, streams, small or huge bodies, Python without
  ``compression.zstd``) passes through untouched.
- Every hashed or delta answer says ``Vary: X-Hermes-Delta-Accept, X-Hermes-Delta-Base`` (merged into the
  handler's own Vary), and a delta drops the handler's ``ETag``: its bytes are a frame, not that entity. A caching
  proxy in front of the gateway then cannot replay a delta to a client that did not ask for one. A delta also
  replaces the handler's ``Cache-Control`` with ``no-store`` (Cloudflare ignores Vary names other than
  Accept-Encoding); a whole hashed body is the handler's entity and keeps the handler's Cache-Control.

Bodies are remembered per credential: the store key is (hash of the request's session-token / Authorization /
Cookie headers, hash of the body), so a caller can only name a base that was served to that same credential. A
delta's length says how much the answer shares with its base, which must not become a way to probe what another
profile or principal was sent. A caller with no credential (the public paths pass auth without one) shares one
namespace that holds only what such callers were sent. A credential that changes on every request (single-use
grants) simply never gets a delta.

Bases outlive the process: the dashboard's store also keeps each body in ``<home>/cache/delta-bodies`` (0600 files
named ``<credential namespace>-<body hash>``, both hashes; 64 MB, least recently used out), which every backend of
that home shares. After an always-on roll the new backend still holds the bases the old one sent, so the Desktop's
first reads are deltas instead of whole bodies. A file is a base only when its bytes hash to the name's digest. It
never answers a request: the handler always runs.

Failure never costs the answer: if encoding raises, or the body's length is not its declared Content-Length, the
handler's response goes out as it was.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Optional

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

try:  # Python 3.14+
    from compression import zstd as _zstd
except ImportError:  # pragma: no cover - older interpreters serve whole bodies
    _zstd = None

ACCEPT_HEADER = "x-hermes-delta-accept"
BASE_HEADER = "x-hermes-delta-base"
HASH_HEADER = "x-hermes-body-hash"
DELTA_HEADER = "x-hermes-delta"
ENCODING = "zstd"
DELTA_MEDIA_TYPE = "application/vnd.hermes.delta+zstd"
MIN_BYTES = 16 * 1024
MAX_BYTES = 8 * 1024 * 1024
STORE_MAX_BYTES = 64 * 1024 * 1024
FRAMES_MAX_BYTES = 8 * 1024 * 1024
FRAMES_MAX_ENTRIES = 512
ZSTD_LEVEL = 3
CREDENTIAL_HEADERS = ("x-hermes-session-token", "authorization", "cookie")
VARY_ON = ("X-Hermes-Delta-Accept", "X-Hermes-Delta-Base")
_HEX32 = re.compile(r"[0-9a-f]{32}")

logger = logging.getLogger(__name__)


def body_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()[:32]


def credential_namespace(request: Headers) -> str:
    """One namespace per credential presented; "" for a request that presents none."""
    presented = [f"{name}={request.get(name, '')}" for name in CREDENTIAL_HEADERS if request.get(name)]
    return hashlib.sha256("\n".join(presented).encode()).hexdigest()[:32] if presented else ""


def encode_delta(body: bytes, base: bytes) -> bytes:
    """``body`` as a zstd frame that decodes only with ``base`` as its (raw-content) dictionary.

    The window covers base + body and long-distance matching is on: at the default 2 MiB window a 3.5 MB body
    with one changed field encoded to 36 KB; with both it is 430 bytes for 2 ms more (measured 2026-11-13)."""
    window_log = min(27, max(10, math.ceil(math.log2(len(base) + len(body) + 1))))
    options = {
        _zstd.CompressionParameter.compression_level: ZSTD_LEVEL,
        _zstd.CompressionParameter.window_log: window_log,
        _zstd.CompressionParameter.enable_long_distance_matching: 1,
    }
    return _zstd.compress(body, options=options, zstd_dict=_zstd.ZstdDict(base, is_raw=True).as_prefix)


class BodyStore:
    """Recently served bodies by hash, least recently used first out, bounded by total bytes.

    It also remembers recent delta frames by (namespace, base hash, body hash): a client polling an unchanged
    route names the same base for the same body every time, so the zstd encode runs once per change, not once
    per poll (t_01a1d59ed9eb75fe). The body hash is the route's change key; a new body is a new key, encoded
    on the request that first sees it. [daveey]

    With ``disk_dir`` every body is also written there (one 0600 file per key, LRU by mtime, the same byte budget),
    so a backend started later (an always-on roll, a restart) can still decode against a base an earlier backend
    sent. A file is used only when its bytes hash to the digest in its key; anything else is removed and the read
    goes whole. ``disk_dir`` may be a callable resolved on first use (the home is not final at import time).
    Frames are memory-only."""

    def __init__(
        self,
        max_bytes: int = STORE_MAX_BYTES,
        frames_max_bytes: int = FRAMES_MAX_BYTES,
        disk_dir: Path | Callable[[], Path] | None = None,
    ):
        self._max_bytes = max_bytes
        self._bodies: OrderedDict[str, bytes] = OrderedDict()
        self._bytes = 0
        self._frames_max_bytes = frames_max_bytes
        self._frames: OrderedDict[str, Optional[bytes]] = OrderedDict()
        self._frames_bytes = 0
        self._lock = threading.Lock()
        self._disk_dir = disk_dir

    def frame(self, key: str) -> tuple[bool, Optional[bytes]]:
        """(remembered, frame); a remembered ``None`` is an encode that came out no smaller than its body."""
        with self._lock:
            if key not in self._frames:
                return False, None
            self._frames.move_to_end(key)
            return True, self._frames[key]

    def put_frame(self, key: str, frame: Optional[bytes]) -> None:
        size = len(frame) if frame is not None else 0
        with self._lock:
            if key in self._frames or size > self._frames_max_bytes:
                return
            self._frames[key] = frame
            self._frames_bytes += size
            while self._frames_bytes > self._frames_max_bytes and self._frames:
                _, dropped = self._frames.popitem(last=False)
                self._frames_bytes -= len(dropped) if dropped is not None else 0
            while len(self._frames) > FRAMES_MAX_ENTRIES:
                _, dropped = self._frames.popitem(last=False)
                self._frames_bytes -= len(dropped) if dropped is not None else 0

    def get(self, digest: str) -> Optional[bytes]:
        with self._lock:
            body = self._bodies.get(digest)
            if body is not None:
                self._bodies.move_to_end(digest)
                return body
        body = self._disk_get(digest)
        if body is not None:
            self._remember(digest, body)
        return body

    def put(self, digest: str, body: bytes) -> None:
        if self._remember(digest, body):
            self._disk_put(digest, body)

    def _remember(self, digest: str, body: bytes) -> bool:
        """Hold ``body`` in memory; False when it already was."""
        with self._lock:
            if digest in self._bodies:
                self._bodies.move_to_end(digest)
                return False
            self._bodies[digest] = body
            self._bytes += len(body)
            while self._bytes > self._max_bytes and self._bodies:
                _, dropped = self._bodies.popitem(last=False)
                self._bytes -= len(dropped)
            return True

    def __len__(self) -> int:
        return len(self._bodies)

    # ── on disk ──────────────────────────────────────────────────────────────────────────────────────────

    def _dir(self) -> Optional[Path]:
        if callable(self._disk_dir):
            try:
                self._disk_dir = Path(self._disk_dir())
            except Exception:
                logger.warning("delta response: no directory for persisted bases; memory only", exc_info=True)
                self._disk_dir = None
        return self._disk_dir

    @staticmethod
    def _file_name(key: str) -> Optional[str]:
        """``<namespace or _>-<digest>``: both parts are hex hashes (the credential itself is never written)."""
        namespace, _, digest = key.rpartition(":")
        if not _HEX32.fullmatch(digest) or (namespace and not _HEX32.fullmatch(namespace)):
            return None
        return f"{namespace or '_'}-{digest}"

    def _disk_get(self, key: str) -> Optional[bytes]:
        root, name = self._dir(), self._file_name(key)
        if root is None or name is None:
            return None
        path = root / name
        try:
            body = path.read_bytes()
        except OSError:
            return None
        if body_hash(body) != key.rpartition(":")[2]:  # truncated, corrupted or not ours: never a base
            with contextlib.suppress(OSError):
                path.unlink()
            return None
        with contextlib.suppress(OSError):
            os.utime(path)  # most recently used
        return body

    def _disk_put(self, key: str, body: bytes) -> None:
        root, name = self._dir(), self._file_name(key)
        if root is None or name is None or len(body) > self._max_bytes:
            return
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = root / name
            if path.exists():
                os.utime(path)
                return
            tmp = root / f".{name}.{os.getpid()}.{threading.get_ident()}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(body)
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    tmp.unlink()
                raise
            self._disk_evict(root)
        except OSError:
            logger.warning("delta response: could not persist a base; memory only for it", exc_info=True)

    def _disk_evict(self, root: Path) -> None:
        """Drop the least recently used files until the directory fits the budget (shared by every backend)."""
        entries = []
        for entry in os.scandir(root):
            with contextlib.suppress(OSError):
                if entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    entries.append((st.st_mtime_ns, st.st_size, entry.path))
        total = sum(size for _, size, _ in entries)
        for _, size, path in sorted(entries):
            if total <= self._max_bytes:
                break
            with contextlib.suppress(OSError):
                os.unlink(path)
            total -= size


def default_disk_dir() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "cache" / "delta-bodies"


def _eligible(message: Message) -> bool:
    """A complete, plain JSON 200 of a size worth encoding (a stream has no Content-Length)."""
    if message["status"] != 200:
        return False
    headers = Headers(raw=message["headers"])
    length = headers.get("content-length", "")
    return (
        headers.get("content-type", "").split(";", 1)[0].strip().lower() == "application/json"
        and "content-encoding" not in headers
        and length.isascii()
        and length.isdecimal()
        and MIN_BYTES <= int(length) <= MAX_BYTES
    )


def _encode(body: bytes, base_digest: str, namespace: str, store: BodyStore) -> tuple[str, Optional[bytes]]:
    """(hash of body, delta frame or None). Runs off the event loop: hashing and zstd release the GIL.
    P1 pass ``delta.encode``: a skip is a frame served from the memo."""
    from hermes_cli import loop_accounting as la
    with la.pass_("delta.encode") as p:
        digest = body_hash(body)
        base = store.get(f"{namespace}:{base_digest}") if base_digest else None
        store.put(f"{namespace}:{digest}", body)
        if base is None:
            return digest, None
        key = f"{namespace}:{base_digest}:{digest}"
        remembered, frame = store.frame(key)
        if remembered:
            p.skip()
        else:
            p.change()
            frame = encode_delta(body, base)
            frame = frame if len(frame) < len(body) else None
            store.put_frame(key, frame)
        return digest, frame


def _merge_vary(headers: MutableHeaders) -> None:
    """One Vary header: the handler's names, then the two request headers the encoding depends on."""
    names: list[str] = []
    for value in headers.getlist("vary"):
        names.extend(name.strip() for name in value.split(",") if name.strip())
    if "*" not in names:
        held = {name.lower() for name in names}
        names.extend(name for name in VARY_ON if name.lower() not in held)
    del headers["vary"]
    headers["vary"] = ", ".join(names)


class DeltaResponseMiddleware:
    """See the module docstring. Pure ASGI: a response that is not eligible is never buffered."""

    def __init__(self, app: ASGIApp, store: Optional[BodyStore] = None):
        self.app = app
        self.store = store if store is not None else BodyStore(disk_dir=default_disk_dir)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if _zstd is None or scope["type"] != "http" or scope["method"] != "GET":
            await self.app(scope, receive, send)
            return
        request = Headers(scope=scope)
        if request.get(ACCEPT_HEADER, "").strip().lower() != ENCODING:
            await self.app(scope, receive, send)
            return
        held = _DeltaSend(send, request.get(BASE_HEADER, "").strip(), credential_namespace(request), self.store)
        await self.app(scope, receive, held)


class _DeltaSend:
    """The ``send`` of one asked request: holds an eligible response until its body is complete."""

    def __init__(self, send: Send, base_digest: str, namespace: str, store: BodyStore):
        self._send = send
        self._base_digest = base_digest
        self._namespace = namespace
        self._store = store
        self._start: Optional[Message] = None
        self._chunks: list[bytes] = []

    async def __call__(self, message: Message) -> None:
        if message["type"] == "http.response.start" and _eligible(message):
            self._start = message
            return
        if self._start is None:
            await self._send(message)
            return
        if message["type"] != "http.response.body":
            # Not a body we can collect (http.response.pathsend / zerocopysend: the server sends the file itself).
            # The held start goes out first, unchanged, with any body collected so far; the rest passes through.
            start, chunks = self._start, self._chunks
            self._start, self._chunks = None, []
            await self._send(start)
            if chunks:
                await self._send({"type": "http.response.body", "body": b"".join(chunks), "more_body": True})
            await self._send(message)
            return
        self._chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            await self._finish(b"".join(self._chunks))

    async def _finish(self, body: bytes) -> None:
        start = self._start
        self._start = None
        digest, frame = "", None
        # A body that is not its declared length is the server's to report, exactly as without this middleware.
        if len(body) == int(Headers(raw=start["headers"])["content-length"]):
            try:
                digest, frame = await asyncio.to_thread(
                    _encode, body, self._base_digest, self._namespace, self._store
                )
            except Exception:  # encoding is an optimisation: the answer still goes out whole
                logger.warning("delta response: encoding failed; sending the whole body", exc_info=True)
        if not digest:
            await self._send(start)
            await self._send({"type": "http.response.body", "body": body})
            return
        headers = MutableHeaders(raw=list(start["headers"]))
        headers[HASH_HEADER] = digest
        _merge_vary(headers)
        if frame is not None:
            del headers["etag"]  # the handler's validator names the whole body, not this frame
            # Only the client holding this base can use it, and Cloudflare ignores the Vary names above.
            del headers["cache-control"]
            headers["cache-control"] = "no-store"
            headers[DELTA_HEADER] = ENCODING
            headers[BASE_HEADER] = self._base_digest
            headers["content-type"] = DELTA_MEDIA_TYPE
            headers["content-length"] = str(len(frame))
        await self._send({**start, "headers": headers.raw})
        await self._send({"type": "http.response.body", "body": frame if frame is not None else body})
