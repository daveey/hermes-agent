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


def incarnation(roller, port, deadline):
    """Bind canonical reads to the same unit PID and authenticated launch nonce."""
    left = deadline - roller.clock()
    pid = roller.unit_pid(port, timeout=min(PROBE_S, left)) if left > 0 else 0
    if pid <= 0:
        return None
    bodies = []
    for path in ("/api/host/identity", "/api/ssh/ownership"):
        left = deadline - roller.clock()
        if left <= 0:
            return None
        status, _, body = roller.get(port, path, min(PROBE_S, left))
        if status != 200 or not body or body.get("ok") is not True:
            return None
        bodies.append(body)
    host, custody = bodies
    nonce = custody.get("sshOwnerNonce")
    if (host.get("pid") != pid or not isinstance(nonce, str) or not nonce
            or custody.get("runtimeIntact") is not True):
        return None
    return pid, nonce


def await_sessions(roller, port, deadline):
    pending = {LIST_PATH: False, SIDEBAR_PATH: True}
    while pending and roller.clock() < deadline:
        for path, sidebar in list(pending.items()):
            left = deadline - roller.clock()
            if left <= 0:
                break
            status, _elapsed, body = roller.get(port, path, min(PROBE_S, left))
            if status == 200 and complete(body, sidebar=sidebar):
                del pending[path]
        if pending:
            roller.sleep(max(0, min(GAP_S, deadline - roller.clock())))
    return not pending and roller.clock() < deadline
