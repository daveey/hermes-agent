"""Card-level state of work that runs on another system: light blue (running externally) or red (service broke). [daveey]

Pure and clock-injected: no database, no network, no ``time`` calls. A watch poll yields one record

    {job: queued|running|succeeded|failed|cancelled|unknown,
     service: ok|degraded|down|unreachable|auth_failed|rate_limited|probe_broken,   # the *health* of the service
     detail, url, observed_at, retry_after}

:func:`next_record` folds a poll into the card's stored record (failure streak, next due time with jitter and
``retry_after``); :func:`derive` turns the stored record and the reader's own clock into the card state:

* ``external_running``: last job queued/running, service ok/degraded, record fresh.
* ``external_unknown``: one failed/missed poll, a rate limit, a job nobody could read, or a record older than one
  interval. Light blue with a hollow glyph.
* ``external_down`` (red): two consecutive down/unreachable polls, ``auth_failed`` at once (it never self-heals),
  a probe that broke twice, a host silent for more than two intervals whatever its last record said (a host that
  died after ONE failed poll is red too, not unknown for ever; only a host that advertises
  ``external-watch-v1``: an older host's silence stays grey, ``derive`` returns None), or a ci/deploy/external park
  with nothing watching it (``watch_missing``). ``host_alive`` (the reader IS the polling host and wrote other
  records within the interval) keeps a card its own sweep has not reached yet unknown, up to ``BEHIND_MAX``.
* a terminal job (succeeded/failed/cancelled) sets ``wake``: the card is woken with ``external_result`` and the job
  failing is NOT ``external_down``: that is the woken worker's business, not an outage.

A 429 / rate limit never counts toward red (the GitHub quota livelock of t_01a1d79b50e93032 must not repeat) and
the next poll is never earlier than its ``retry_after``.
"""
from __future__ import annotations

from typing import Any, Optional

SCHEMA = 1
CAPABILITY = "external-watch-v1"
EXTERNAL_KINDS = ("ci", "deploy", "external")

RUNNING = "external_running"
UNKNOWN = "external_unknown"
DOWN = "external_down"

JOBS = ("queued", "running", "succeeded", "failed", "cancelled", "unknown")
TERMINAL_JOBS = ("succeeded", "failed", "cancelled")
HEALTHS = ("ok", "degraded", "down", "unreachable", "auth_failed", "rate_limited", "probe_broken")
FAILING = ("down", "unreachable", "probe_broken")

MIN_EVERY = 120
MAX_EVERY = 6 * 3600
DEFAULT_CI_EVERY = 300
JITTER = 0.10
DOWN_AFTER = 2  # consecutive failed polls before a down/unreachable service is red
GRACE = 90  # a poll lands up to one dispatcher tick (60 s) plus its own read after ``next_due_at``
BEHIND_MAX = 4  # intervals a live local host may be behind on one card before that card is red anyway

SERVICE_NAMES = {"github_run": "GitHub Actions", "github_pr_checks": "GitHub Actions", "shell": "external probe"}


def clamp_every(value: Any, default: int = DEFAULT_CI_EVERY) -> int:
    """A poll interval in seconds: ``default`` when absent or not a number, else within [MIN_EVERY, MAX_EVERY]."""
    try:
        every = int(value)
    except (TypeError, ValueError):
        every = int(default)
    return max(MIN_EVERY, min(MAX_EVERY, every))


def next_record(prev: Optional[dict], poll: dict, *, now: float, every: Any, adapter: str, scope: str = "",
                rand: float = 0.5, host: Optional[str] = None) -> dict:
    """Fold one poll into the card's record. ``rand`` in [0, 1] places the next poll within ±``JITTER`` of
    ``every`` (never under ``MIN_EVERY``, never before the poll's ``retry_after``)."""
    interval = clamp_every(every)
    health = poll.get("service") if poll.get("service") in HEALTHS else "ok"
    job = poll.get("job") if poll.get("job") in JOBS else "unknown"
    streak = int((prev or {}).get("consecutive_failures") or 0)
    if health in FAILING:
        streak += 1
    elif health != "rate_limited":
        streak = 0
    retry_after = poll.get("retry_after")
    try:
        retry_after = max(0, int(retry_after)) if retry_after is not None else None
    except (TypeError, ValueError):
        retry_after = None
    trouble = health in FAILING or health == "auth_failed"
    since = (prev or {}).get("failing_since") if trouble or health == "rate_limited" else None
    factor = jitter_factor(rand)
    wait = max(interval * factor, MIN_EVERY, retry_after or 0)
    return {"schema": SCHEMA, "adapter": adapter, "scope": scope, "host": host, "job": job, "service": health,
            "detail": str(poll.get("detail") or "")[:300], "url": poll.get("url") or None,
            "observed_at": float(now), "next_due_at": float(now) + wait, "retry_after": retry_after,
            "consecutive_failures": streak, "every": interval, "jitter": factor,
            "failing_since": since or (float(now) if trouble else None)}


