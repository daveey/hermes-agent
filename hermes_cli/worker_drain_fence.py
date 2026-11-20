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
from contextvars import ContextVar
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
    uid: int | None = None  # worker account uid from the activation; a root process also trusts it


ACTIVATION_OWNERS: tuple = (0,)  # who may own the activation file (tests rebind)


def _trusted(uid: int | None = None) -> set:
    return {0, os.geteuid(), *(() if uid is None else (uid,))}  # windows-footgun: ok — POSIX-only module


def _check_stat(st, what: str, owners: set, *, directory: bool = False) -> None:
    import stat as _stat
    kind = _stat.S_ISDIR(st.st_mode) if directory else (_stat.S_ISREG(st.st_mode) and st.st_nlink == 1)
    if not kind or st.st_uid not in owners or st.st_mode & 0o022:
        raise DrainRefused('unknown-state', f'{what} untrusted owner/mode/type')


def _secure_read(path: Path, owners: set) -> bytes:
    """Read a state file through O_NOFOLLOW; reject non-regular, hardlinked, foreign-owner or
    group/world-writable files (checked on the opened descriptor, not the path)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise DrainRefused('unknown-state', f'{Path(path).name} unreadable: {type(exc).__name__}') from exc
    try:
        _check_stat(os.fstat(fd), Path(path).name, owners)
        return os.read(fd, 1 << 20)
    finally:
        os.close(fd)


def _check_dir(d: Path, owners: set) -> None:
    try:
        _check_stat(os.lstat(d), d.name, owners, directory=True)  # lstat: a symlinked dir is refused
    except OSError as exc:
        raise DrainRefused('unknown-state', f'{d.name} unreadable: {type(exc).__name__}') from exc


def read_boot_id() -> str:
    return Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()


def request_hash(req: dict) -> str:
    return hashlib.sha256(_canon(req)).hexdigest()


def load_activation(path: Path = ACTIVATION_PATH) -> dict | None:
    """None when absent (hooks no-op). Raises DrainRefused('unknown-state') when present but invalid."""
    try:
        raw = _secure_read(Path(path), set(ACTIVATION_OWNERS))
    except FileNotFoundError:
        return None
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
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(d / LOCK_FILE, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise DrainRefused('unknown-state', f'admission lock unavailable: {type(exc).__name__}') from exc
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


def _read_json(path: Path, owners: set | None = None) -> dict:
    try:
        doc = json.loads(_secure_read(path, _trusted() if owners is None else owners))
    except (OSError, ValueError) as exc:
        raise DrainRefused('unknown-state', f'{path.name} unreadable') from exc
    if not isinstance(doc, dict) or doc.get('version') != 1:
        raise DrainRefused('unknown-state', f'{path.name} malformed')
    return doc


def install_state(root: Path, ident: Identity) -> None:
    """Installer only: immutable install binding (O_EXCL) plus an open latch at generation 0."""
    d = _state_root(root)
    with account_lock(root):
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        (d / LEASE_DIR).mkdir(mode=0o700, exist_ok=True)
        binding = {'version': 1, 'iid': ident.iid, 'node_epoch': ident.node_epoch}
        try:
            fd = os.open(d / INSTALL_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            if _read_json(d / INSTALL_FILE, _trusted(ident.uid)) != binding:
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
    owners = _trusted(ident.uid)
    _check_dir(d, owners)
    install = _read_json(d / INSTALL_FILE, owners)
    latch = _read_json(d / LATCH_FILE, owners)
    if install.get('iid') != ident.iid or install.get('node_epoch') != ident.node_epoch:
        raise DrainRefused('identity-mismatch', 'install binding')
    if latch.get('iid') != ident.iid or latch.get('node_epoch') != ident.node_epoch:
        raise DrainRefused('identity-mismatch', 'latch binding')
    if latch.get('boot') != ident.boot:
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
    remaining = max(0.0, (req['not_after_ms'] - now_ms) / 1000)
    started = time.monotonic()
    with account_lock(root, timeout=min(2.0, remaining)):
        # Lock waits and slow disks spend the request's own deadline: the monotonic elapsed time is
        # added to the supplied clock, so a stalled holder or a stepped wall clock cannot extend it.
        if (now_ms + int((time.monotonic() - started) * 1000)) > req['not_after_ms']:
            raise DrainRefused('unknown-state', 'request expired waiting for lock')
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
        if new == 'sealed' and busy_leases(root):
            raise DrainRefused('draining', 'live work leases prevent seal')
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
        return tuple(fds)

    def child_env(self, env: dict | None = None) -> dict:
        if env is None:
            from tools.environments.local import build_subprocess_env
            env = build_subprocess_env(scrub_secrets=False, inherit_profile_home=False)
        out = dict(env)
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
        if phase == 'draining' and (parent is None or parent.fd is None
                or _parent_from_fd(root, parent.fd, boot=ident.boot) is None):
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
_authority: ContextVar = ContextVar('worker_drain_authority', default=None)


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
    return Identity(iid=iid, boot=boot, node_epoch=act['node_epoch'], uid=act['uid'])


def _state_base() -> Path:
    from hermes_cli.worker_compute_capacity import policy_root
    return policy_root()


def _activation_root(act: dict) -> Path:
    """State root for the activated account. A root-run process (sync, installer, core update) has
    root's home as its policy root, which is a different latch from the worker's; it must use the
    activated account's. Any other foreign uid is refused, never given an empty state."""
    me = os.geteuid()  # windows-footgun: ok — POSIX-only module
    if me == act['uid']:
        return _state_base()
    if me != 0:
        raise DrainRefused('identity-mismatch', 'process uid is not the activated account')
    return Path(act['home']) / '.hermes'


