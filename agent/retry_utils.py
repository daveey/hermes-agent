"""Retry utilities — jittered backoff for decorrelated retries.

Jittered delays (vs. fixed exponential) prevent thundering-herd retry spikes
when many sessions hit the same rate-limited provider concurrently.
"""

import logging
import random
import re
import threading
import time
from datetime import datetime, timezone, UTC
from email.utils import parsedate_to_datetime
from typing import Any, Optional

# Monotonic counter for jitter-seed uniqueness within a process; locked
# because concurrent gateway sessions retry simultaneously.
_jitter_counter = 0
_jitter_lock = threading.Lock()

# Z.AI Coding Plan's GLM-5.2 endpoint often returns 429 code 1305 ("service may be
# temporarily overloaded"). Short retries hammer the same window, so after
# ``_ZAI_CODING_OVERLOAD_SHORT_ATTEMPTS`` normal retries the wait widens progressively;
# the cap stays interactive-friendly (a TUI message should fail visibly in minutes).
# The short count is shared by ``adaptive_rate_limit_backoff`` and
# ``zai_coding_overload_retry_ceiling`` so the two cannot silently desync.
_ZAI_CODING_OVERLOAD_LONG_BACKOFF = (30.0, 60.0, 90.0, 120.0)
_ZAI_CODING_OVERLOAD_SHORT_ATTEMPTS = 3


def parse_retry_after_seconds(value_or_headers: Any) -> Optional[float]:
    """Parse a ``Retry-After`` value (numeric / HTTP-date) or a headers mapping (both casings tried) into
    seconds, clamped at 0.0; None when absent / unparseable."""
    raw = value_or_headers
    if raw is not None and not isinstance(raw, (str, int, float)):
        getter = getattr(raw, "get", None)
        if not callable(getter):
            return None
        try:
            raw = getter("Retry-After")
            if raw is None:
                raw = getter("retry-after")
        except Exception:
            return None
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return max(0.0, float(raw))
    text = str(raw).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except (TypeError, ValueError):
        pass
    # HTTP-date form (RFC 7231): seconds until that instant, clamped at 0.
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:  # older stdlib returns None instead of raising
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


# Free-text "reset" grammars providers put in error bodies, tried in order. One table so the
# conversation loop's error context and the credential pool's cooldown agree on the same wait.
_QUOTA_RESET_DELAY_RE = re.compile(r"quotaResetDelay[:\s\"]+(\d+(?:\.\d+)?)(ms|s)", re.IGNORECASE)
# "Resets in 4hr 5min" (weekly usage limits), "resets in 2 hours 5 minutes", "resets in 30s".
_RESETS_IN_RE = re.compile(
    r"resets?\s+in\s+"
    r"(?:(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b\s*)?"
    r"(?:(\d+(?:\.\d+)?)\s*(?:m|min|mins|minute|minutes)\b\s*)?"
    r"(?:(\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds)\b)?", re.IGNORECASE,
)
_RETRY_AFTER_SECONDS_RE = re.compile(r"retry\s+(?:after\s+)?(\d+(?:\.\d+)?)\s*(?:sec|secs|seconds|s\b)", re.IGNORECASE)
# The plan usage-limit body field as it appears once stringified: ``'resets_in_seconds': 30995``.
_RESETS_IN_SECONDS_FIELD_RE = re.compile(r"resets_in_seconds\W{1,4}(\d+(?:\.\d+)?)", re.IGNORECASE)


def _quota_reset_seconds(m: re.Match[str]) -> float:
    value = float(m.group(1))
    return value / 1000.0 if m.group(2).lower() == "ms" else value


def _resets_in_seconds(m: re.Match[str]) -> Optional[float]:
    if not any(m.groups()):  # "resets in" with no unit-bearing number: not this grammar
        return None
    return float(m.group(1) or 0) * 3600 + float(m.group(2) or 0) * 60 + float(m.group(3) or 0)


