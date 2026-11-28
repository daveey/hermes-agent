"""Authenticate actual exec code objects against the declared immutable source closure.

This is a contract check in the dedicated producer, not an OS security sandbox.
A scoped audit observer sees spec.loader.exec_module as well as ordinary imports,
so a timestamp-valid stale pyc cannot be certified by hashing newer source bytes.
"""
from __future__ import annotations

import hashlib
from importlib.machinery import SourceFileLoader, SourcelessFileLoader
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

from hermes_cli.serve_producer_manifest import _read, verify_declared
from hermes_cli.serve_producer_protocol import ProtocolError

_guards = set()
_lock = threading.RLock()
_installed = False


def _observe(event, args):
    if event != 'exec':
        return
    code = args[0]
    # The embedded filename in a relocated .pyc is not its loader's origin. Authenticate
    # both: explicit spec.loader.exec_module uses this same stdlib loader frame.
    frame = sys._getframe(1)
    if (frame.f_code.co_name == '_call_with_frames_removed'
            and frame.f_globals.get('__name__') in {'importlib._bootstrap', '_frozen_importlib'}
            and frame.f_back is not None):
        frame = frame.f_back
    loader = frame.f_locals.get('self')
    origin = loader.path if isinstance(loader, (SourceFileLoader, SourcelessFileLoader)) else None
    with _lock:
        for guard in tuple(_guards):
            guard.check(code)
            if origin is not None:
                guard.check(code, origin=origin)


class _Guard:
    def __init__(self, manifest, base, entry):
        self.root = base.resolve()
        self.expected = {}
        self.executed = {}
        files = verify_declared(manifest, base)
        for relative, digest in entry.items():
            if relative in files and files[relative] != digest:
                raise ProtocolError('entry manifest digest mismatch')
            files[relative] = digest
        for relative, digest in files.items():
            path = self.root / relative
            try:
                path.resolve().relative_to(self.root)
            except ValueError:
                raise ProtocolError('manifest member outside provider closure') from None
            source = _read(path)
            if hashlib.sha256(source).hexdigest() != digest:
                raise ProtocolError('manifest digest mismatch')
            self.expected[str(path.resolve())] = (compile(source, str(path), 'exec', dont_inherit=True), digest)

    def check(self, code, *, origin=None):
        path = Path(code.co_filename if origin is None else origin)
        # Dynamic code has no filesystem identity. Never infer one from the caller's
        # cwd; real loader origins are checked separately, including relative origins.
        if origin is None and not path.is_absolute():
            return
        try:
            path = path.resolve()
            path.relative_to(self.root)
        except (ValueError, OSError):
            return
        expected = self.expected.get(str(path))
        if expected is None:
            raise ProtocolError('undeclared provider module executed')
        if code != expected[0]:
            raise ProtocolError('loaded provider executable bytes mismatch')
        self.executed[str(path)] = expected[1]


@contextmanager
def authenticated_execution(manifest, base, *, entry):
    """Keep the immutable closure enforced through bootstrap and every later provider call."""
    global _installed
    guard = _Guard(manifest, Path(base), entry)
    with _lock:
        if not _installed:
            sys.addaudithook(_observe)
            _installed = True
        _guards.add(guard)
    try:
        yield guard.executed
    finally:
        with _lock:
            _guards.discard(guard)
