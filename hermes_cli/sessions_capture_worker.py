"""Private one-request worker; stdout contains only one bounded binary frame."""
from contextlib import redirect_stdout
import json
import os
import struct
import sys
import threading

MAX_REQUEST = 16 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
REFRESH_JOIN_S = .2


def preimport():
    """Reader imports, before the request: a standby child does them while it waits for stdin."""
    import hermes_cli.web_routers.profiles
    import hermes_cli.web_json_response
    import hermes_cli.kanban_session_activity


def capture(name, selector):
    from contextlib import nullcontext
    import os
    from hermes_cli.kanban_annotation_refresh import capture_role

    # health: allow HX002 -- creator-defined rollback switch, never a credential
    role = nullcontext() if os.environ.get('HERMES_SESSIONS_CAPTURE_COPY_FIRST') == '0' else capture_role()
    with role:
        from hermes_cli.web_routers import profiles
        from hermes_cli.web_json_response import native_json_response
        readers = {'sessions': profiles.get_profiles_sessions,
                   'sessions_sidebar': profiles.get_profiles_sessions_sidebar.__wrapped__,
                   'sessions_stamps': profiles.get_profiles_session_stamps}
        # No native binding is installed in this fresh child. Keep the canonical
        # SQL/policy/ordering and all seven annotation families, bypassing sidebar TTL.
        return native_json_response(readers[name](**selector)).body


def main():
    incoming, outgoing = sys.stdin.buffer, sys.stdout.buffer
    baseline = _open_fds()
    # Libraries/plugins may print, at import too. Never permit such output to enter IPC framing.
    with redirect_stdout(sys.stderr):
        preimport()
    header = incoming.read(4)
    if len(header) != 4:
        return 2
    size = struct.unpack('!I', header)[0]
    if size > MAX_REQUEST:
        return 2
    request = incoming.read(size)
    if len(request) != size or incoming.read(1):
        return 2
    value = json.loads(request)
    with redirect_stdout(sys.stderr):
        if isinstance(value, dict) and set(value) == {'views'}:
            body = capture_many(value['views'])
        elif (isinstance(value, dict) and set(value) == {'name', 'selector'}
              and valid_view([value['name'], value['selector']])):
            body = capture(value['name'], value['selector'])
        else:
            return 2
    if len(body) > MAX_RESPONSE:
        return 3
    outgoing.write(struct.pack('!I', len(body)))
    outgoing.write(body)
    outgoing.flush()
    # Rollback: HERMES_SESSIONS_CAPTURE_FAST_EXIT=0 keeps the ordinary interpreter exit.
    # health: allow HX002 -- creator-defined rollback switch, never a credential
    if os.environ.get('HERMES_SESSIONS_CAPTURE_FAST_EXIT', '1').strip() != '0' and released(baseline):
        os._exit(0)  # the parent's budget also covers this exit; interpreter teardown was ~0.4 s of it
    return 0


def valid_view(view):
    return (isinstance(view, (list, tuple)) and len(view) == 2
            and view[0] in ('sessions', 'sessions_sidebar', 'sessions_stamps')
            and isinstance(view[1], dict))


def capture_many(views):
    if not isinstance(views, list) or not 1 <= len(views) <= 4 or not all(map(valid_view, views)):
        raise ValueError('invalid views')
    from hermes_cli.kanban_session_activity import one_annotation_snapshot
    results = []
    with one_annotation_snapshot():
        for name, selector in views:
            try:
                results.append({'body': json.loads(capture(name, selector))})
            except Exception:  # health: allow BLE001 -- one failed selector must not hide successful peers
                results.append({'unavailable': 'reader_error'})
    return json.dumps({'results': results}, separators=(',', ':'), allow_nan=False).encode()


def _open_fds():
    """Open descriptors of this process, or None when procfs cannot say (then nothing is verified)."""
    try:
        listed = [int(name) for name in os.listdir('/proc/self/fd')]
    except (OSError, ValueError):
        return None
    found = set()
    for fd in listed:
        try:
            os.fstat(fd)  # drops the listing's own, already closed, descriptor
        except OSError:
            continue
        found.add(fd)
    return found


