"""Evidence-checked hub admission, using an exact subscription-only judge."""
from __future__ import annotations

import contextvars
import hashlib
import json
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import asdict, dataclass, replace

MODEL = "claude-sonnet-5-5"
PROVIDER = "anthropic"
TIMEOUT = 30
NEEDS = frozenset({"hub_chrome", "board_admin_key", "instance_role_ssm", "user_terminal",
                   "hub_worktree", "gateway_key_cli", "hub_live_process"})


@dataclass(frozen=True)
class Verdict:
    admit: bool = False
    need: str | None = None
    evidence: str = ""
    why: str = "admission unavailable"
    model: str = MODEL
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
    return Verdict(**result, skill_sha=sha)


def _parse(text, fields, hub_reason, sha):
    try:
        result = json.loads(text)
    except (ValueError, TypeError):
        result = None
    return validated(result, fields, hub_reason, sha) or Verdict(why="invalid admission verdict", skill_sha=sha)


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
    """Deny if the managed subscription pool, rubric or judge is unavailable."""
    sha = ""
    try:
        from agent import auxiliary_client as aux
        from agent.model_routing_managed import managed_config
        if managed_config() is None:
            return Verdict()
        body, sha = _skill()
        key = hashlib.sha256(json.dumps([card_fields.get("title") or "", card_fields.get("body") or "",
                                        hub_reason, sha], ensure_ascii=False).encode()).hexdigest()
        cached = _cache(key)
        if cached is not None:
            payload = {k: getattr(cached, k) for k in ("admit", "need", "evidence", "why")}
            return replace(_parse(json.dumps(payload), card_fields, hub_reason, sha), cached=True)
        route = {}
        deadline = time.monotonic() + TIMEOUT
        response = _bounded(lambda: _ask(aux, body, card_fields, hub_reason, route, deadline), deadline)
        if response is None:
            return Verdict(skill_sha=sha)
        if (route.get("provider"), route.get("model"), route.get("billing_group")) != (
                PROVIDER, MODEL, "subscription"):
            return Verdict(skill_sha=sha)
        verdict = _parse(response.choices[0].message.content, card_fields, hub_reason, sha)
        _cache(key, verdict)
        return verdict
    except Exception:  # health: allow BLE001 -- a judge outage must deny placement, never fall back
        return Verdict(skill_sha=sha)


def _ask(aux, body, card_fields, hub_reason, route, deadline):
    with aux.aux_stream_deadline(deadline):
        return aux.call_llm(
            task="hub_admission", provider=PROVIDER, model=MODEL, timeout=TIMEOUT,
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
