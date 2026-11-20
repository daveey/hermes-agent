"""Durable node-local worker drain latch and work leases (stdlib only).

Separate from the updater hold. State lives beneath the shared account root
(``worker_compute_capacity.policy_root()/core-update``) so every profile, custom home and
CPU-policy-off install sees the same latch. The latch binds IID, kernel boot and a random attempt.

Enforcement is gated by a root-owned activation file (written last by the installer). Absent
activation: every hook is a no-op. Present but malformed, or bound state lost/mismatched: deny.

Phases: open -> draining -> sealed, draining -> cancelled (never after seal intent), cancelled/open
-> higher generation. Missing, corrupt, other-boot or other-IID state never reads as open.
Work leases are separately flocked close-only files: closing never unlinks or unlocks, and a lease
is busy only when an independent fresh nonblocking flock fails.
"""
from __future__ import annotations

import importlib
import os
from typing import Any

# Linux worker nodes only; on Windows enforcement can never be activated.
fcntl: Any = importlib.import_module('fcntl') if os.name == 'posix' else None
import hashlib
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

ACTIVATION_PATH = Path('/var/lib/hermes-worker/drain-activation.json')
LATCH_FILE, INSTALL_FILE, LEASE_DIR, LOCK_FILE = 'worker-drain.json', 'worker-drain.install.json', 'worker-drain-leases', 'admission.lock'
PHASES = ('open', 'draining', 'sealed', 'cancelled')
OPS = ('establish', 'cancel', 'seal')
REQUEST_FIELDS = {'version', 'op', 'iid', 'boot', 'node_epoch', 'attempt', 'generation', 'request_id',
                  'issued_at_ms', 'not_after_ms'}
MUTATION_DEADLINE_S = 25
IID_RE = re.compile(r'i-(?:[0-9a-f]{8}|[0-9a-f]{17})\Z')
REASONS = ('draining', 'sealed', 'unknown-state', 'identity-mismatch')


class DrainRefused(RuntimeError):
    """Admission refused. ``reason`` is one of REASONS."""

    def __init__(self, reason: str, detail: str = ''):
        super().__init__(f'worker drain admission refused: {reason}' + (f' ({detail})' if detail else ''))
        self.reason = reason


@dataclass(frozen=True)
class Identity:
    iid: str
    boot: str
    node_epoch: str


def read_boot_id() -> str:
    return Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()


def request_hash(req: dict) -> str:
    return hashlib.sha256(_canon(req)).hexdigest()


def load_activation(path: Path = ACTIVATION_PATH) -> dict | None:
    """None when absent (hooks no-op). Raises DrainRefused('unknown-state') when present but invalid."""
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DrainRefused('unknown-state', f'activation unreadable: {type(exc).__name__}') from exc
    try:
        doc = json.loads(raw)
        if (doc.get('version') != 1 or not IID_RE.match(doc.get('iid', '')) or not isinstance(doc.get('uid'), int)
                or not doc.get('node_epoch') or not os.path.isabs(doc.get('home', ''))):
            raise ValueError('shape')
    except (ValueError, AttributeError) as exc:
        raise DrainRefused('unknown-state', 'activation malformed') from exc
    return doc


def _state_root(root: Path) -> Path:
    return Path(root) / 'core-update'


@contextmanager
def account_lock(root: Path, *, timeout: float = 2.0):
    """Nonblocking-poll flock on the shared admission.lock; never unlinks or replaces it."""
    d = _state_root(root)
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / LOCK_FILE, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DrainRefused('unknown-state', 'admission lock busy') from None
                time.sleep(0.01)
        yield
    finally:
        os.close(fd)


def _write_atomic(path: Path, doc: dict) -> None:
    tmp = path.with_name(f'.{path.name}.{uuid.uuid4().hex}')
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, _canon(doc))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _read_json(path: Path) -> dict:
    try:
        doc = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise DrainRefused('unknown-state', f'{path.name} unreadable') from exc
    if not isinstance(doc, dict) or doc.get('version') != 1:
        raise DrainRefused('unknown-state', f'{path.name} malformed')
    return doc