# An explicit "retry after N s" wins over "resets in ..." (the credential pool's precedence):
# a body carrying both describes a short throttle inside a long quota window, and the
# shorter explicit wait is the one the provider actually asks for.
RETRY_DELAY_PATTERNS = (
    (_QUOTA_RESET_DELAY_RE, _quota_reset_seconds),
    (_RETRY_AFTER_SECONDS_RE, lambda m: float(m.group(1))),
    (_RESETS_IN_SECONDS_FIELD_RE, lambda m: float(m.group(1))),
    (_RESETS_IN_RE, _resets_in_seconds),
)


def format_reset_window(seconds: float) -> str:
    """``~9h`` / ``~45 min`` for chat copy naming when a quota window reopens (ceilinged)."""
    seconds = int(seconds)
    return f"~{-(-seconds // 3600)}h" if seconds >= 3600 else f"~{-(-seconds // 60)} min"


def reset_delay_from_message(message: str) -> Optional[float]:
    """Seconds-until-reset parsed from free-text provider error messages, or None."""
    if not message:
        return None
    for pattern, to_seconds in RETRY_DELAY_PATTERNS:
        m = pattern.search(message)
        if m and (seconds := to_seconds(m)) is not None:
            return seconds
    return None


def jittered_backoff(attempt: int, *, base_delay: float = 5.0, max_delay: float = 120.0, jitter_ratio: float = 0.5) -> float:
    """min(base * 2^(attempt-1), max_delay) + uniform jitter in
    [0, jitter_ratio * delay]. ``attempt`` is 1-based."""
    global _jitter_counter
    with _jitter_lock:
        _jitter_counter += 1
        tick = _jitter_counter

    exponent = max(0, attempt - 1)
    delay = max_delay if (exponent >= 63 or base_delay <= 0) else min(base_delay * (2 ** exponent), max_delay)

    # Seed from time + counter so coarse clocks still decorrelate.
    seed = (time.time_ns() ^ (tick * 0x9E3779B9)) & 0xFFFFFFFF
    return delay + random.Random(seed).uniform(0, jitter_ratio * delay)


def _error_text(error: Any) -> str:
    """Best-effort flattened provider error text for retry classification."""
    parts = [error, getattr(error, "message", None), getattr(error, "body", None), getattr(error, "response", None)]
    return " ".join(str(part) for part in parts if part is not None).lower()


def is_zai_coding_overload_error(*, base_url: str | None, model: str | None, error: Any) -> bool:
    """True only for the narrow Z.AI Coding Plan overload shape (429 + code
    1305 / "temporarily overloaded"), so ordinary quota 429s still fail fast."""
    text = _error_text(error)
    return (
        getattr(error, "status_code", None) == 429
        and "api.z.ai/api/coding/paas/v4" in (base_url or "").lower()
        and "glm-5.2" in (model or "").lower()
        and ("1305" in text or "temporarily overloaded" in text)
    )


def adaptive_rate_limit_backoff(
    attempt: int, *, base_url: str | None, model: str | None, error: Any, default_wait: float,
    short_attempts: int = _ZAI_CODING_OVERLOAD_SHORT_ATTEMPTS,
) -> tuple[float, str | None]:
    """``(wait_seconds, reason_label)``: ``default_wait`` for most providers; Z.AI Coding GLM-5.2 overloads keep
    ``short_attempts`` short retries, then 30→60→90→120s with light jitter. ``attempt`` is 1-based."""
    if not is_zai_coding_overload_error(base_url=base_url, model=model, error=error):
        return default_wait, None
    if attempt <= short_attempts:
        return default_wait, "zai_coding_overload_short"
    idx = min(attempt - short_attempts - 1, len(_ZAI_CODING_OVERLOAD_LONG_BACKOFF) - 1)
    base_delay = _ZAI_CODING_OVERLOAD_LONG_BACKOFF[idx]
    return jittered_backoff(1, base_delay=base_delay, max_delay=base_delay, jitter_ratio=0.2), "zai_coding_overload_long"


