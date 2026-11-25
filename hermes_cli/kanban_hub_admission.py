"""Evidence-checked hub admission, using a routed subscription-only judge."""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import asdict, dataclass, replace

_log = logging.getLogger(__name__)
TIMEOUT = 30
# The hub-admission-test rubric's allowlist. Board admin key and instance-role SSM are not on it: hub-admin from
# a capability:hub-write worker reaches both, so a verdict naming either is invalid (deny).
NEEDS = frozenset({"hub_chrome", "user_terminal", "hub_worktree", "gateway_key_cli", "hub_live_process"})


@dataclass(frozen=True)
class Verdict:
    admit: bool = False
    need: str | None = None
    evidence: str = ""
    why: str = "admission unavailable"
    model: str = ""
    skill_sha: str = ""
    cached: bool = False
    forced: bool = False


def _skill():
    from hermes_cli.config import get_hermes_home
    from agent.skill_utils import get_external_skills_dirs
    # Only profile-owned/configured roots: never let a repository's project skill
    # replace the admission policy that decides where that repository's work runs.
    roots = [get_hermes_home() / "skills", *get_external_skills_dirs()]
    paths = [root / relative for root in roots for relative in (
        "devops/hub-admission-test/SKILL.md", "hub-admission-test/SKILL.md")]
    path = next((path for path in paths if path.is_file()), None)
    if path is None:
        raise FileNotFoundError("hub-admission-test skill is not installed")
    text = path.read_text(encoding="utf-8-sig")
    body = text.split("---", 2)[-1].strip() if text.startswith("---\n") else text
    return body, hashlib.sha256(text.encode()).hexdigest()


# A card producer's own placement sentence ("Pinned to the gateway that filed it: <why>") is that producer's
# self-assertion about where its card should run, written before any admission. It is never evidence of an
# allowlisted hub-only need: t_01a1cf7f8f211403 was admitted `hub_live_process` on exactly that sentence, while a
# capability:hub-write worker could do every step with ssh and hub-admin. [daveey]
SELF_ASSERTIONS = ("pinned to the gateway that filed it", "pinned to the machine that filed it")
SELF_ASSERTION_WHY = "the evidence is the producer's own placement sentence, not a hub-only need"


def self_assertion(quote):
    """True when an admission quote leans on a producer's pin sentence (any case, any surrounding text)."""
    text = " ".join(str(quote or "").split()).lower()
    return any(marker in text for marker in SELF_ASSERTIONS)


_PIN = re.compile("|".join(r"\s+".join(map(re.escape, m.split())) for m in SELF_ASSERTIONS), re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)|\n[ \t]*\n")
# A "." ending one of these words, or a dotted run of single letters ("e.g", "U.S"), abbreviates rather than ends
# the sentence: stopping there let the wrapped rest of a pin escape its span (review 5884663184). Any other dotted
# token (a filename, version, URL or ellipsis) and "no." do end it, or real evidence on the next line would be
# denied as pin text (review 5884844910). [daveey]
_ABBREVIATIONS = frozenset({"e.g", "i.e", "etc", "vs", "cf", "viz", "approx", "incl", "esp", "al", "mr", "mrs",
                            "ms", "dr", "st"})
_LETTER_DOTS = re.compile(r"(?:[^\W\d_]\.)+[^\W\d_]")
_LAST_WORD = re.compile(r"[\w.]*$")


def _sentence_end(text, pos):
    """The first sentence terminator (or blank line) at or after ``pos``, skipping abbreviation dots."""
    for stop in _SENTENCE_END.finditer(text, pos):
        if text[stop.start()] == ".":
            last = _LAST_WORD.search(text, max(0, stop.start() - 32), stop.start())
            word = last.group().lower() if last else ""
            if word in _ABBREVIATIONS or _LETTER_DOTS.fullmatch(word):
                continue
        return stop
    return None