def _inherited_parent(root: Path):
    local = _authority.get()
    if local is not None and local.fd is not None:
        parent = _parent_from_fd(root, local.fd)
        if parent is not None:
            return parent
    raw = os.environ.get(LEASE_FD_ENV)
    if not raw or not raw.isdigit():
        return None
    return _parent_from_fd(root, int(raw))


def _parent_from_fd(root: Path, fd: int, *, boot=None):
    try:
        real = Path(os.readlink(f'/proc/self/fd/{fd}'))
        same = os.path.samestat(os.fstat(fd), os.stat(real))
        info = Path(f'/proc/self/fdinfo/{fd}').read_text(encoding='ascii')
        locked = any(re.search(r'\bFLOCK\s+ADVISORY\s+WRITE\b', line) for line in info.splitlines()
                     if line.startswith('lock:'))
        metadata = json.loads(os.pread(fd, 4096, 0))
        same_boot = metadata.get('boot') == (read_boot_id() if boot is None else boot)
    except (OSError, ValueError, AttributeError):
        return None
    if real.parent != _state_root(root) / LEASE_DIR or not same or not locked or not same_boot:
        return None
    return WorkLease(fd, real, owned=False)


@contextmanager
def lease_scope(lease):
    """Carry a retained, live lease through synchronous/context-copied work.

    The owner must hold it until the callback finishes. A copied context is not
    authority after close: admit revalidates the live descriptor/kernel lock.
    """
    token = _authority.set(lease)
    try:
        yield
    finally:
        _authority.reset(token)


def popen_worker(popen, cmd, *, env=None, drain_purpose='kanban.worker', **kwargs):
    """Start a detached worker under an admission lease it keeps for life (inherited fd), so its own
    hermes descendants stay admitted while draining. Plain call when not activated."""
    lease = admit(drain_purpose)
    if lease is None:
        return popen(cmd, env=env, **kwargs)
    try:
        kwargs['pass_fds'] = tuple(dict.fromkeys((*kwargs.get('pass_fds', ()), *lease.pass_fds())))
        kwargs['close_fds'] = True
        return popen(cmd, env=lease.child_env(env), **kwargs)
    finally:
        lease.close()


def fenced(purpose: str):
    """Decorator: admit before the call, hold the lease for its duration, refuse loudly otherwise.
    A no-op wrapper when the node is not activated."""
    import functools

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            lease = admit(purpose)
            try:
                with lease_scope(lease):
                    return fn(*args, **kwargs)
            finally:
                if lease is not None:
                    lease.close()
        wrapper.__drain_fenced__ = purpose  # type: ignore[attr-defined]
        return wrapper
    return deco


def admitted_reservation(reserve, *args, **kwargs):
    """Attach a close-only drain lease to an updater reservation.

    Acquire before the updater account lock, never recursively inside it. The
    caller owns both lifetimes. Failure/update hold releases the drain lease.
    """
    lease = admit('admission.reservation')
    try:
        pending = reserve(*args, **kwargs)
        if pending is not None:
            pending._worker_drain_lease = lease
            lease = None
        return pending
    finally:
        if lease is not None:
            lease.close()


def admit(purpose: str, *, activation_path: Path | None = None, imds=None):
    """Gate one admission/spawn. None when enforcement is not activated (full no-op).

    Raises DrainRefused when latched, sealed, or state is unknown. The returned lease keeps the
    node non-idle while held; for detached children pass ``lease.pass_fds()`` and
    ``lease.child_env()`` to the spawn so the flock outlives this process, then ``close()``.
    """
    act = load_activation(ACTIVATION_PATH if activation_path is None else activation_path)
    if act is None:
        return None
    try:
        root = _activation_root(act)
        ident = current_identity(act, imds=imds)
        return acquire_lease(root, ident, purpose, parent=_inherited_parent(root))
    except OSError as exc:
        raise DrainRefused('unknown-state', f'admission unavailable: {type(exc).__name__}') from exc