def zai_coding_overload_retry_ceiling(short_attempts: int = _ZAI_CODING_OVERLOAD_SHORT_ATTEMPTS) -> int:
    """Retry-loop ceiling for the full Z.AI overload schedule: one past the last long entry,
    because the loop gives up when ``retry_count >= ceiling`` BEFORE computing the attempt's
    backoff (the default ``api_max_retries`` of 3 equals ``short_attempts``)."""
    return short_attempts + len(_ZAI_CODING_OVERLOAD_LONG_BACKOFF) + 1


# A wait longer than this is one a person feels: a non-rate-limit Retry-After this long is announced
# when it starts, and on the Nous free tier an attended session ends the turn instead of sitting through it.
LIVE_RETRY_WAIT_CAP_S = 60.0
# Anthropic Tier 1 input-token buckets reset in ~171s, so a 120s cap re-tripped the limit; 600s
# covers realistic provider windows while still rejecting pathological values (#26293).
RETRY_AFTER_CAP_S = 600.0


def provider_retry_after_seconds(error: Any) -> Optional[float]:
    """Provider-declared cooldown: the ``Retry-After`` header, else a ``retry_after`` body field
    (top level or nested under ``error``). None when absent, unparseable or zero: a zero or expired
    cooldown carries no usable wait, and treating it as one would hot-loop the provider."""
    value = parse_retry_after_seconds(getattr(getattr(error, "response", None), "headers", None))
    if value is None:
        body = getattr(error, "body", None)
        if isinstance(body, dict):
            nested = body.get("error")
            value = parse_retry_after_seconds((nested if isinstance(nested, dict) else body).get("retry_after"))
    return value if value is not None and value > 0 else None


# ── Waiting out a local network loss ────────────────────────────────────────────────────────────
# A laptop that sleeps mid-turn wakes briefly ("dark wake" on macOS) with no working network: every
# retry fails DNS at once, so a retry budget counted in attempts or wall-clock seconds is gone in one
# 3 s wake (self-heal cron-b459306dea). The retry loop calls ``wait_for_network`` instead of spending an
# attempt when the error's cause chain shows a LOCAL resolution/network failure. Its budget is awake
# time (``time.monotonic`` pauses while macOS/Linux sleep, see ``AwakeIdleMeter``).
DEFAULT_OFFLINE_WAIT_SECONDS = 900.0
OFFLINE_POLL_MIN_S = 5.0
OFFLINE_POLL_MAX_S = 30.0
# A host that resolves whenever the machine has working DNS: it tells "this machine is offline"
# apart from "the provider's host name does not resolve" (a typo or a dead custom endpoint).
NETWORK_CONTROL_HOST = "github.com"
_RESOLVE_TIMEOUT_S = 5.0
_OFFLINE_SLICE_S = 0.2
_logger = logging.getLogger(__name__)


def is_local_network_error(error: Any) -> bool:
    """True when *error* or anything in its ``__cause__``/``__context__`` chain is a local DNS or
    network-down failure (``socket.gaierror``, ENETUNREACH, or SDK text naming one). Refused or reset
    connections and timeouts are the far end's doing and stay ordinary retryable errors."""
    import errno as _errno
    import socket as _socket
    from agent.api_error_summary import _NETWORK_RESOLUTION_MARKERS

    seen: set[int] = set()
    current = error
    while current is not None and id(current) not in seen and len(seen) < 16:
        seen.add(id(current))
        if isinstance(current, _socket.gaierror):
            return True
        if isinstance(current, OSError) and getattr(current, "errno", None) == _errno.ENETUNREACH:
            return True
        if any(marker in str(current).lower() for marker in _NETWORK_RESOLUTION_MARKERS):
            return True
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return False


# libc resolution is not cancellable. Bound abandoned probes process-wide so a stuck resolver
# cannot accumulate a new native thread on every poll in every turn.
_dns_slots = threading.BoundedSemaphore(8)