def _pin_spans(text):
    """``[start, end)`` of every pin line in ``text``: the marker's whole line, run on to the sentence's end
    when the producer wrapped it (never past a blank line).

    Deliberately coarse and fail-safe (review 5884663184): the span is the pin's WHOLE line, so other text on that
    line after the pin sentence counts as pin text, and a pin with no terminator and no blank line runs to the end
    of the field. A quote found only there is denied as self-assertion; evidence the card also states outside a
    pin span is still judged on that text (``quotes_pin_sentence``)."""
    spans = []
    for match in _PIN.finditer(text):
        start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        stop = _sentence_end(text, match.end())
        spans.append((start, max(len(text) if line_end < 0 else line_end, stop.end() if stop else len(text))))
    return spans


def quotes_pin_sentence(quote, fields, hub_reason):
    """True when ``quote`` is a producer's pin sentence: it names the marker, or every verbatim occurrence of it
    in the card overlaps a pin line (the rationale tail alone, review t_01a1d24d488edbd9). [daveey]"""
    if self_assertion(quote):
        return True
    quote = str(quote or "")
    found = False
    for item in (fields.get("title") or "", fields.get("body") or "", hub_reason or ""):
        spans, at = _pin_spans(item), item.find(quote) if quote else -1
        while at >= 0:
            found = True
            if not any(at < end and at + len(quote) > start for start, end in spans):
                return False
            at = item.find(quote, at + 1)
    return found


def validated(result, fields, hub_reason, sha):
    """The Verdict for a well-formed, evidence-checked ``{admit, need, evidence, why}`` dict, else None."""
    if not isinstance(result, dict) or set(result) != {"admit", "need", "evidence", "why"}:
        return None
    if (type(result["admit"]) is not bool or not isinstance(result["evidence"], str)
            or not isinstance(result["why"], str) or not result["why"].strip()):
        return None
    need = result["need"]
    if need is not None and (not isinstance(need, str) or need not in NEEDS):
        return None
    if result["admit"]:
        quote = result["evidence"]
        inputs = (fields.get("title") or "", fields.get("body") or "", hub_reason or "")
        if need is None or not quote.strip() or not any(quote in item for item in inputs):
            return None
        if quotes_pin_sentence(quote, fields, hub_reason):
            return None  # a producer's self-assertion: invalid as admission evidence (deny), cached copies too
    return Verdict(**result, skill_sha=sha)


def _parse(text, fields, hub_reason, sha):
    try:
        result = json.loads(text)
    except (ValueError, TypeError):
        result = None
    verdict = validated(result, fields, hub_reason, sha)
    if verdict is not None:
        return verdict
    if pin_quoting_admit(result, fields, hub_reason):
        return Verdict(why=f"invalid admission verdict: {SELF_ASSERTION_WHY}", skill_sha=sha)
    return Verdict(why="invalid admission verdict", skill_sha=sha)


def pin_quoting_admit(result, fields, hub_reason):
    """True when ``result`` admits on a producer's pin sentence (so a deny can carry SELF_ASSERTION_WHY)."""
    return (isinstance(result, dict) and result.get("admit") is True and isinstance(result.get("evidence"), str)
            and quotes_pin_sentence(result["evidence"], fields, hub_reason))


