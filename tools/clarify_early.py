"""Early answers for a blocked clarify: a decision recorded elsewhere (user-steps For-you queue) ends the wait.

Plugins register a provider ``fn(session_id, tool_call_id) -> EarlyAnswer | None``. ``clarify_tool`` asks providers
before showing a question and, while a surface is blocked waiting on the human, every ``POLL_INTERVAL_S``. A provider
returns an answer only for decisions that belong to that session; ``commit()`` marks them delivered and is called only
when the early answer is the one the clarify returns (never when the human answered first).

Bound questions: a provider whose function has ``accepts_question = True`` is called as
``fn(session_id, tool_call_id, question=descriptor)`` where ``descriptor`` (:func:`question_descriptor`) is the exact
ordered question/choice snapshot the human sees plus its SHA-256 ``digest``. An early answer that carries
``decisions`` ends the clarify only when its ``binding`` names this tool call and this digest: matching the session
alone is never enough, so an unrelated decision recorded in the same session stays undelivered for normal delivery.
A legacy provider (no ``accepts_question``) can still withdraw a clarify without answering it (no ``decisions``,
e.g. the update pause), but its decision answers are ignored for a bound question.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 2.0
STATUS = "answered_elsewhere"
DESCRIPTOR_VERSION = 1


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def question_descriptor(normalized: list, tool_call_id: str) -> dict:
    """The exact ordered questions and choice labels a surface shows for this clarify, with their digest."""
    questions = [{"question": str(q.get("question") or ""),
                  "choices": [str(c) for c in (q.get("choices_offered") or q.get("choices") or [])]}
                 for q in normalized or [] if isinstance(q, dict)]
    return {"schemaVersion": DESCRIPTOR_VERSION, "toolCallId": str(tool_call_id or ""), "questions": questions,
            "digest": hashlib.sha256(_canonical(questions).encode("utf-8")).hexdigest()}


def bound_to(early: EarlyAnswer, question: Optional[dict]) -> bool:
    """Whether ``early`` may end the clarify described by ``question``."""
    if question is None:
        return True  # legacy caller without a descriptor: unchanged behaviour
    result = early.result if isinstance(getattr(early, "result", None), dict) else {}
    if not result.get("decisions"):
        return True  # a withdrawal that answers nothing (update pause) needs no binding
    binding = result.get("binding")
    return (isinstance(binding, dict) and binding.get("questionDigest") == question.get("digest")
            and binding.get("toolCallId") == question.get("toolCallId"))


@dataclass
class EarlyAnswer:
    result: dict
    commit: Callable[[], Any]


_lock = threading.Lock()
_providers: list[Callable[..., Optional[EarlyAnswer]]] = []


def register_provider(fn):
    """Add a provider; returns a disposer."""
    with _lock:
        _providers.append(fn)

    def dispose():
        with _lock:
            if fn in _providers:
                _providers.remove(fn)
    return dispose


def reset_for_tests() -> None:
    with _lock:
        _providers.clear()


def has_providers() -> bool:
    with _lock:
        return bool(_providers)


def poll(session_id: str, tool_call_id: str, question: Optional[dict] = None) -> Optional[EarlyAnswer]:
    """First provider answer bound to this clarify, or None. A failing provider never affects the clarify."""
    with _lock:
        providers = list(_providers)
    for fn in providers:
        try:
            if question is not None and getattr(fn, "accepts_question", False) is True:
                found = fn(session_id, tool_call_id, question=question)
            else:
                found = fn(session_id, tool_call_id)
        except Exception:
            logger.debug("clarify early-answer provider failed", exc_info=True)
            continue
        if found is None:
            continue
        if not bound_to(found, question):
            logger.info("clarify early answer ignored: its decisions are not bound to this question")
            continue
        return found
    return None


def render(early: EarlyAnswer) -> str:
    """Tool result JSON. Deliberately not the ``responses`` shape: this is never text the user typed."""
    result = dict(early.result)
    result.setdefault("status", STATUS)
    result.setdefault("outcome", STATUS)
    if not result.get("notice"):
        parts = [f"{d.get('title', d.get('requestId', ''))}: {d.get('choice', '')}" for d in result.get("decisions", [])]
        result["notice"] = "David answered in the For-you queue: " + "; ".join(parts)
    return json.dumps(result, ensure_ascii=False)


def _commit(early: EarlyAnswer) -> None:
    try:
        early.commit()
    except Exception:
        logger.warning("clarify early-answer commit failed", exc_info=True)


def ask(callback: Callable, normalized: list, session_id: str, tool_call_id: str) -> tuple[Optional[dict], Optional[str]]:
    """Run ``callback(normalized)`` with early-answer support -> ``(reply, early_json)``; exactly one is not None."""
    question = question_descriptor(normalized, tool_call_id)
    if session_id and has_providers():
        early = poll(session_id, tool_call_id, question)
        if early is not None:
            _commit(early)
            return None, render(early)
    cancel = getattr(callback, "cancel_early", None)
    if not (session_id and has_providers() and callable(cancel)):
        return callback(normalized), None
    box: dict[str, Any] = {}
    done = threading.Event()
    ctx = contextvars.copy_context()

    def run():
        try:
            box["reply"] = ctx.run(callback, normalized)
        except BaseException as exc:  # re-raised on the caller's thread
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=run, name="clarify-wait", daemon=True).start()
    while not done.wait(POLL_INTERVAL_S):
        early = poll(session_id, tool_call_id, question)
        if early is None:
            continue
        cancelled = False
        try:
            cancelled = bool(cancel(str(early.result.get("status") or STATUS)))
        except Exception:
            logger.debug("clarify cancel_early failed", exc_info=True)
        done.wait(5)
        reply = box.get("reply")
        if cancelled or reply is None or reply.get("outcome") == "cancelled":
            _commit(early)
            return None, render(early)
        break  # the human answered first: their answer stands, the decision stays undelivered
    if "error" in box:
        raise box["error"]
    return box["reply"], None
