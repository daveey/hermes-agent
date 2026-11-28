"""Server-side home binding for the shared panel producer: canonical home + life + authorization generation.

A serve process names the ONE home it is authorized to serve; the browser, request labels and plugin
strings never do. The binding is derived from facts the platform already owns:

* canonical home: ``Path.resolve(strict=True)``, so aliases of one home share one binding and equal-label
  homes under different roots never collide;
* lifecycle generation: the home directory's device/inode plus its live (not tombstoned) state, so a
  deleted-then-recreated profile is a new generation even under the same name;
* authorization generation: the serving ``SessionAuthority`` (profile, instance, epoch) when the process
  has one, otherwise the standalone process's own launch authority (its pid and start time, valid for its
  own launch home only).

A binding is not a capability: every consumer re-derives it and compares. ``is_current`` is False the
moment any component changed, and the relay then revokes queued work, outputs and events before another
reply.
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path

MAX_HOME_BYTES = 4096


class HomeRevoked(Exception):
    """The home is not (or no longer) authorized to be served by this process."""


def _digest(*parts) -> str:
    return hashlib.sha256('\0'.join(str(part) for part in parts).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class HomeBinding:
    home: str
    lifecycle_generation: str
    authorization_generation: str

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.home, self.lifecycle_generation, self.authorization_generation)

    def wire(self) -> dict:
        return {'home': self.home, 'lifecycle_generation': self.lifecycle_generation,
                'authorization_generation': self.authorization_generation}

    @classmethod
    def from_wire(cls, value) -> HomeBinding:
        """Strict decode of an untrusted partition claim: exactly three bounded nonempty strings."""
        if not isinstance(value, dict) or set(value) != {'home', 'lifecycle_generation', 'authorization_generation'}:
            raise ValueError('invalid home partition')
        home, life, auth = (value['home'], value['lifecycle_generation'], value['authorization_generation'])
        for item in (home, life, auth):
            if not isinstance(item, str) or not item or len(item.encode('utf-8')) > MAX_HOME_BYTES:
                raise ValueError('invalid home partition')
        if not os.path.isabs(home):
            raise ValueError('invalid home partition')
        return cls(home, life, auth)


def _canonical(home) -> Path:
    try:
        path = Path(home).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HomeRevoked('home does not exist') from exc
    if not path.is_dir():
        raise HomeRevoked('home is not a directory')
    return path


def lifecycle_generation(home) -> tuple[Path, str]:
    """Canonical home and its lifecycle generation; a tombstoned named profile is revoked."""
    from hermes_constants import named_profile_home, named_profile_is_deleted

    path = _canonical(home)
    named = named_profile_home(path)
    if named is not None and named_profile_is_deleted(named):
        raise HomeRevoked('profile home is tombstoned')
    info = os.stat(path)
    return path, _digest('life', path, info.st_dev, info.st_ino)


def _process_start() -> str:
    try:
        with open('/proc/self/stat', 'rb') as handle:
            return handle.read().rsplit(b')', 1)[1].split()[19].decode('ascii')
    except (OSError, IndexError):
        return str(_STARTED_NS)  # non-Linux: the import stamp still differs between two process lives


_STARTED_NS = time.time_ns()


def authorization_generation(path: Path, app_state=None) -> str:
    """Generation of the authority that lets THIS process serve *path*; HomeRevoked when none does."""
    runner = getattr(app_state, 'gateway_runner', None)
    authority = getattr(app_state, 'session_authority', None)
    if runner is not None:
        from gateway.session_authorities import authority_for_home
        authority = authority_for_home(runner, path)
    elif authority is not None:
        try:
            owned = Path(authority.db.db_path).resolve().parent
        except (AttributeError, OSError):
            raise HomeRevoked('session authority has no home') from None
        if owned != path:
            authority = None
    if runner is not None or authority is not None:
        if authority is None or getattr(authority, 'retiring', False) or getattr(authority, 'retired', False):
            raise HomeRevoked('home is not served by an active authority')
        return _digest('authority', authority.profile_id, authority.instance_id, authority.epoch)
    # Standalone serve/dashboard: the process authorizes only its own launch home.
    from hermes_constants import get_process_hermes_home

    try:
        launch = Path(get_process_hermes_home()).resolve(strict=True)
    except OSError as exc:
        raise HomeRevoked('launch home unavailable') from exc
    if launch != path:
        raise HomeRevoked('standalone serve is not authorized for another home')
    return _digest('standalone', path, os.getpid(), _process_start())


def bind(home, app_state=None) -> HomeBinding:
    path, life = lifecycle_generation(home)
    return HomeBinding(str(path), life, authorization_generation(path, app_state))


def is_current(binding: HomeBinding, app_state=None) -> bool:
    try:
        return bind(binding.home, app_state) == binding
    except (HomeRevoked, OSError, ValueError):
        return False
