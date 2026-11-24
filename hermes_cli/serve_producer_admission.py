"""Fail-closed admission cuts and pre-import enrollment for supported launches.

Enrollment is positive launch-coverage evidence, NOT guarded readiness. Until a
broker establishes family custody, enrolled processes remain legacy conflicts.
A COMPLETE cut is scoped to one held gate acquisition; no persisted clean bit
can authorize a successor or boot. This module starts no external producers.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from hermes_cli.serve_producer_census import (
    MAX_PROCESSES, CensusUnavailable, Incarnation, LinuxProcessCensus, ProcessCensus,
    _bounded_names, _read_bytes, read_ledger_strict,
)
from hermes_cli.serve_producer_custody import CustodyConflict, FamilyCustody, _acquire_runtime

SUPPORTED_LAUNCHES = frozenset(('serve', 'dashboard', 'gateway', 'agent_startup',
                                'tui_gateway', 'gateway_spawner', 'plugin_host', 'producer_broker'))
# Hash actual import boundaries, not a version string or inherited environment bit.
LAUNCH_SOURCES = ('hermes_cli/main.py', 'gateway/run.py', 'tui_gateway/entry.py',
                  'hermes_cli/gateway_runtime_start.py', 'hermes_cli/plugin_host_child.py',
                  'hermes_cli/serve_producer_admission.py', 'hermes_cli/serve_producer_census.py',
                  'hermes_cli/serve_producer_process.py', 'hermes_cli/serve_producer_enrollment.py')
_MAX_PARTICIPANTS = 8192
_MAX_RECEIPTS = 8 * 1024 * 1024


@dataclass(frozen=True)
class LaunchReceipt:
    identity: Incarnation
    installation: str
    generation: str
    role: str
    observation: str
    namespace: str
    guarded: bool = False


def observation_digest(record) -> str:
    """Bind receipt to observed exec identity without persisting argv or environment."""
    return hashlib.sha256(json.dumps(asdict(record), sort_keys=True,
                                     ensure_ascii=True).encode('ascii')).hexdigest()


def launcher_generation(installation: Path) -> str:
    digest = hashlib.sha256()
    for relative in LAUNCH_SOURCES:
        digest.update(relative.encode('ascii') + b'\0')
        digest.update(_read_bytes(installation / relative, 1024 * 1024))
    return digest.hexdigest()


def _receipt(value) -> LaunchReceipt:
    if not isinstance(value, dict) or set(value) != {
            'identity', 'installation', 'generation', 'role', 'observation', 'namespace', 'guarded'}:
        raise CensusUnavailable('participant schema')
    identity = value['identity']
    if not isinstance(identity, dict) or set(identity) != {'boot_id', 'pid', 'start_ticks'}:
        raise CensusUnavailable('participant identity')
    if not isinstance(identity['boot_id'], str) or not identity['boot_id'] or any(
            type(identity[key]) is not int or identity[key] <= 0 for key in ('pid', 'start_ticks')):
        raise CensusUnavailable('participant birth')
    if (not isinstance(value['role'], str) or value['role'] not in SUPPORTED_LAUNCHES
            or value['guarded'] is not False):
        # No custody bootstrap is installed yet. A persisted bit cannot mint it.
        raise CensusUnavailable('unguarded participant required')
    namespace = value['namespace']
    if (not isinstance(namespace, str) or not namespace.startswith('pid:[')
            or not namespace.endswith(']') or not namespace[5:-1].isascii()
            or not namespace[5:-1].isdecimal()):
        raise CensusUnavailable('participant namespace')
    if not isinstance(value['installation'], str) or not Path(value['installation']).is_absolute():
        raise CensusUnavailable('participant installation')
    for key in ('generation', 'observation'):
        text = value[key]
        if not isinstance(text, str) or len(text) != 64 or any(c not in '0123456789abcdef' for c in text):
            raise CensusUnavailable('participant digest')
    return LaunchReceipt(Incarnation(**identity), **{key: item for key, item in value.items() if key != 'identity'})


def read_participants_strict(runtime: Path) -> tuple[LaunchReceipt, ...]:
    """Absent, unsafe or malformed enrollment is uncertainty, never an empty host."""
    path = runtime / 'participants.json'
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):  # windows-footgun: ok — Linux adapter
            raise CensusUnavailable('unsafe participant ledger')
        raw = _read_bytes(path, _MAX_RECEIPTS, nofollow=True)
        values = json.loads(raw)
        if not isinstance(values, list) or len(values) > _MAX_PARTICIPANTS:
            raise CensusUnavailable('participant ledger budget/schema')
        receipts = tuple(_receipt(value) for value in values)
        adapter = LinuxProcessCensus()
        boot, namespace = adapter.boot_id(), adapter.namespace_id()
        if any(row.identity.boot_id == boot and row.namespace != namespace for row in receipts):
            raise CensusUnavailable('foreign pid namespace')
        if len({receipt.identity for receipt in receipts}) != len(receipts):
            raise CensusUnavailable('duplicate participant incarnation')
        if _read_bytes(path, _MAX_RECEIPTS, nofollow=True) != raw:
            raise CensusUnavailable('participant ledger changed')
        return receipts
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise CensusUnavailable(f'participants:{type(exc).__name__}') from exc


def incarnation_exited(adapter: LinuxProcessCensus, identity: Incarnation) -> bool:
    """Positive retirement includes PID reuse, never permission failure or a new boot."""
    if adapter.boot_id() != identity.boot_id:
        raise CensusUnavailable('retirement boot changed')
    try:
        current = adapter.identity(identity.pid, identity.boot_id)
    except FileNotFoundError:
        current = None
    if adapter.boot_id() != identity.boot_id:
        raise CensusUnavailable('retirement boot changed')
    return current != identity


def _write_participants(runtime: Path, receipts):
    raw = json.dumps([asdict(receipt) for receipt in receipts], ensure_ascii=True).encode('ascii')
    if len(raw) > _MAX_RECEIPTS:
        raise CensusUnavailable('participant byte budget')
    # Random exclusive scratch names cannot collide with a killed predecessor's PID.
    fd, name = tempfile.mkstemp(prefix='participants.', suffix='.pending', dir=runtime)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, runtime / 'participants.json')
        directory = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _wait_gate(runtime: Path, deadline: float):
    waiter = threading.Event()
    while True:
        try:
            return LaunchGate.acquire(runtime)
        except CustodyConflict:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AdmissionUnavailable('launch enrollment busy') from None
            waiter.wait(min(remaining, 0.025))


def enroll_launch(runtime: Path, role: str, *, installation: Path, timeout: float = 10) -> LaunchReceipt:
    """Pre-import enrollment only: no guarded mode or COMPLETE claim is minted here.

    Existing receipts survive until their exact incarnation is positively gone.
    The ticket is intentionally unguarded until the separate broker bootstrap can
    establish family custody. Enrollment cannot authorize external production.
    """
    if role not in SUPPORTED_LAUNCHES:
        raise ValueError('unsupported launch role')
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
        raise ValueError('invalid launch timeout')
    installation = installation.resolve(strict=True)
    adapter = LinuxProcessCensus()
    with _wait_gate(runtime, time.monotonic() + timeout):
        path = runtime / 'participants.json'
        try:
            path.lstat()
        except FileNotFoundError:
            receipts = ()  # first enrollment, NEVER used as host absence evidence
        else:
            receipts = read_participants_strict(runtime)
        boot = adapter.boot_id()
        record = adapter.read_process(os.getpid(), boot)
        receipt = LaunchReceipt(record.identity, str(installation), launcher_generation(installation),
                                role, observation_digest(record), adapter.namespace_id())
        kept = [row for row in receipts if row.identity.boot_id == boot
                and row.identity != record.identity and not incarnation_exited(adapter, row.identity)]
        if len(kept) >= _MAX_PARTICIPANTS:
            raise CensusUnavailable('participant count budget')
        if adapter.read_process(os.getpid(), boot) != record or adapter.boot_id() != boot:
            raise CensusUnavailable('enrolling process changed')
        _write_participants(runtime, (*kept, receipt))
        if receipt not in read_participants_strict(runtime):
            raise CensusUnavailable('participant publication lost')
        return receipt


def supported_launch(role: str, *, timeout: float = 10) -> LaunchReceipt | None:
    """Supported entrypoint pre-import gate; unsupported hosts cannot certify COMPLETE.

    Non-Linux hosts retain existing in-process launches; no strict census adapter
    exists there, so they cannot activate the external producer path.
    """
    if sys.platform != 'linux':
        return None
    from hermes_constants import get_default_hermes_root
    from hermes_cli._startup_fast import project_root_str
    runtime = Path(get_default_hermes_root()) / 'serve-producer-v1'
    return enroll_launch(runtime, role, installation=Path(project_root_str()), timeout=timeout)


_KNOWN_MODULES = frozenset(('hermes_cli.main', 'gateway.run', 'tui_gateway.entry',
                            'hermes_cli.plugin_host_child'))
_CONTROL_FILES = ('gateway.pid', 'gateway.lock', 'gateway_state.json', 'gateway.sock',
                  'gateway.sock.path')
_HOST_RECORDS = ('host-serve.json', 'host-gateway.json')
_PROFILE_MARKERS = ('config.yaml', '.env', 'SOUL.md', 'profile.yaml', 'auth.json', 'state.db')


def _inside(value: str, root: str) -> bool:
    """Path-component containment; never a string-prefix or argv-substring test."""
    if not value or not root or not value.startswith('/') or not root.startswith('/'):
        return False
    left, right = Path(value), Path(root)
    return left == right or right in left.parents


# Environment variables whose value is an os.pathsep-separated search path, not one path.
_PATH_LIST_ENV = frozenset(('PYTHONPATH',))
_MAX_PATH_LIST_ENTRIES = 256
_MAX_PATH_BYTES = 4096


def _absolute(value: str, cwd: str) -> str | None:
    """Lexical absolute form; None when a relative value cannot be anchored at the cwd.

    A deleted/unreadable cwd leaves ``..`` unresolvable (it could climb anywhere).
    Without ``..`` the value lies under that cwd, which is tested separately.
    """
    if value.startswith('/'):
        return '/' + os.path.normpath(value).lstrip('/')
    if cwd.startswith('/') and not cwd.endswith(' (deleted)'):
        return '/' + os.path.normpath(os.path.join(cwd, value)).lstrip('/')
    return None if '..' in value.split('/') else ''


def _may_reach(value: str, cwd: str, roots: tuple[str, ...], *, follow_links: bool) -> bool:
    """True unless this one reference provably cannot name a path under any root.

    Relative references resolve against the OBSERVED cwd. Symlinks are followed
    for import search paths, where an indirection really changes what is imported;
    other references (argv words, flag values) are judged lexically, so a census
    does not stat arbitrary foreign paths.
    """
    absolute = _absolute(value, cwd)
    if absolute is None:
        return True
    if not absolute or len(absolute) > _MAX_PATH_BYTES:
        return False
    if any(_inside(absolute, root) for root in roots):
        return True
    if not follow_links:
        return False
    try:
        real = os.path.realpath(absolute)
    except (OSError, ValueError):
        return True
    return any(_inside(real, root) for root in roots)


def _record_touches(record, *roots: str) -> bool:
    """Fail toward 'touches': absence of access must be positively established."""
    anchors = []
    for root in roots:
        if root and root.startswith('/'):
            anchors.append('/' + os.path.normpath(root).lstrip('/'))
            try:
                anchors.append(os.path.realpath(root))
            except (OSError, ValueError):
                return True
    anchors = tuple(dict.fromkeys(anchors))
    if not anchors:
        return False
    cwd = record.cwd
    values = [(record.exe, False), (cwd, False)]
    for item in record.argv:
        values.append((item, False))
        if '=' in item:  # --flag=/path, KEY=/path
            values.append((item.partition('=')[2], False))
    for key, value in record.environment:
        if key in _PATH_LIST_ENV:
            entries = value.split(os.pathsep)
            if len(entries) > _MAX_PATH_LIST_ENTRIES:
                return True
            values.extend((entry, True) for entry in entries)
        else:
            values.append((value, False))
    return any(value and _may_reach(value, cwd, anchors, follow_links=links) for value, links in values)


def _module_token(argv: tuple[str, ...]) -> str | None:
    try:
        index = argv.index('-m')
    except ValueError:
        return None
    return argv[index + 1] if index + 1 < len(argv) else None


def classify_process(record, participants: tuple[LaunchReceipt, ...], generation: str,
                     *, installation: str, home: str = '') -> Classification:
    """Exact enrollment is unguarded legacy; unrelated needs a path-free identity."""
    namespace = LinuxProcessCensus().namespace_id()
    matching = [row for row in participants if row.identity == record.identity and row.namespace == namespace
                and row.installation == installation
                and row.observation == observation_digest(record) and row.generation == generation]
    if len(matching) == 1:
        receipt = matching[0]
        # No broker/custody hook exists yet, even an in-memory guarded bit is not authority.
        return Classification(record.identity, 'legacy',
                              f'enrollment:{receipt.generation}:{receipt.role}')
    if not _record_touches(record, installation, home):
        return Classification(record.identity, 'unrelated', f'exe:{record.exe}')
    module = _module_token(record.argv)
    if module in _KNOWN_MODULES:
        return Classification(record.identity, 'legacy', f'launcher:{module}')
    return Classification(record.identity, 'unknown', 'no exact guarded launch receipt')

# Each collector must enumerate its exact configured scope (including disabled or
# empty scope receipts), distinguish errors and return positive evidence refs.
REQUIRED_COLLECTORS = frozenset(('raw_ledger', 'profile_controls', 'supervisor_ports',
                                 'supported_launchers', 'installation_access'))


class AdmissionUnavailable(RuntimeError):
    """External production stays dormant while exclusion cannot be established."""


@dataclass(frozen=True)
class CollectorEvidence:
    refs: tuple[str, ...]
    conflicts: tuple[str, ...]


@dataclass(frozen=True)
class Classification:
    identity: Incarnation
    kind: str  # participant or positively unrelated; legacy/unknown are conflicts
    proof: str


@dataclass(frozen=True)
class AdmissionCut:
    boot_id: str
    generation: str
    installation: str
    census: ProcessCensus | None
    collectors: tuple[tuple[str, CollectorEvidence], ...]
    classifications: tuple[Classification, ...]
    legacy_candidates: tuple[Incarnation, ...]
    retired: tuple[Incarnation, ...]
    errors: tuple[str, ...]
    _acquisition: object

    @property
    def complete(self) -> bool:
        return not self.errors


class LaunchGate:
    """Stable nonblocking launch mutex; hold through registration+family admission.

    This gate alone does not fence pre-integration processes. Family custody must
    outlive this short launch critical section and all live producer callbacks.
    """

    def __init__(self, fd: int):
        self._custody = FamilyCustody(fd)
        self._pid = os.getpid()
        self._acquisition = object()

    @classmethod
    def acquire(cls, runtime: Path) -> LaunchGate:
        return cls(_acquire_runtime(runtime, 'admission'))

    def __enter__(self):
        self._held()
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        self._custody.close()

    def _held(self):
        if self._custody.fd is None or os.getpid() != self._pid:
            raise AdmissionUnavailable('launch gate must remain held by collecting process')

    def inspect(self, *, boot_id: str, generation: str, installation: str,
                census, collectors: dict, classify, legacy_candidates: tuple[Incarnation, ...],
                exited) -> AdmissionCut:
        """Run strict host observations inside the gate; never from an HTTP path.

        `exited` must affirm the exact candidate incarnation is gone, including
        spawners, not just absent from the current ledger. Exceptions are unknown.
        All adapters are mandatory: no best-effort inventory fallback exists.
        """
        self._held()
        errors, evidence, classified, retired = [], [], [], []
        if not all(isinstance(value, str) and value for value in (boot_id, generation, installation)):
            errors.append('identity:missing')
        for name in sorted(REQUIRED_COLLECTORS):
            _collect(name, collectors.get(name), evidence, errors)
        observation = _census(census, boot_id, errors)
        if observation is not None:
            for record in observation.processes:
                _classify(record, classify, classified, errors)
        for identity in legacy_candidates:
            try:
                if exited(identity) is not True:
                    errors.append(f'legacy:{identity.pid}:not_retired')
                else:
                    retired.append(identity)
            except (OSError, ValueError, CensusUnavailable) as exc:
                errors.append(f'legacy:{identity.pid}:{type(exc).__name__}')
        self._held()
        return AdmissionCut(boot_id, generation, installation, observation, tuple(evidence),
                            tuple(classified), tuple(legacy_candidates), tuple(retired),
                            tuple(errors), self._acquisition)

    def require(self, cut: AdmissionCut, *, boot_id: str, generation: str, installation: str):
        self._held()
        if cut._acquisition is not self._acquisition:
            raise AdmissionUnavailable('different launch-gate acquisition')
        if (cut.boot_id, cut.generation, cut.installation) != (boot_id, generation, installation):
            raise AdmissionUnavailable('admission identity changed')
        if not cut.complete:
            raise AdmissionUnavailable('incomplete producer-admission evidence')


def _collect(name, callback, evidence, errors):
    if callback is None:
        errors.append(f'{name}:missing')
        return
    try:
        result = callback()
        if not isinstance(result, CollectorEvidence) or not result.refs or not all(result.refs):
            errors.append(f'{name}:no_positive_evidence')
            return
        evidence.append((name, result))
        errors.extend(f'{name}:{conflict}' for conflict in result.conflicts)
    except (OSError, ValueError, CensusUnavailable) as exc:
        errors.append(f'{name}:{type(exc).__name__}')


def _census(callback, boot_id, errors):
    try:
        result = callback()
        if not isinstance(result, ProcessCensus):
            errors.append('processes:missing')
            return None
        errors.extend(f'processes:{error}' for error in result.errors)
        if result.boot_id != boot_id:
            errors.append('processes:boot_changed')
        return result
    except (OSError, ValueError, CensusUnavailable) as exc:
        errors.append(f'processes:{type(exc).__name__}')
        return None


def _classify(record, callback, classified, errors):
    try:
        result = callback(record)
        if not isinstance(result, Classification) or result.identity != record.identity:
            errors.append(f'pid:{record.identity.pid}:classification_identity')
            return
        classified.append(result)
        if result.kind not in ('participant', 'unrelated') or not result.proof:
            errors.append(f'pid:{record.identity.pid}:unproven_classification')
    except (OSError, ValueError, CensusUnavailable) as exc:
        errors.append(f'pid:{record.identity.pid}:{type(exc).__name__}')


def _regular_owned(path: Path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):  # windows-footgun: ok — Linux adapter
        raise CensusUnavailable(f'unsafe:{path.name}')
    return info


def configured_homes(root: Path) -> tuple[Path, ...]:
    """Default home plus identity-bearing named profiles; unlistable dirs fail closed."""
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():  # windows-footgun: ok — Linux adapter
        raise CensusUnavailable('home ownership boundary')
    homes = [root]
    try:
        names = _bounded_names(root / 'profiles', MAX_PROCESSES)
    except FileNotFoundError:
        return tuple(homes)
    except OSError as exc:
        raise CensusUnavailable(f'profiles:{type(exc).__name__}') from exc
    for name in names:
        if name.startswith('.'):
            continue
        home = root / 'profiles' / name
        try:
            info = home.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():  # windows-footgun: ok — Linux adapter
            raise CensusUnavailable(f'profile:{name}:ownership')
        identified = False
        for marker in _PROFILE_MARKERS:
            try:
                (home / marker).lstat()
            except FileNotFoundError:
                continue
            identified = True
            break
        if identified:
            homes.append(home)
    return tuple(homes)


def collect_raw_ledger(root: Path) -> CollectorEvidence:
    path = root / 'spawn-ledger.json'
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise CensusUnavailable('ledger missing') from exc
    if not stat.S_ISREG(info.st_mode):
        # Early refusal only; the opened descriptor is re-judged without blocking,
        # so a FIFO/device/symlink swapped in after this lstat cannot hold the gate.
        raise CensusUnavailable('unsafe:spawn-ledger.json')
    rows = read_ledger_strict(path)
    return CollectorEvidence((f'ledger:{path}:{len(rows)}',), ())


def collect_profile_controls(root: Path) -> CollectorEvidence:
    homes = configured_homes(root)
    refs, conflicts = [f'homes:{len(homes)}'], []
    for home in homes:
        label = 'default' if home == root else home.name
        for name in _CONTROL_FILES:
            path = home / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                refs.append(f'{label}/{name}:absent')
                continue
            except OSError as exc:
                raise CensusUnavailable(f'{label}/{name}:{type(exc).__name__}') from exc
            if stat.S_ISLNK(info.st_mode):
                conflicts.append(f'{label}/{name}:symlink')
                continue
            refs.append(f'{label}/{name}:{info.st_ino}')
    return CollectorEvidence(tuple(refs), tuple(conflicts))


def _lock_dir() -> Path:
    override = os.environ.get('HERMES_GATEWAY_LOCK_DIR', '').strip()
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise CensusUnavailable('relative lock dir')
        return path
    xdg = os.environ.get('XDG_STATE_HOME', '')
    state = Path(xdg) if os.path.isabs(xdg) else Path.home() / '.local' / 'state'
    return state / 'hermes' / 'gateway-locks'


def collect_supervisor_ports(root: Path) -> CollectorEvidence:
    refs, conflicts = [], []
    lock = _lock_dir()
    try:
        info = lock.lstat()
    except FileNotFoundError:
        refs.append('locks:absent')
    else:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():  # windows-footgun: ok — Linux adapter
            raise CensusUnavailable('lock dir ownership')
        refs.append(f'locks:{info.st_ino}')
        for name in _HOST_RECORDS:
            path = lock / name
            try:
                _regular_owned(path)
            except FileNotFoundError:
                refs.append(f'{name}:absent')
                continue
            raw = _read_bytes(path, 1024 * 1024, nofollow=True)
            if not isinstance(json.loads(raw), dict):
                raise CensusUnavailable(f'{name}:schema')
            refs.append(f'{name}:{hashlib.sha256(raw).hexdigest()}')
    for home in configured_homes(root):
        label = 'default' if home == root else home.name
        sock, pid_path = home / 'gateway.sock', home / 'gateway.pid'
        try:
            info = sock.lstat()
        except FileNotFoundError:
            refs.append(f'{label}/gateway.sock:absent')
            continue
        if not stat.S_ISSOCK(info.st_mode):
            conflicts.append(f'{label}/gateway.sock:not_socket')
            continue
        try:
            _regular_owned(pid_path)
            payload = json.loads(_read_bytes(pid_path, 1024 * 1024, nofollow=True))
            pid = payload.get('pid') if isinstance(payload, dict) else None
            if type(pid) is not int or pid <= 0:
                raise CensusUnavailable('gateway pid')
            LinuxProcessCensus().identity(pid, LinuxProcessCensus().boot_id())
            refs.append(f'{label}/gateway.sock:pid:{pid}')
        except FileNotFoundError:
            conflicts.append(f'{label}/gateway.sock:unresolved_owner')
        except (OSError, ValueError, CensusUnavailable) as exc:
            conflicts.append(f'{label}/gateway.sock:{type(exc).__name__}')
    return CollectorEvidence(tuple(refs), tuple(conflicts))


def collect_supported_launchers(installation: Path) -> CollectorEvidence:
    generation = launcher_generation(installation)
    refs = [f'generation:{generation}']
    for relative in LAUNCH_SOURCES:
        info = (installation / relative).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():  # windows-footgun: ok — Linux adapter
            raise CensusUnavailable(f'launcher:{relative}')
        refs.append(f'{relative}:{info.st_size}')
    return CollectorEvidence(tuple(refs), ())


def collect_installation_access(installation: Path) -> CollectorEvidence:
    info = installation.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():  # windows-footgun: ok — Linux adapter
        raise CensusUnavailable('installation ownership boundary')
    fd = os.open(installation, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    os.close(fd)
    return CollectorEvidence((f'install:{installation}:{info.st_ino}:{info.st_uid}',), ())


def host_collectors(root: Path, installation: Path) -> dict:
    return {
        'raw_ledger': lambda: collect_raw_ledger(root),
        'profile_controls': lambda: collect_profile_controls(root),
        'supervisor_ports': lambda: collect_supervisor_ports(root),
        'supported_launchers': lambda: collect_supported_launchers(installation),
        'installation_access': lambda: collect_installation_access(installation),
    }


def launcher_coverage(installation: Path) -> dict:
    evidence = collect_supported_launchers(installation)
    return {
        'status': 'COMPLETE',
        'generation': evidence.refs[0].split(':', 1)[1],
        'sources': list(LAUNCH_SOURCES),
        'roles': sorted(SUPPORTED_LAUNCHES),
        'refs': list(evidence.refs),
    }


def inspect_host(runtime: Path, installation: Path, *, root: Path, timeout: float = 10,
                  legacy_candidates: tuple[Incarnation, ...] = (), broker=None) -> AdmissionCut:
    """Gate-held strict cut. Enrollment remains unguarded; COMPLETE stays conservative."""
    if sys.platform != 'linux':
        raise AdmissionUnavailable('no linux census adapter')
    from hermes_cli.serve_producer_enrollment import classify_with_broker

    installation = installation.resolve(strict=True)
    root = Path(root)
    generation = launcher_generation(installation)
    adapter = LinuxProcessCensus()
    with _wait_gate(runtime, time.monotonic() + timeout) as gate:
        try:
            (runtime / 'participants.json').lstat()
        except FileNotFoundError:
            participants = ()
        else:
            participants = read_participants_strict(runtime)
        return gate.inspect(
            boot_id=adapter.boot_id(), generation=generation, installation=str(installation),
            census=adapter.collect,
            collectors=host_collectors(root, installation),
            classify=lambda record: classify_with_broker(
                record, participants, generation, installation=str(installation),
                runtime=runtime, broker=broker, home=str(root)),
            legacy_candidates=legacy_candidates, exited=lambda identity: incarnation_exited(adapter, identity),
        )