def released(baseline):
    """Do, and verify, what interpreter exit would do for this worker; True only when all of it held.

    The frame is already flushed, so a False here costs only the ordinary (slower) exit.
    """
    try:
        return (baseline is not None and threading.current_thread() is threading.main_thread()
                and _cleaned() and _quiescent(baseline))
    except Exception:  # health: allow BLE001 -- any doubt keeps the ordinary exit; the frame is already out
        return False


def _cleaned():
    """Release what this worker holds, each step checked. In order:

    - join the daemon ``kanban-annotation-refresh`` thread (bounded): it borrows the readers closed
      below and would otherwise be cut off mid-read; still running means no early exit.
    - close the two ``ProfileSessionScanCache`` observers of ``web_routers.profiles`` under their
      own locks: read-only, autocommit connections that never hold a transaction.
    - close the retained kanban readers (``kanban_waiting_reader`` and ``kanban_reader_retention``,
      i.e. ``cached_ro``) with the very functions their atexit hooks run; one that stays borrowed
      stays in its cache, which is checked empty.
    - run ``hermes_profile_resources``'s own exit hook (releases drained diagnostic owners).
    - stop and join the code-attestation settler when one runs (its atexit hook, same function).
    - release the dependency-generation lease ``hermes_bootstrap`` took (its atexit hook), so no
      unlocked lease file is left for the next pruner.
    - ``logging.shutdown()`` and flush stdout/stderr, as exit would.
    """
    for thread in threading.enumerate():
        if thread.name == 'kanban-annotation-refresh':
            thread.join(REFRESH_JOIN_S)
            if thread.is_alive():
                return False
    loaded = sys.modules.get
    profiles = loaded('hermes_cli.web_routers.profiles')
    for lock, cache in () if profiles is None else (
            (profiles._PROFILE_SESSION_SCAN_LOCK, profiles._PROFILE_SESSION_SCAN_CACHE),
            (profiles._PROJECT_TREE_SCAN_LOCK, profiles._PROJECT_TREE_SCAN_CACHE)):
        if not lock.acquire(blocking=False):
            return False
        try:
            cache.clear()
        finally:
            lock.release()
        if cache._profiles:
            return False
    waiting, activity = loaded('hermes_cli.kanban_waiting_reader'), loaded('hermes_cli.kanban_session_activity')
    if waiting is not None:
        waiting.close_all()
    if activity is not None:
        from hermes_cli import kanban_reader_retention
        kanban_reader_retention.close_all()
    if getattr(waiting, '_cache', None) or getattr(activity, '_RO_CACHE', None):
        return False
    resources = loaded('hermes_profile_resources')
    if resources is not None:
        resources._cleanup_drained_at_exit()
    attestation = getattr(loaded('hermes_cli.code_attestation'), '_installed', None)
    if attestation is not None and attestation._settler is not None:
        attestation._stop_settling()
        if attestation._settler is not None and attestation._settler.is_alive():
            return False
    state = loaded('hermes_cli.runtime_state')
    if state is not None:
        state.release_process_leases()
        if state._process_leases:
            return False
    import logging
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    return True


def _quiescent(baseline):
    """Nothing else may be pending: no live non-daemon thread but this one, no exit-time
    ``weakref.finalize``, and no descriptor opened since ``baseline`` other than the raw read-only
    probe descriptors ``sqlite_safe_read``/``hermes_state_dbfile`` never close by design (they hold
    no SQLite state; closing one would drop SQLite's POSIX locks). A SQLite connection that
    ``_cleaned`` does not know about fails this descriptor check."""
    import weakref
    if any(thread is not threading.current_thread() and not thread.daemon and thread.is_alive()
           for thread in threading.enumerate()):
        return False
    if any(info.atexit for info in list(vars(weakref.finalize)['_registry'].values())):
        return False
    probes = set()
    safe, dbfile = sys.modules.get('hermes_cli.sqlite_safe_read'), sys.modules.get('hermes_state_dbfile')
    if safe is not None:
        probes.update(entry[0] for entry in safe._PREAD_FDS.values())
        probes.update(safe._RETIRED_PREAD_FDS)
    if dbfile is not None:
        probes.update(entry[0] for entry in dbfile._HEADER_PROBE_FDS.values())
        probes.update(dbfile._RETIRED_HEADER_PROBE_FDS)
    after = _open_fds()
    return after is not None and after <= baseline | probes


if __name__ == '__main__':
    raise SystemExit(main())