def jitter_factor(rand: Any) -> float:
    """``rand`` in [0, 1] -> the factor in [1 - JITTER, 1 + JITTER] the next poll's interval is stretched by."""
    return round(1 + (min(1.0, max(0.0, float(rand))) - 0.5) * 2 * JITTER, 4)


def _external(rec: Optional[dict], *, health: str, job: str, reason: str, adapter: str, service_name: str) -> dict:
    return {"service": service_name, "adapter": adapter, "job": job, "health": health, "reason": reason,
            "url": (rec or {}).get("url") or None,
            "observed_at": (rec or {}).get("observed_at"), "next_due_at": (rec or {}).get("next_due_at")}


def _out(state: str, health: str, job: str, reason: str, rec: Optional[dict], adapter: str, service_name: str,
         wake: bool = False) -> dict:
    return {"state": state, "health": health, "job": job, "reason": reason, "wake": wake,
            "external": _external(rec, health=health, job=job, reason=reason, adapter=adapter,
                                  service_name=service_name)}


def derive(rec: Optional[dict], *, now: float, every: Any, since: Optional[float] = None, advertises: bool = True,
           watch_missing: bool = False, adapter: Optional[str] = None,
           service_name: Optional[str] = None, host_alive: bool = False) -> Optional[dict]:
    """The card's external state at ``now`` by the reader's own clock, or None (grey: nothing to say).

    ``rec`` is the stored record (None before the first poll), ``since`` when the park began, ``advertises`` whether
    the probe host writes records at all (False: silence proves nothing, so None)."""
    interval = clamp_every(every)
    adapter = adapter or (rec or {}).get("adapter") or "shell"
    name = service_name or SERVICE_NAMES.get(adapter, "external service")
    if watch_missing:
        return _out(DOWN, "watch_missing", "unknown", "nothing is watching this wait: no adapter and no probe", rec,
                    adapter, name)
    if rec is None:
        if not advertises:
            return None
        age = now - (since if since is not None else now)
        if age > 2 * interval and not (host_alive and age <= BEHIND_MAX * interval):
            return _out(DOWN, "host_silent", "unknown", f"no check recorded {int(age // 60)} min after the park", rec,
                        adapter, name)
        if age > interval * (1 + JITTER):
            return _out(UNKNOWN, "host_silent", "unknown", f"no check recorded {int(age // 60)} min after the park",
                        rec, adapter, name)
        return _out(UNKNOWN, "ok", "unknown", "not checked yet", rec, adapter, name)
    job = rec.get("job") if rec.get("job") in JOBS else "unknown"
    health = rec.get("service") if rec.get("service") in HEALTHS else "ok"
    detail = str(rec.get("detail") or "")
    fails = int(rec.get("consecutive_failures") or 0)
    if job in TERMINAL_JOBS:
        return _out(RUNNING, "ok" if health in ("ok", "degraded") else health, job, detail or f"job {job}", rec,
                    adapter, name, wake=True)
    if health == "auth_failed":
        return _out(DOWN, health, job, detail or "authentication failed", rec, adapter, name)
    limited = health == "rate_limited"
    slack = float(rec.get("retry_after") or 0) if limited else 0.0
    age = now - float(rec.get("observed_at") or now)
    red = health in FAILING and fails >= DOWN_AFTER
    if age > 2 * interval + slack and not red:
        # Silence outranks whatever the last record said: one failed poll and then a dead host is not "unknown".
        if not advertises:
            return None
        mins = int(age // 60)
        if host_alive and age <= BEHIND_MAX * interval + slack:
            return _out(UNKNOWN, "host_silent", job, f"check overdue: this host's sweep is behind ({mins} min)", rec,
                        adapter, name)
        return _out(DOWN, "host_silent", job, f"no check for {mins} min (host silent)", rec, adapter, name)
    if health in FAILING:
        word = "probe failed" if health == "probe_broken" else f"{health}"
        reason = f"{fails} failed check{'s' if fails != 1 else ''} ({word}){': ' + detail if detail else ''}"
        return _out(DOWN if red else UNKNOWN, health, job, reason, rec, adapter, name)
    if limited:
        return _out(UNKNOWN, health, job, detail or "rate limited", rec, adapter, name)
    planned = float(rec.get("next_due_at") or 0) - float(rec.get("observed_at") or 0)
    if age > min(max(planned, interval * (1 + JITTER)), 2 * interval) + GRACE:
        return _out(UNKNOWN, "host_silent", job, f"last check {int(age // 60)} min ago", rec, adapter, name)
    if job == "unknown":
        return _out(UNKNOWN, health, job, detail or "job status unknown", rec, adapter, name)
    return _out(RUNNING, health, job, detail or f"job {job}", rec, adapter, name)
