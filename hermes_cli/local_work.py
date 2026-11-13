"""Process-local physical work ledger. Stdlib only; no runtime coverage claim.

Homes are ownership keys supplied by the caller; the registry canonicalizes every one
(``canonical_home``: the same expanduser/realpath/normcase form as
``hermes_constants.hermes_home_key``), so a symlinked or relative spelling of a member home
is the member, never a sticky ``unknown-home``. This foundation does not discover profiles,
attest code or establish readiness: membership is seeded by the process profile reservation
(``gateway.runtime_ownership.process_ownership``).
A directly constructed Registry is useful for isolated consumers/tests; installing
one at bootstrap is a separate integration responsibility.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import os
import threading
from types import MappingProxyType
import weakref


_HOME_KEYS = {}
_HOME_KEYS_MAX = 256


def canonical_home(home):
    """Canonical membership key for ``home`` (None stays None: an unbound home).

    Mirrors ``hermes_constants.hermes_home_key`` without importing it (stdlib-only seed module):
    expanduser + realpath + normcase. Only an existing directory's answer is cached, because a
    path that does not exist yet can resolve differently once created. The cache is keyed on the
    absolute spelling, so a relative spelling is resolved against the current directory every time.
    That spelling is joined onto the cwd, never ``abspath``-normalized: normpath collapses
    ``link/..`` as text, while realpath follows the link first.
    """
    if home is None:
        return None
    spelling = os.path.expanduser(os.fspath(home))
    if os.path.isabs(spelling):
        pass
    elif isinstance(spelling, bytes):
        spelling = os.path.join(os.getcwdb(), spelling)
    else:
        spelling = os.path.join(os.getcwd(), spelling)
    cached = _HOME_KEYS.get(spelling)
    if cached is not None:
        return cached
    key = os.path.normcase(os.path.realpath(spelling))
    if os.path.isdir(key):
        if len(_HOME_KEYS) >= _HOME_KEYS_MAX:
            _HOME_KEYS.clear()
        _HOME_KEYS[spelling] = key
    return key


ACTIVE_SCOPE: ContextVar["WorkScope | None"] = ContextVar(
    "local_work_active_scope", default=None,
)


class WorkScope:
    """Captured ownership, independent of any reservation's physical lifetime."""

    __slots__ = ("home", "kind", "registry", "source")

    def __init__(self, registry, kind, home, source):
        self.registry = registry
        self.kind = kind
        self.home = home
        self.source = source


class Snapshot:
    """Detached read: mutating a returned object cannot change the ledger."""

    __slots__ = (
        "active",
        "code_generation",
        "components",
        "coverage_generation",
        "epoch",
        "membership",
        "membership_generation",
        "pending_transitions",
        "per_home",
        "per_kind",
        "pid",
        "unknowns",
    )

    def __init__(self, epoch, active, per_kind, unknowns, pid, *, per_home=None,
                 membership=None, membership_generation=0, coverage_generation=0,
                 pending_transitions=0, components=None, code_generation=None):
        self.epoch = epoch
        self.active = active
        self.per_kind = MappingProxyType(per_kind)
        self.unknowns = unknowns
        self.pid = pid
        self.per_home = MappingProxyType(dict(per_home or {}))
        self.membership = MappingProxyType(dict(membership or {}))
        self.membership_generation = membership_generation
        self.coverage_generation = coverage_generation
        self.pending_transitions = pending_transitions
        self.components = MappingProxyType(dict(components or {}))
        # None: no attestation source wired (unknown identity, not "clean").
        self.code_generation = code_generation


class MembershipToken:
    """Opaque custody of one in-flight home transition (identity-only)."""

    __slots__ = ("home", "role", "transition")

    def __init__(self, home, role, transition):
        self.home = home
        self.role = role
        self.transition = transition


MEMBERSHIP_STATES = frozenset({"starting", "ready", "retiring", "cron-only", "parked", "removed"})


