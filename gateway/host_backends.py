"""Per-process entries for every chat-hosting serve backend, next to the host role records.

:mod:`gateway.host_rendezvous` keeps ONE record per role. Two Desktop-owned backends can run at
once (the supervised always-on SSH backend plus a local Desktop pool child, or two pool children),
and the one that finds ``desktop-serve`` held publishes nothing. ``hermes plugins enable`` and
hermes-sync then never reach the chats it hosts: on daveey-mini the always-on backend on :9147
kept its old plugin hooks after the Desktop child that held the role exited (t_01a1ce929e118cf2).

So every serve backend also publishes ``serve-backends/<pid>.json`` (a :class:`HostRecord`) and a
0600 ``serve-backends/<pid>.token``, whatever its role-lock outcome. This is discovery only: the
attach/refuse ladder still reads the role records, and an entry never makes a backend a host owner.
An entry is trusted only while its pid still names the same process incarnation; a provably dead
one is pruned by the next reader.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional, Sequence

from gateway import host_rendezvous as hr
from utils import atomic_json_write

logger = logging.getLogger(__name__)

DIRNAME = "serve-backends"
#: Roles an entry may carry: the chat-hosting serve roles, never the gateway.
ENTRY_ROLES = (hr.ROLE_SERVE, hr.ROLE_DESKTOP_SERVE)


def backends_dir() -> Path:
    return hr.host_state_dir() / DIRNAME


def entry_path(pid: int) -> Path:
    return backends_dir() / f"{int(pid)}.json"


def token_path(pid: int) -> Path:
    return backends_dir() / f"{int(pid)}.token"


def _ensure_dir() -> Path:
    from hermes_dir_acl import make_user_dir

    hr.ensure_host_state_dir()
    directory = backends_dir()
    make_user_dir(directory, parents=True, exist_ok=True, mode=0o700)
    return directory


def publish(
    role: str,
    *,
    host: str = "",
    port: Optional[int] = None,
    profiles: Sequence[str] = (),
    token: Optional[str] = None,
    home: str = "",
) -> Optional[hr.HostRecord]:
    """Publish this process's entry (token first, then the record). ``None`` when not written.

    A backend with no port or no token cannot be dialed, so it publishes nothing."""
    if role not in ENTRY_ROLES or not token or not (isinstance(port, int) and port > 0):
        return None
    from gateway.status import get_process_start_time

    pid = os.getpid()
    record = hr.HostRecord(
        role=role,
        pid=pid,
        create_time=hr.process_create_time(),
        start_time=get_process_start_time(pid),
        boot_epoch=hr._current_boot_epoch(),
        host=str(host or ""),
        port=port,
        protocol_version=hr.HOST_PROTOCOL_VERSION,
        token_fingerprint=hr.token_fingerprint(token),
        profiles=tuple(str(p) for p in profiles),
        updated_at=datetime.now(UTC).isoformat(),
        home=str(home or ""),
    )
    try:
        _ensure_dir()
        hr._write_private_text(token_path(pid), token)
        atomic_json_write(entry_path(pid), record.to_json(), mode=0o600)
    except OSError:
        logger.warning("serve backend entry could not be published; plugin activation will not "
                       "find this backend unless it holds its role record", exc_info=True)
        return None
    hr.register_exit_cleanup(clear)
    return record


def clear() -> None:
    """Remove this process's entry + token (never another process's)."""
    pid = os.getpid()
    for path in (entry_path(pid), token_path(pid)):
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def _read(path: Path) -> Optional[hr.HostRecord]:
    if not hr._record_is_own(path):
        return None
    try:
        record = hr.HostRecord.from_json(json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return None
    if record is None or record.role not in ENTRY_ROLES or path.stem != str(record.pid):
        return None
    return record


def _prune(path: Path, record: hr.HostRecord) -> None:
    """Drop a provably dead entry, unless its pid was republished since it was read."""
    if _read(path) != record:
        return
    for stale in (path, token_path(record.pid)):
        with contextlib.suppress(OSError):
            stale.unlink(missing_ok=True)


def read_entries() -> list[hr.HostRecord]:
    """Every live entry, oldest process first. A provably dead one is pruned; an unprovable one
    (no psutil, permission denied) is kept for the token check and the dial to settle."""
    directory = backends_dir()
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return []
    out = []
    for path in paths:
        record = _read(path)
        if record is None:
            continue
        if record.protocol_version != hr.HOST_PROTOCOL_VERSION:
            continue
        if hr._record_incarnation_matches(record) is False:
            _prune(path, record)
            continue
        out.append(record)
    return sorted(out, key=lambda r: (r.create_time or 0.0, r.pid))


def read_token(record: hr.HostRecord) -> str:
    """The entry token for ``record``'s pid (``""`` when absent)."""
    try:
        return token_path(record.pid).read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def token_is_consistent(record: hr.HostRecord) -> bool:
    """Does the entry token still hash to the record's fingerprint (no torn restart)?"""
    return bool(record.token_fingerprint) and hr.token_fingerprint(read_token(record)) == record.token_fingerprint


def token_for(record: hr.HostRecord) -> str:
    """Session token to dial ``record``: its own entry token, else its role token when the role
    record still names the same pid (a backend from before entries existed). Never another
    process's token."""
    token = read_token(record) if isinstance(getattr(record, "pid", None), int) else ""
    if token and hr.token_fingerprint(token) == getattr(record, "token_fingerprint", ""):
        return token
    role = getattr(record, "role", hr.ROLE_SERVE)
    role_record = hr.read_record(role, include_stale=True)
    if role_record is None or role_record.pid == getattr(record, "pid", None):
        # The role's own token: the record is that role's owner, or (no role record to compare,
        # e.g. a pre-entry caller handing in its own record object) the only token there is.
        return hr.read_token(role)
    return ""
