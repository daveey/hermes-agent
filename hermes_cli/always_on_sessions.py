"""Native sessions promotion policy for the stdlib-only always-on roller."""
from __future__ import annotations

LIST_PATH = "/api/profiles/sessions?limit=50&offset=0&order=recent&profile=all&min_messages=1&archived=exclude"
SIDEBAR_PATH = "/api/profiles/sessions/sidebar?recents_profile=all"
PROBE_S = .25
GAP_S = .05


def _rows(value):
    return isinstance(value, list) and all(isinstance(row, dict) and isinstance(row.get("id"), str)
                                          and bool(row["id"]) for row in value)


def complete(body, *, sidebar):
    """Only canonical, complete envelopes qualify; legitimate empty stores qualify too.

    No database reads, alternate proof endpoint or reduced serializer. HTTP 200 alone
    does not distinguish a failed/partial profile scan from an empty database.
    """
    if not isinstance(body, dict) or body.get("errors") != [] or body.get("storage") != {}:
        return False
    if any(body.get(key) for key in ("failed", "retry", "profiles_failed", "incomplete", "error")):
        return False
    if sidebar:
        for key in ("recents", "cron", "messaging"):
            part = body.get(key)
            if not isinstance(part, dict) or not _rows(part.get("sessions")):
                return False
            if any(part.get(k) for k in ("failed", "retry", "errors", "profiles_failed", "incomplete", "error")):
                return False
        return True
    totals = body.get("profile_totals")
    return (_rows(body.get("sessions")) and type(body.get("total")) is int and body["total"] >= 0
            and isinstance(totals, dict) and all(type(n) is int and n >= 0 for n in totals.values())
            and body["total"] == sum(totals.values()) and body.get("limit") == 50 and body.get("offset") == 0)


RETRY_S = 10.0  # per-request allowance once a fast probe missed; the roll deadline still bounds every wait


class _Pending(Exception):
    """The successor has not answered yet (timeout, refused connection, 5xx, no unit PID): ask again."""


def _incarnation_once(roller, port, deadline, timeout):
    left = deadline - roller.clock()
    pid = roller.unit_pid(port, timeout=min(timeout, left)) if left > 0 else 0
    if pid <= 0:
        left = deadline - roller.clock()
        state = roller._unit_state(port, min(timeout, left)) if left > 0 else None
        if state not in (None, "active", "activating", "reloading"):
            return None  # the unit is gone (inactive/failed): nothing to wait for
        raise _Pending(f"unit pid, state {state}")
    bodies = []
    for path in ("/api/host/identity", "/api/ssh/ownership"):
        left = deadline - roller.clock()
        if left <= 0:
            return None
        status, _, body = roller.get(port, path, min(timeout, left))
        if status is None or status >= 500:
            raise _Pending(f"{path} {status}")
        if status != 200 or not body or body.get("ok") is not True:
            return None  # an answer that refuses (401/403/404, not ok) is definitive
        bodies.append(body)
    host, custody = bodies
    nonce = custody.get("sshOwnerNonce")
    if (host.get("pid") != pid or not isinstance(nonce, str) or not nonce
            or custody.get("runtimeIntact") is not True):
        return None
    return pid, nonce


def incarnation(roller, port, deadline, *, log=None):
    """Bind canonical reads to the same unit PID and authenticated launch nonce.

    A successor that has not answered yet (its loop is busy right after the listener handoff, or
    systemctl is slow under boot load) is asked again until *deadline*; only an answer that names a
    different process, nonce or refused credential fails at once. One missed 0.25 s probe used to
    abandon a healthy natural successor (run64 canary, 2026-11-27 23:36:50Z).
    """
    timeout, started, seen = PROBE_S, roller.clock(), set()
    while roller.clock() < deadline:
        try:
            return _incarnation_once(roller, port, deadline, timeout)
        except _Pending as pending:
            reason = str(pending)
            if log is not None and reason not in seen:
                seen.add(reason)
                log(f"roll: backend :{port} incarnation pending ({reason}) at +{roller.clock() - started:.2f}s")
            timeout = RETRY_S
            roller.sleep(max(0, min(GAP_S, deadline - roller.clock())))
    return None


def await_sessions(roller, port, deadline):
    pending = {LIST_PATH: False, SIDEBAR_PATH: True}
    timeouts = dict.fromkeys(pending, PROBE_S)
    while pending and roller.clock() < deadline:
        for path, sidebar in list(pending.items()):
            left = deadline - roller.clock()
            if left <= 0:
                break
            status, _elapsed, body = roller.get(port, path, min(timeouts[path], left))
            if status == 200 and complete(body, sidebar=sidebar):
                del pending[path]
            elif status is None:
                # A complete real-store envelope can take longer than one fast probe (471 KB list,
                # 0.23-0.57 s measured): give the next read the full allowance instead of
                # discarding every answer that is still being produced.
                timeouts[path] = RETRY_S
        if pending:
            roller.sleep(max(0, min(GAP_S, deadline - roller.clock())))
    return not pending and roller.clock() < deadline