def install_state(root: Path, ident: Identity) -> None:
    """Installer only: immutable install binding (O_EXCL) plus an open latch at generation 0."""
    d = _state_root(root)
    with account_lock(root):
        d.mkdir(parents=True, exist_ok=True)
        (d / LEASE_DIR).mkdir(mode=0o700, exist_ok=True)
        binding = {'version': 1, 'iid': ident.iid, 'node_epoch': ident.node_epoch}
        try:
            fd = os.open(d / INSTALL_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if _read_json(d / INSTALL_FILE) != binding:
                raise DrainRefused('identity-mismatch', 'install binding differs') from None
        else:
            try:
                os.write(fd, _canon(binding))
                os.fsync(fd)
            finally:
                os.close(fd)
        if not (d / LATCH_FILE).exists():
            _write_atomic(d / LATCH_FILE, _latch_doc(ident, 'open', 0, '', None))


def _latch_doc(ident: Identity, phase: str, generation: int, attempt: str, last) -> dict:
    return {'version': 1, 'iid': ident.iid, 'boot': ident.boot, 'node_epoch': ident.node_epoch, 'phase': phase,
            'generation': generation, 'attempt': attempt, 'last': last}


def read_latch(root: Path, ident: Identity) -> dict:
    """Validated latch. Missing/corrupt/other-IID/other-epoch/other-boot raise; never read as open."""
    d = _state_root(root)
    install = _read_json(d / INSTALL_FILE)
    latch = _read_json(d / LATCH_FILE)
    if install.get('iid') != ident.iid or install.get('node_epoch') != ident.node_epoch:
        raise DrainRefused('identity-mismatch', 'install binding')
    if latch.get('iid') != ident.iid or latch.get('node_epoch') != ident.node_epoch:
        raise DrainRefused('identity-mismatch', 'latch binding')
    if latch.get('boot') != ident.boot and latch.get('phase') != 'open':
        raise DrainRefused('unknown-state', 'latch bound to another boot')
    if latch.get('phase') not in PHASES or not isinstance(latch.get('generation'), int):
        raise DrainRefused('unknown-state', 'latch malformed')
    return latch


def apply_request(root: Path, ident: Identity, req: dict, *, now_ms: int | None = None) -> dict:
    """Apply one drain mutation under the account lock. Exact replay returns the stored receipt."""
    if set(req) != REQUEST_FIELDS or req['version'] != 1 or req['op'] not in OPS:
        raise DrainRefused('unknown-state', 'request fields')
    if (req['iid'], req['boot'], req['node_epoch']) != (ident.iid, ident.boot, ident.node_epoch):
        raise DrainRefused('identity-mismatch', 'request binding')
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if not (req['issued_at_ms'] <= now_ms + 2000 and now_ms <= req['not_after_ms']
            and req['not_after_ms'] - req['issued_at_ms'] <= MUTATION_DEADLINE_S * 1000):
        raise DrainRefused('unknown-state', 'request expired')
    digest = request_hash(req)
    d = _state_root(root)
    with account_lock(root):
        latch = read_latch(root, ident)
        last = latch.get('last')
        if last and last['request_id'] == req['request_id']:
            if last['hash'] != digest:
                raise DrainRefused('identity-mismatch', 'request id reused with different bytes')
            return last['receipt']
        phase, gen, att = latch['phase'], latch['generation'], latch['attempt']
        if req['generation'] < gen:
            raise DrainRefused('identity-mismatch', 'lower generation')
        if req['generation'] == gen:
            if req['attempt'] != att:
                raise DrainRefused('identity-mismatch', 'attempt')
            new = {('establish', 'draining'): 'draining', ('cancel', 'draining'): 'cancelled',
                   ('seal', 'draining'): 'sealed', ('seal', 'sealed'): 'sealed'}.get((req['op'], phase))
        else:
            if phase not in ('open', 'cancelled') or req['op'] != 'establish':
                raise DrainRefused('draining' if phase == 'draining' else 'sealed', 'active attempt differs')
            new, gen, att = 'draining', req['generation'], req['attempt']
        if new is None:
            raise DrainRefused('identity-mismatch', f'illegal {req["op"]} from {phase}')
        receipt = {'phase': new, 'generation': gen, 'attempt': att, 'request_id': req['request_id'], 'hash': digest}
        _write_atomic(d / LATCH_FILE, _latch_doc(ident, new, gen, att,
                                                 {'request_id': req['request_id'], 'hash': digest, 'receipt': receipt}))
        return receipt


class WorkLease:
    """A flocked close-only lease file. Closing never unlinks or unlocks."""

    def __init__(self, fd: int, path: Path, owned: bool = True, parent: WorkLease | None = None):
        self.fd, self.path, self.owned, self.parent = fd, path, owned, parent

    def close(self) -> None:
        if self.fd is not None and self.owned:
            os.close(self.fd)
        self.fd = None

    def pass_fds(self) -> tuple:
        """fds for subprocess pass_fds: the child keeps the same open file description (and flock),
        plus the held parent so deeper hermes descendants stay admitted while draining."""
        fds = [f for f in (self.fd, self.parent.fd if self.parent else None) if f is not None]
        for f in fds:
            os.set_inheritable(f, True)
        return tuple(fds)

    def child_env(self, env: dict | None = None) -> dict:
        out = dict(os.environ if env is None else env)
        anchor = self.parent if self.parent is not None and self.parent.fd is not None else self
        if anchor.fd is not None:
            out[LEASE_FD_ENV] = str(anchor.fd)
        return out


def lease_busy(path: Path) -> bool:
    """Busy only if an independent fresh flock fails (metadata existence is not busy)."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def busy_leases(root: Path) -> int:
    d = _state_root(root) / LEASE_DIR
    try:
        return sum(1 for p in d.iterdir() if p.name.endswith('.lease') and lease_busy(p))
    except OSError as exc:
        raise DrainRefused('unknown-state', 'lease dir unreadable') from exc


def acquire_lease(root: Path, ident: Identity, purpose: str, *, parent: WorkLease | None = None) -> WorkLease:
    """Admit independent work: refuses unless phase is open/cancelled. A held parent lease lets
    descendants through while draining (never when sealed). Returns the flocked lease."""
    d = _state_root(root)
    with account_lock(root):
        phase = read_latch(root, ident)['phase']
        if phase == 'sealed':
            raise DrainRefused('sealed')
        if phase == 'draining' and parent is None:
            raise DrainRefused('draining')
        ld = d / LEASE_DIR
        name = uuid.uuid4().hex + '.lease'
        fd = os.open(ld / name, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, _canon({'version': 1, 'boot': ident.boot, 'pid': os.getpid(), 'purpose': purpose}))
        os.fsync(fd)
        return WorkLease(fd, ld / name, parent=parent)


def seal_precheck(root: Path, ident: Identity) -> bool:
    """True when no lease is busy; callers then request op=seal. Positive leases keep the latch draining."""
    with account_lock(root):
        read_latch(root, ident)
        return busy_leases(root) == 0


# ---------------------------------------------------------------------------
# Activation-gated hook API used by every admission/spawn seam.
# ---------------------------------------------------------------------------
LEASE_FD_ENV = 'HERMES_WORKER_DRAIN_LEASE_FD'
_IMDS = 'http://169.254.169.254'
_iid_cache: dict = {}


def read_imds_iid(timeout: float = 1.5) -> str:
    """Instance id from IMDSv2; any failure is unknown-state (never guessed)."""
    import urllib.request
    try:
        tok = urllib.request.urlopen(urllib.request.Request(
            _IMDS + '/latest/api/token', method='PUT',
            headers={'X-aws-ec2-metadata-token-ttl-seconds': '60'}), timeout=timeout).read().decode()
        iid = urllib.request.urlopen(urllib.request.Request(
            _IMDS + '/latest/meta-data/instance-id',
            headers={'X-aws-ec2-metadata-token': tok}), timeout=timeout).read().decode().strip()
    except Exception as exc:  # any transport failure holds
        raise DrainRefused('unknown-state', f'imds unavailable: {type(exc).__name__}') from exc
    if not IID_RE.match(iid):
        raise DrainRefused('unknown-state', 'imds iid malformed')
    return iid


def current_identity(act: dict, *, imds=None) -> Identity:
    boot = read_boot_id()
    iid = _iid_cache.get(boot)
    if iid is None:
        iid = (imds or read_imds_iid)()
        _iid_cache.clear()
        _iid_cache[boot] = iid
    if iid != act['iid']:
        raise DrainRefused('identity-mismatch', 'activation bound to another instance')
    return Identity(iid=iid, boot=boot, node_epoch=act['node_epoch'])


def _state_base() -> Path:
    from hermes_cli.worker_compute_capacity import policy_root
    return policy_root()


def _inherited_parent(root: Path):
    raw = os.environ.get(LEASE_FD_ENV)
    if not raw or not raw.isdigit():
        return None
    fd = int(raw)
    try:
        real = Path(os.readlink(f'/proc/self/fd/{fd}'))
        same = os.path.samestat(os.fstat(fd), os.stat(real))
    except OSError:
        return None
    if real.parent != _state_root(root) / LEASE_DIR or not same:
        return None
    return WorkLease(fd, real, owned=False)


def fenced(purpose: str):
    """Decorator: admit before the call, hold the lease for its duration, refuse loudly otherwise.
    A no-op wrapper when the node is not activated."""
    import functools

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            lease = admit(purpose)
            try:
                return fn(*args, **kwargs)
            finally:
                if lease is not None:
                    lease.close()
        wrapper.__drain_fenced__ = purpose  # type: ignore[attr-defined]
        return wrapper
    return deco


def admit(purpose: str, *, activation_path: Path | None = None, imds=None):
    """Gate one admission/spawn. None when enforcement is not activated (full no-op).

    Raises DrainRefused when latched, sealed, or state is unknown. The returned lease keeps the
    node non-idle while held; for detached children pass ``lease.pass_fds()`` and
    ``lease.child_env()`` to the spawn so the flock outlives this process, then ``close()``.
    """
    act = load_activation(ACTIVATION_PATH if activation_path is None else activation_path)
    if act is None:
        return None
    root = _state_base()
    ident = current_identity(act, imds=imds)
    return acquire_lease(root, ident, purpose, parent=_inherited_parent(root))