def _cache(key, verdict=None):
    from hermes_cli.config import get_hermes_home
    from hermes_cli import kanban_lockguard
    directory = get_hermes_home() / "kanban"
    directory.mkdir(parents=True, exist_ok=True)
    with closing(kanban_lockguard.connect(directory / "hub-admission.sqlite", timeout=1)) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS verdicts (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        if verdict is None:
            row = conn.execute("SELECT value FROM verdicts WHERE key=?", (key,)).fetchone()
            return replace(Verdict(**json.loads(row[0])), cached=True) if row else None
        conn.execute("INSERT OR REPLACE INTO verdicts VALUES (?, ?)", (key, json.dumps(asdict(verdict))))
        conn.execute("DELETE FROM verdicts WHERE rowid NOT IN "
                     "(SELECT rowid FROM verdicts ORDER BY rowid DESC LIMIT 1024)")
    return None


def admit(card_fields, *, hub_reason=None) -> Verdict:
    """Deny if the managed subscription pool, rubric or judge is unavailable (each branch logs why)."""
    sha = ""
    try:
        from hermes_cli import kanban_comment_relay
        relay = kanban_comment_relay.verdict(card_fields, hub_reason)
        if relay is not None:
            return relay  # a comment relay is denied with its worker route, no model call
        from agent import auxiliary_client as aux
        from agent.model_routing_managed import managed_config
        if managed_config() is None:
            _log.warning("hub admission unavailable: managed subscription routing is off")
            return Verdict()
        body, sha = _skill()
        key = hashlib.sha256(json.dumps([card_fields.get("title") or "", card_fields.get("body") or "",
                                        hub_reason, sha], ensure_ascii=False).encode()).hexdigest()
        cached = _cache(key)
        if cached is not None:
            payload = {k: getattr(cached, k) for k in ("admit", "need", "evidence", "why")}
            return replace(_parse(json.dumps(payload), card_fields, hub_reason, sha), cached=True, model=cached.model)
        route = {}
        deadline = time.monotonic() + TIMEOUT
        with _owner_scope():
            response = _bounded(lambda: _ask(aux, body, card_fields, hub_reason, route, deadline), deadline)
        if response is None:
            _log.warning("hub admission unavailable: the judge did not answer within %ss", TIMEOUT)
            return Verdict(skill_sha=sha)
        if route.get("billing_group") != "subscription":
            _log.warning("hub admission unavailable: route %s/%s/%s is not the subscription judge",
                         route.get("provider"), route.get("model"), route.get("billing_group"))
            return Verdict(skill_sha=sha)
        verdict = replace(_parse(response.choices[0].message.content, card_fields, hub_reason, sha),
                          model=route.get("model") or "")
        _cache(key, verdict)
        return verdict
    except Exception as exc:  # health: allow BLE001 -- a judge outage must deny placement, never fall back
        _log.warning("hub admission unavailable: %s: %s", type(exc).__name__, str(exc)[:300])
        return Verdict(skill_sha=sha)


@contextlib.contextmanager
def _owner_scope():
    """Bind the launch profile's secret scope for a judge call made outside any turn. [daveey]

    The gateway dispatcher ticks in an empty Context, so hub pickup's judge ran unscoped and, on a
    multiplexing gateway, its first credential read raised ``UnscopedSecretError``: every cache-miss
    card was "unavailable" (t_01a1cff283f89392). The auto-decomposer binds the same scope. A bound
    scope (a creating turn's) is kept; the judge thread inherits it via ``copy_context``.
    """
    from agent.secret_scope import current_secret_scope, is_multiplex_active, reset_secret_scope, set_secret_scope
    if not is_multiplex_active() or current_secret_scope() is not None:
        yield
        return
    from hermes_constants import get_hermes_home
    from tui_gateway.launch_profile_policy import owner_secret_scope
    home = get_hermes_home()
    token = set_secret_scope(owner_secret_scope(home), profile_home=str(home))
    try:
        yield
    finally:
        reset_secret_scope(token)


def _ask(aux, body, card_fields, hub_reason, route, deadline):
    with aux.aux_stream_deadline(deadline):
        return aux.call_llm(
            task="hub_admission", timeout=TIMEOUT,
            max_tokens=1024, temperature=0, route_info=route,
            messages=[{"role": "system", "content": body}, {"role": "user", "content": json.dumps({
                "title": card_fields.get("title") or "", "body": card_fields.get("body") or "",
                "hub_reason": hub_reason})}])


def _bounded(call, deadline):
    """Run ``call`` with a hard total deadline, route and credential resolution included.

    The stream deadline clamps provider sockets, but nothing bounds the work before a stream
    starts; a placement decision must not wait on it. Past the deadline the owner denies and
    the daemon is abandoned: it holds no lock or DB handle, its socket is clamped by the same
    absolute stream deadline, and its result is discarded (never cached).
    """
    done, outcome = threading.Event(), {}

    def run():
        try:
            outcome["result"] = call()
        except BaseException as exc:  # health: allow BLE001 -- reported to the owner as a deny
            outcome["exception"] = exc
        finally:
            done.set()

    threading.Thread(target=contextvars.copy_context().run, args=(run,), name="hub-admission-judge",
                     daemon=True).start()
    if not done.wait(max(0.0, deadline - time.monotonic())):
        return None
    if "exception" in outcome:
        raise outcome["exception"]
    return outcome.get("result")