def host_resolves(host: str, timeout: float = _RESOLVE_TIMEOUT_S, *, is_interrupted=None) -> bool:
    """Bounded, interruptible DNS observation; timed-out native lookups retain their slot until exit."""
    import socket as _socket

    interrupted = is_interrupted or (lambda: False)
    if interrupted() or timeout <= 0 or not _dns_slots.acquire(blocking=False):
        return False
    outcome: dict[str, bool] = {}
    done = threading.Event()

    def _lookup() -> None:
        try:
            outcome["ok"] = bool(_socket.getaddrinfo(host, 443, type=_socket.SOCK_STREAM))
        except (OSError, UnicodeError):
            outcome["ok"] = False
        finally:
            _dns_slots.release()
            done.set()

    worker = threading.Thread(target=_lookup, name="offline-dns-probe", daemon=True)
    try:
        worker.start()
    except BaseException:
        _dns_slots.release()
        raise
    deadline = time.monotonic() + timeout
    while not done.is_set():
        remaining = deadline - time.monotonic()
        if interrupted() or remaining <= 0:
            return False
        done.wait(min(_OFFLINE_SLICE_S, remaining))
    return bool(outcome.get("ok"))


def provider_host(base_url: Any) -> str:
    """The host name of *base_url* ("" when it has none)."""
    from urllib.parse import urlparse

    try:
        return urlparse(str(base_url or "")).hostname or ""
    except ValueError:
        return ""


def wait_for_network(
    host: str, *, budget_s: float, is_interrupted: Any = None, on_tick: Any = None, on_wait: Any = None,
    resolve: Any = None, control_host: str = NETWORK_CONTROL_HOST,
) -> tuple[str, float]:
    """Wait until *host* resolves again, for at most *budget_s* seconds of AWAKE time.

    Returns ``(outcome, awake_seconds_spent)``; outcome is ``"online"`` (it resolves again: retry
    without spending an attempt), ``"already_online"`` (it resolved at once: the failure is not the
    network's, count the attempt), ``"misconfigured"`` (the control host resolves but the provider's
    does not: logs an ERROR and returns at once), ``"interrupted"``, ``"exhausted"`` or ``"disabled"``.
    Polls every OFFLINE_POLL_MIN_S..OFFLINE_POLL_MAX_S seconds in short slices so *is_interrupted*
    is honoured promptly; *on_tick(awake_spent_s)* runs about every 30 s of waiting (liveness)."""
    is_interrupted = is_interrupted or (lambda: False)
    if not host or budget_s <= 0:
        return "disabled", 0.0
    if is_interrupted():
        return "interrupted", 0.0
    started = time.monotonic()

    def elapsed():
        return max(0.0, time.monotonic() - started)

    def lookup(name):
        if is_interrupted() or elapsed() >= budget_s:
            return False
        if resolve is not None:
            return resolve(name)
        return host_resolves(name, timeout=min(_RESOLVE_TIMEOUT_S, budget_s - elapsed()),
                             is_interrupted=is_interrupted)

    if lookup(host):
        return "already_online", elapsed()
    if control_host and control_host != host and lookup(control_host):
        _logger.error(
            "provider host does not resolve while network is up: %s (control host %s resolves) — "
            "check the provider base_url", host, control_host,
        )
        return "misconfigured", elapsed()
    _logger.warning(
        "offline — waiting for network: %s and %s do not resolve; waiting up to %.0fs of awake time",
        host, control_host, budget_s,
    )
    if on_wait is not None and not is_interrupted():
        on_wait()
    spent = elapsed()
    delay = OFFLINE_POLL_MIN_S
    next_tick = 30.0
    while spent < budget_s:
        slice_end = min(budget_s, spent + delay)
        while spent < slice_end:
            if is_interrupted():
                return "interrupted", elapsed()
            time.sleep(min(_OFFLINE_SLICE_S, slice_end - spent))
            spent = elapsed()
        if on_tick is not None and spent >= next_tick:
            next_tick = spent + 30.0
            on_tick(spent)
        if is_interrupted():
            return "interrupted", elapsed()
        if lookup(host):
            spent = elapsed()
            _logger.info("network is back: %s resolves after %.0fs of awake waiting", host, spent)
            return "online", spent
        spent = elapsed()
        delay = min(delay * 1.5, OFFLINE_POLL_MAX_S)
    return "interrupted" if is_interrupted() else "exhausted", elapsed()
