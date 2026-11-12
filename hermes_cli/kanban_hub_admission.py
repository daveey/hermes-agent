"""Evidence-checked hub admission, using an exact subscription-only judge."""
from __future__ import annotations

import hashlib
import json
import sqlite3
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
    path = get_hermes_home() / "skills/devops/hub-admission-test/SKILL.md"
    text = path.read_text(encoding="utf-8")
    body = text.split("---", 2)[-1].strip() if text.startswith("---\n") else text
    return body, hashlib.sha256(text.encode()).hexdigest()


def _parse(text, fields, hub_reason, sha):
    invalid = Verdict(why="invalid admission verdict", skill_sha=sha)
    try:
        result = json.loads(text)
    except (ValueError, TypeError):
        return invalid
    if not isinstance(result, dict) or set(result) != {"admit", "need", "evidence", "why"}:
        return invalid
    if (type(result["admit"]) is not bool or not isinstance(result["evidence"], str)
            or not isinstance(result["why"], str) or not result["why"].strip()):
        return invalid
    need = result["need"]
    if need is not None and (not isinstance(need, str) or need not in NEEDS):
        return invalid
    if result["admit"]:
        quote = result["evidence"]
        inputs = (fields.get("title") or "", fields.get("body") or "", hub_reason or "")
        if need is None or not quote.strip() or not any(quote in item for item in inputs):
            return invalid
    return Verdict(**result, skill_sha=sha)


def _cache(key, verdict=None):
    from hermes_cli.config import get_hermes_home
    directory = get_hermes_home() / "kanban"
    directory.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(directory / "hub-admission.sqlite", timeout=1)) as conn, conn:
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
            return cached
        route = {}
        response = aux.call_llm(
            task="hub_admission", provider=PROVIDER, model=MODEL, timeout=TIMEOUT,
            max_tokens=1024, temperature=0, route_info=route,
            messages=[{"role": "system", "content": body}, {"role": "user", "content": json.dumps({
                "title": card_fields.get("title") or "", "body": card_fields.get("body") or "",
                "hub_reason": hub_reason})}])
        if (route.get("provider"), route.get("model"), route.get("billing_group")) != (
                PROVIDER, MODEL, "subscription"):
            return Verdict(skill_sha=sha)
        verdict = _parse(response.choices[0].message.content, card_fields, hub_reason, sha)
        _cache(key, verdict)
        return verdict
    except Exception:  # health: allow BLE001 -- a judge outage must deny placement, never fall back
        return Verdict(skill_sha=sha)
