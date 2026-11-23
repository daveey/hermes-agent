"""Strict, dynamically checked availability of the cross-revision capsule ABI."""
import threading
import time
from pathlib import Path

from hermes_cli.kanban_capsule_owner import CONTRACT_CAPABILITY
from hermes_cli._startup_fast import project_root_str

CORE_ROOT = Path(project_root_str())


def _verified_sha(*, worker=False, deadline=None):
    """Require the boot-installed ledger as well as a clean, unchanged disk.

    Never install a ledger here: late provenance cannot attest already loaded
    code. Unknowns are sticky and intentionally unfiltered, including deps.
    """
    from hermes_cli.code_attestation import integrity_observation
    from hermes_cli.git_fingerprint import capsule_code_sha
    try:
        if deadline is not None and time.monotonic() >= deadline:
            return None
        sha = capsule_code_sha(CORE_ROOT)
        if sha is None:
            return None
        observed = integrity_observation(deadline=deadline) if deadline is not None else integrity_observation()
        routes = ('managed-worker-stub',) if worker else (
            'managed-stub', 'managed-stub-relaunch', 'console-main-first')
        if (observed['core_root'] != str(CORE_ROOT.resolve())
                or observed['boot_sha'] != sha or observed['route'] not in routes
                or observed['unknowns']):
            return None
        # The inventory can itself span an update. Never cache a positive.
        return sha if capsule_code_sha(CORE_ROOT) == sha else None
    except (RuntimeError, OSError, KeyError, TypeError, ValueError):
        return None


# Advisory only: one in-flight verifier, no cached positives. A slow filesystem
# must withdraw v2 rather than consume the control client's entire timeout.
_IDENTIFY_VERIFY_BUDGET_S = 0.4  # Leave transport/scheduling headroom inside identify's 0.5s budget.
_advisory_lock = threading.Lock()


def _advisory_sha():
    deadline = time.monotonic() + _IDENTIFY_VERIFY_BUDGET_S
    if not _advisory_lock.acquire(blocking=False):
        return None
    done = threading.Event()
    result: list[str | None] = [None]

    def verify():
        try:
            result[0] = _verified_sha(deadline=deadline)
        except Exception:
            # This advisory must never take legacy identify down with it.
            import logging
            logging.getLogger(__name__).exception('Capsule identify verification failed')
            result[0] = None
        finally:
            _advisory_lock.release()
            done.set()

    try:
        from agent.memory_provider import spawn_context_thread
        spawn_context_thread(verify, name='capsule-identify', daemon=True).start()
    except RuntimeError:
        _advisory_lock.release()
        return None
    completed = done.wait(max(0.0, deadline - time.monotonic()))
    return result[0] if completed and time.monotonic() < deadline else None


def available_capabilities(capabilities, *, advertised_sha=None):
    """Withdraw v2 on unknown/dirty/skewed code; never manufacture an advertisement.

    Legacy transport remains unchanged. Unlike ordinary code-skew diagnostics,
    an unresolved identity and even a restart-free revision difference refuse v2.
    """
    result = list(capabilities)
    if CONTRACT_CAPABILITY in result:
        sha = _advisory_sha()
        if sha is None or advertised_sha != sha:
            result = [cap for cap in result if cap != CONTRACT_CAPABILITY]
    return result


def owner_launch_sha(required):
    """Non-consuming capsule prelaunch identity; ordinary launches are unchanged."""
    if not required:
        return None
    from hermes_state_runtime import RuntimeStoreError
    sha = _verified_sha()
    if sha is None:
        raise RuntimeStoreError('capsule_owner_code_unverified')
    return sha


WORKER_FLAG = '--capsule-contract-v2'
_worker_expected_sha = None


def worker_command(policy):
    """Owner-selected core and interpreter, never a submitter-supplied target."""
    import sys
    from gateway.session_kanban_capsule import check_capsule_transport
    if not check_capsule_transport(policy):
        return [sys.executable, '-m', 'agent.managed_worker']
    from hermes_cli._launch_template import render
    return [sys.executable, '-I', '-c', render(str(CORE_ROOT), 'agent.managed_worker', 'main')]


def install_worker_contract(argv):
    """Read only the owner's private exec argv, never policy/env/bootstrap data.

    Strip the expectation before the legacy observation-fd parser. An old child
    cannot understand this extra argv and therefore refuses instead of executing.
    """
    global _worker_expected_sha
    argv = list(argv)
    if WORKER_FLAG not in argv:
        if argv:
            raise RuntimeError('capsule_worker_code_unverified')
        return argv
    if (len(argv) != 4 or argv[0] != '--observation-fd' or argv[2] != WORKER_FLAG
            or _worker_expected_sha is not None or not argv[3]
            or _verified_sha(worker=True) != argv[3]):
        raise RuntimeError('capsule_worker_code_unverified')
    _worker_expected_sha = argv[3]
    return argv[:2]


def check_worker_identity():
    """Repeat at execution entry and before observation/model/tool initialization."""
    from agent.observation_bind import installed_fd
    if (_worker_expected_sha is None and installed_fd() is not None
            or _worker_expected_sha is not None and _verified_sha(worker=True) != _worker_expected_sha):
        raise RuntimeError('capsule_worker_code_unverified')
