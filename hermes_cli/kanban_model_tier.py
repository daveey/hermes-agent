"""Kanban default model tier [daveey].

When ``kanban.default_model`` is set, the dispatcher runs a card that has no pinned model on that model
(typically a cheaper tier) instead of the assignee profile's own model. The pin is applied to the in-memory
task only (``model_override``/``provider_override`` on the claimed ``Task``), so the card row never changes
and a later run re-decides. Cards keep the profile's model when:

* the card pins ``model`` itself (``kanban_create(model=...)`` / ``hermes kanban create --model``);
* it runs in the review lane (``sdlc-review`` loaded — claimed from the review column);
* an earlier run on the card failed (``crashed``/``timed_out``/``gave_up``/``failed``) or a reviewer asked for
  changes: the second attempt after a failed fix gets the stronger model;
* its title/body matches one of ``kanban.default_model_keep_patterns`` (case-insensitive regexes);
* its assignee is not in ``kanban.default_model_profiles`` (empty = every profile).

Unset ``kanban.default_model`` (the default) = upstream behaviour: every card runs on its profile's model.
With a class default, review/retry/keep-pattern work explicitly requests ``class:deep`` rather than
inheriting a potentially weaker profile default. Explicit card pins and out-of-scope profiles are unchanged.
Each decision is recorded as a ``model_tier`` task event on the run so tier effects can be measured later.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Run outcomes that mean "the previous attempt did not work" (not a checkpoint, a human block or a reclaim).
FAILED_RUN_OUTCOMES = ("crashed", "oom_killed", "signaled", "timed_out", "gave_up", "failed")
# Events that mean "a reviewer rejected the previous attempt".
REWORK_EVENT_KINDS = ("changes_requested", "review_reopened")
REVIEW_SKILL = "sdlc-review"


def _str(value: Any) -> str:
    return str(value or "").strip()


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        return [_str(v) for v in value if _str(v)]
    return []


def load_tier_config() -> dict[str, Any]:
    """The ``kanban`` config section (empty dict when unreadable)."""
    try:
        from hermes_cli.config import load_config
        section = (load_config() or {}).get("kanban") or {}
        return section if isinstance(section, dict) else {}
    except Exception as exc:  # never break dispatch over config
        logger.debug("kanban model tier: config unreadable (%s)", exc)
        return {}


def _prior_failure(conn: sqlite3.Connection, task_id: str, current_run_id: Optional[int]) -> Optional[str]:
    marks = ",".join("?" for _ in FAILED_RUN_OUTCOMES)
    row = conn.execute(
        f"SELECT outcome FROM task_runs WHERE task_id = ? AND id IS NOT ? AND outcome IN ({marks}) "
        "ORDER BY id DESC LIMIT 1",
        (task_id, current_run_id, *FAILED_RUN_OUTCOMES),
    ).fetchone()
    if row is not None:
        return f"run_{row[0]}"
    marks = ",".join("?" for _ in REWORK_EVENT_KINDS)
    row = conn.execute(
        f"SELECT kind FROM task_events WHERE task_id = ? AND kind IN ({marks}) ORDER BY id DESC LIMIT 1",
        (task_id, *REWORK_EVENT_KINDS),
    ).fetchone()
    return row[0] if row is not None else None


def resolve_model_tier(
    conn: sqlite3.Connection, task: Any, *, lane: str, cfg: Optional[dict[str, Any]] = None,
) -> tuple[Optional[str], Optional[str], str]:
    """``(model, provider, reason)`` for a claimed task. ``model`` is None when the worker keeps whatever it
    would have run on anyway (its own pin or the profile's model); ``reason`` names the rule that decided."""
    cfg = load_tier_config() if cfg is None else cfg
    default_model = _str(cfg.get("default_model"))
    if _str(getattr(task, "model_override", None)):
        return None, None, "pinned"
    if not default_model:
        return None, None, "off"
    profiles = _as_list(cfg.get("default_model_profiles"))
    if profiles and _str(getattr(task, "assignee", None)) not in profiles:
        return None, None, "profile_out_of_scope"
    # Class defaults opt into class-preserving routing. Strong work must not
    # fall back to a profile that may itself now default to standard/fast.
    from agent.model_routing import is_class_selector
    strong: tuple[Optional[str], Optional[str]] = (
        ("class:deep", "auto") if is_class_selector(default_model) else (None, None)
    )
    if lane == "review" or REVIEW_SKILL in (getattr(task, "skills", None) or []):
        return *strong, "review"
    try:
        failure = _prior_failure(conn, task.id, getattr(task, "current_run_id", None))
    except sqlite3.Error as exc:
        logger.debug("kanban model tier: prior-failure lookup failed for %s (%s)", task.id, exc)
        failure = None
    if failure:
        return *strong, f"retry_after_{failure}"
    text = f"{getattr(task, 'title', '') or ''}\n{getattr(task, 'body', '') or ''}"
    for pattern in _as_list(cfg.get("default_model_keep_patterns")):
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return *strong, f"pattern:{pattern}"
        except re.error as exc:
            logger.warning("kanban model tier: bad default_model_keep_patterns entry %r (%s)", pattern, exc)
    return default_model, _str(cfg.get("default_provider")) or infer_provider(default_model), "default"


def infer_provider(model: str) -> Optional[str]:
    """Provider for a tier model when ``default_provider`` is unset. Without one the worker pairs the tier model
    with the PROFILE's provider, so ``claude-sonnet-5-5`` was sent to openai-codex (HTTP 400, every card dead on
    its first run) after the profile model was switched to a GPT. Static catalog first, then the model family."""
    try:
        from hermes_cli.models import detect_static_provider_for_model
        hit = detect_static_provider_for_model(model, "")
        if hit:
            return hit[0]
    except Exception as exc:  # never break dispatch over the catalog
        logger.debug("kanban model tier: provider lookup failed for %s (%s)", model, exc)
    return "anthropic" if model.lower().startswith("claude") else None


def worker_model_args(model: str, provider: Optional[str]) -> list[str]:
    """Keep class allocation inside the owning worker, never the dispatcher.

    An explicit auto provider prevents the worker profile's concrete provider
    from turning a class request into a backend pin.
    """
    from agent.model_routing import ModelRoutingError, is_class_selector
    if is_class_selector(model):
        if provider and provider != "auto":
            raise ModelRoutingError("Class model selectors require provider auto")
        provider = "auto"
    args = ["-m", model]
    if provider:
        args.extend(["--provider", provider])
    return args


def apply_model_tier(conn: sqlite3.Connection, task: Any, *, lane: str, cfg: Optional[dict[str, Any]] = None) -> str:
    """Pin the tier's model on the in-memory ``task`` (never the DB row) and record a ``model_tier`` event.
    Returns the reason. Never raises: a failure leaves the task on its profile's model."""
    try:
        model, provider, reason = resolve_model_tier(conn, task, lane=lane, cfg=cfg)
    except Exception as exc:
        logger.warning("kanban model tier: resolve failed for %s (%s)", getattr(task, "id", "?"), exc)
        return "error"
    if reason not in ("off", "profile_out_of_scope"):
        try:
            from hermes_cli import kanban_provider_walls as _walls
            cfg_ = load_tier_config() if cfg is None else cfg
            if model:
                probe = type("T", (), {"model_override": model, "provider_override": provider,
                                       "title": task.title, "body": task.body})()
            else:
                probe = task
            moved = _walls.retier_for_walls(probe, cfg_)
            if moved:
                model, provider, walled = moved
                reason = f"{reason}+wall:{walled}"
        except Exception as exc:  # never break dispatch over wall bookkeeping
            logger.debug("kanban model tier: wall re-tier failed for %s (%s)", getattr(task, "id", "?"), exc)
    if model:
        task.model_override = model
        task.provider_override = provider
    if reason != "off":
        try:
            from hermes_cli import kanban_db as _kb
            payload = {"reason": reason, "model": model or _str(getattr(task, "model_override", None)) or None,
                       "provider": provider or _str(getattr(task, "provider_override", None)) or None,
                       "lane": lane}
            with _kb.write_txn(conn):
                _kb._append_event(conn, task.id, "model_tier", payload, run_id=getattr(task, "current_run_id", None))
        except Exception as exc:
            logger.debug("kanban model tier: event write failed for %s (%s)", getattr(task, "id", "?"), exc)
    return reason
