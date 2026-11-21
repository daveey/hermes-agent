"""Model-neutral Kanban specifications; execution receipts remain concrete.

Ingress rejects restrictions rather than silently accepting an ineffective pin.
Legacy routing reads use a separate projection and never rewrite author history.
Interactive sessions and the model catalog are deliberately outside this policy.
"""
from __future__ import annotations

import json
import re
from agent.model_routing import is_class_selector

ERROR = 'Kanban is model-neutral: use a class/tier or capability requirement, not a concrete model/provider or mandatory pin'
# Match directives, not incidental named execution evidence or author exclusions.
_REQUIRED = re.compile(r'^\s*require_model\s*:\s*(?:true|yes|1)\s*$', re.IGNORECASE | re.MULTILINE)
_MODEL = re.compile(r'(?:\b|(?<=independent)|(?<=primary))(?:gpt-[\w.-]+|claude-[\w.-]+|grok-[\w.-]+|gemini-[\w.-]+|opus|sonnet|fable|astra|luna|sol)\b', re.IGNORECASE)
_FORCE = re.compile(r'\b(?:mandatory|required|must|only|pinned|pin|no fallback)\b', re.IGNORECASE)
_EVIDENCE = 'Historical run instruction (superseded and not active): '


def _active_text(line):
    """Exclude a labelled JSON evidence string, never unquoted trailing policy."""
    stripped = line.lstrip()
    if stripped.startswith(_EVIDENCE):
        try:
            value, end = json.JSONDecoder().raw_decode(stripped[len(_EVIDENCE):])
        except ValueError:
            return line
        if isinstance(value, str):
            return stripped[len(_EVIDENCE) + end:]
    return line


def validate_selector(model=None, provider=None, *, mandatory=False):
    """Validate newly supplied selectors without consulting quota or credentials."""
    model = (model or '').strip() or None
    provider = (provider or '').strip() or None
    if mandatory or (model and not is_class_selector(model)) or (provider and provider != 'auto'):
        raise ValueError(ERROR)
    if provider and not model:
        raise ValueError(ERROR)
    return model, ('auto' if model else None)


def validate_text(*values):
    """Reject positive machine directives and explicit named-model obligations.

    A free-form historical observation is not a restriction. Authorship markers,
    exclusions and negated restrictions are deliberately not rewritten here.
    """
    for text in values:
        for line in (text or '').splitlines():
            line = _active_text(line)
            if _REQUIRED.search(line):
                raise ValueError(ERROR)
            if line.lstrip().startswith('<!-- implementer-models:'):
                continue
            for clause in re.split(r'(?<=[.!?;])\s+', line):
                if not _MODEL.search(clause):
                    continue
                # Negation applies to the directive, not any unrelated safety
                # sentence elsewhere on the same line.
                if re.search(r'\b(?:never|do not|must not)\s+(?:pin|require|restrict)', clause, re.IGNORECASE):
                    continue
                if re.search(r'\b(?:implementer models|forbidden overlaps|excluded models|exclude)\s*:', clause, re.IGNORECASE):
                    continue
                if re.search(r'\b(?:differ from|not be reviewed by|other than|distinct from|not (?:the )?same as)\b', clause, re.IGNORECASE):
                    continue
                if re.fullmatch(
                    r'\s*(?:the )?(?:old|legacy|previous|historical) '
                    r'(?:configuration|policy|instruction|card|review|run|session) '
                    r'(?:required|mandated|pinned)\s+' + _MODEL.pattern + r'(?:\s+only)?[.!?;]?\s*',
                    clause, re.IGNORECASE,
                ):
                    continue
                named_review = re.search(
                    r'(?:primary|independent|exact[- ]?head)[ -]*(?:opus|sonnet|fable|astra|luna|sol)\b'
                    r'|\breview\s*\((?:claude-|gpt-|grok-|gemini-)', clause, re.IGNORECASE)
                if _FORCE.search(clause) or named_review:
                    raise ValueError(ERROR)


def forwarded_text(text):
    """Retain superseded legacy directives as labelled evidence, not new policy."""
    lines = []
    for line in (text or '').splitlines():
        try:
            validate_text(line)
        except ValueError:
            line = _EVIDENCE + json.dumps(line, ensure_ascii=False)
        lines.append(line)
    return '\n'.join(lines)


def effective_selector(task):
    """Ignore legacy concrete restrictions without mutating stored author evidence."""
    model = str(getattr(task, 'model_override', None) or '').strip()
    return (model if is_class_selector(model) else 'class:standard'), 'auto'


def routing_view(task):
    """A next-admission view, never a replacement for the historical author row."""
    from copy import copy
    view = copy(task)
    view._routing_source = getattr(task, '_routing_source', task)
    model = str(getattr(task, 'model_override', None) or '').strip()
    view.model_override = model if is_class_selector(model) else None
    view.provider_override = 'auto' if view.model_override else None
    view.model_pin_mandatory = None
    return view


def validate_review(metadata):
    """New handoff metadata is policy, unlike past run/model evidence."""
    model, provider = validate_selector(metadata.get('review_model'), metadata.get('review_provider'),
                                        mandatory=metadata.get('pin_mandatory') or metadata.get('require_model'))
    return (model, provider, 'review_override') if model else None