class Registry:
    """Continuous ledger with sticky uncertainty and identity-only finish tokens.

    Unknown homes still acquire tokens: losing ownership cannot make physical
    work disappear. There is deliberately no episode reset or clear_unknown API.
    """

    def __init__(self, *, homes=()):
        # home -> (state, roles). Seeds are ready launch homes.
        self._members = {canonical_home(h): ("ready", frozenset({"launch"})) for h in homes}
        self._retired = set()
        self._transitions = {}
        self._membership_generation = 0
        self._coverage_generation = 0
        self._components = {}
        self._code_source = None
        self._pid = os.getpid()
        self._lock = threading.Lock()
        self._tokens = {}
        self._epoch = 0
        self._unknowns = set()
        if hasattr(os, "register_at_fork"):
            reference = weakref.ref(self)

            def after_fork():
                registry = reference()
                if registry is not None:
                    registry._after_fork()

            os.register_at_fork(after_in_child=after_fork)

    def _after_fork(self):
        # The vanished parent thread may have held the old lock at fork.
        self._lock = threading.Lock()
        self._pid = os.getpid()
        self._tokens = {}
        self._epoch += 1
        self._unknowns = self._unknowns | {"fork-inherited-registry"}
        ACTIVE_SCOPE.set(None)

    def mark_unknown(self, reason):
        """Latch a diagnostic; a new uncertainty is an observable transition."""
        with self._lock:
            if reason not in self._unknowns:
                self._unknowns.add(reason)
                self._epoch += 1

    def reserve(self, kind, *, home, source):
        if not isinstance(kind, str) or not kind:
            raise ValueError("kind must be a nonempty string")
        if not isinstance(source, str) or not source:
            raise ValueError("source must be a nonempty string")
        home = canonical_home(home)
        token = object()
        with self._lock:
            if home not in self._members:
                # Never map an unowned/retired home onto the default: invalidate.
                reason = "retired-home-reentry" if home in self._retired else "unknown-home"
                self._unknowns.add(reason)
                self._membership_generation += 1
            self._tokens[id(token)] = (token, kind, home, source)
            self._epoch += 1
        return token

    def finish(self, token):
        """Release exactly once; foreign/duplicate tokens fail closed."""
        with self._lock:
            record = self._tokens.get(id(token))
            if record is None or record[0] is not token:
                if "unmatched-finish" not in self._unknowns:
                    self._unknowns.add("unmatched-finish")
                    self._epoch += 1
                return False
            del self._tokens[id(token)]
            self._epoch += 1
            return True

    def membership_begin(self, home, *, role, transition):
        """Begin a home transition BEFORE its externally visible effects.

        transition: "add" / "replace" (-> starting) or "retire" (-> retiring).
        Bumps membership generation and epoch; any open token makes snapshots
        report pending_transitions>0 (observers refuse).
        """
        if transition not in ("add", "retire", "replace"):
            raise ValueError("unknown membership transition")
        if not isinstance(role, str) or not role:
            raise ValueError("role must be a nonempty string")
        home = canonical_home(home)
        token = MembershipToken(home, role, transition)
        with self._lock:
            roles = self._members.get(home, (None, frozenset()))[1]
            if transition == "retire":
                if home not in self._members:
                    self._unknowns.add("retire-unknown-home")
                state = "retiring"
            else:
                self._retired.discard(home)
                state = "starting"
            self._members[home] = (state, roles | {role})
            self._transitions[id(token)] = token
            self._membership_generation += 1
            self._epoch += 1
        return token

    def membership_commit(self, token, *, state, coverage=True):
        """Finish a transition. state="removed" retires the home for good: later
        reservations for it invalidate; live tokens are never forgotten.
        coverage=False latches unknown (e.g. store open failure)."""
        if state not in MEMBERSHIP_STATES or state == "starting":
            raise ValueError("invalid commit state")
        with self._lock:
            if self._transitions.pop(id(token), None) is not token:
                self._unknowns.add("unmatched-membership-commit")
                self._membership_generation += 1
                self._epoch += 1
                return False
            home = token.home
            roles = self._members.get(home, (None, frozenset()))[1]
            if state == "removed":
                self._members.pop(home, None)
                self._retired.add(home)
            else:
                self._members[home] = (state, roles)
            if not coverage:
                self._unknowns.add("membership-coverage-incomplete")
                self._coverage_generation += 1
            self._membership_generation += 1
            self._epoch += 1
            return True

    def membership_generation(self):
        """Current membership generation (also on every ``Snapshot``); observers bracket reads with it."""
        with self._lock:
            return self._membership_generation

    def membership_snapshot(self):
        """Detached ``{home: (state, roles)}`` view of the served-home membership."""
        with self._lock:
            return {h: (m[0], tuple(sorted(m[1]))) for h, m in self._members.items()}

    def coverage_pending(self):
        """Reasons membership coverage is not established (observers refuse to start on any)."""
        with self._lock:
            reasons = []
            if not any(state == "ready" for state, _roles in self._members.values()):
                reasons.append("membership_unseeded")
            if self._transitions:
                reasons.append("membership_transition_pending")
            return reasons

    def attest_component(self, component, *, implementation_id, runtime_instance,
                         capabilities=()):
        """Record a concrete component identity; a change bumps coverage generation."""
        value = (implementation_id, runtime_instance, tuple(sorted(capabilities)))
        with self._lock:
            if self._components.get(component) != value:
                self._components[component] = value
                self._coverage_generation += 1
                self._epoch += 1

    def set_code_attestation(self, source):
        """Optional zero-arg callable returning the loaded-code generation
        (e.g. hermes_cli.code_attestation.generation); called OUTSIDE the lock."""
        with self._lock:
            self._code_source = source
            self._coverage_generation += 1

    def snapshot(self):
        with self._lock:
            counts = {}
            per_home = {}
            for _, kind, home, _ in self._tokens.values():
                counts[kind] = counts.get(kind, 0) + 1
                per_home[home] = per_home.get(home, 0) + 1
            extra = dict(
                per_home=per_home,
                membership={h: (m[0], tuple(sorted(m[1]))) for h, m in self._members.items()},
                membership_generation=self._membership_generation,
                coverage_generation=self._coverage_generation,
                pending_transitions=len(self._transitions),
                components=self._components,
            )
            head = [self._epoch, len(self._tokens), counts, set(self._unknowns), self._pid]
            source = self._code_source
        code_generation = None
        if source is not None:
            try:
                code_generation = source()
            except Exception:  # health: allow BLE001 -- an arbitrary code-source failure latches unknown (fail closed), never propagates
                self.mark_unknown("code-attestation-read-failed")
                head[3].add("code-attestation-read-failed")
                with self._lock:
                    head[0] = self._epoch
        head[3] = tuple(sorted(head[3]))
        return Snapshot(*head, code_generation=code_generation, **extra)

    @contextmanager
    def scope(self, kind, *, home, source):
        owner = WorkScope(self, kind, home, source)
        reservation = self.reserve(kind, home=home, source=source)
        context_token = ACTIVE_SCOPE.set(owner)
        try:
            yield owner
        finally:
            ACTIVE_SCOPE.reset(context_token)
            self.finish(reservation)


_registry = None


def install(*, homes=()):
    """Install once at the caller's bootstrap boundary, not lazily on first work.

    This alone does not attest bootstrap provenance or gateway readiness.
    """
    global _registry
    if _registry is None:
        _registry = Registry(homes=homes)
    return _registry


def get_registry():
    if _registry is None:
        raise RuntimeError("local work registry not installed")
    return _registry
