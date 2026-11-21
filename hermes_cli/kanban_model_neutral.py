"""Model-neutral Kanban specifications; execution receipts remain concrete.

Ingress rejects restrictions rather than silently accepting an ineffective pin.
Legacy routing reads use a separate projection and never rewrite author history.
Interactive sessions and the model catalog are deliberately outside this policy.
"""
from __future__ import annotations

import json
import re
from agent.model_routing import is_class_selector
from hermes_cli.models_catalog_static import _PROVIDER_MODELS

ERROR = 'Kanban is model-neutral: use a class/tier or capability requirement, not a concrete model/provider or mandatory pin'
# Match directives, not incidental named execution evidence or author exclusions.
_REQUIRED = re.compile(r'^\s*require_model\s*:\s*(?:true|yes|1)\s*$', re.IGNORECASE | re.MULTILINE)
def _model_names():
    """Offline catalog identity, not vendor-prefix guesses or live entitlement."""
    from hermes_cli.models_catalog_static import _PROVIDER_MODELS
    names = {name.lower().rsplit('/', 1)[-1] for rows in _PROVIDER_MODELS.values() for name in rows}
    # Catalog transport selectors (e.g. moa/default) are not model identities.
    # Otherwise ordinary board/profile references such as default/task become pins.
    names.difference_update({'default', 'auto'})
    # These are human shorthand for catalog families, not arbitrary claude-* tools.
    aliases = {alias for alias in ('opus', 'sonnet', 'fable', 'astra', 'luna', 'sol')
               if any(alias in re.split(r'[-.]', name) for name in names)}
    return '|'.join(re.escape(name) for name in sorted(names | aliases, key=lambda name: (-len(name), name)))


_MODEL = re.compile(r'(?:\b|(?<=independent)|(?<=primary))(?:' + _model_names() + r')(?![\w-]|\.[\w])', re.IGNORECASE)
# Bind the obligation to the route, rather than co-occurrence with unrelated
# words such as "required CI", "source only" or a tool-call bug report.
_QUALIFIER = r'(?:(?:the|a|an|only|model|provider|route|reviewer|review|independent|primary|exact[- ]head|on|to|with|use|using|run|by)\s+)*'
_ROUTE_NAME = r'(?:[\w-]+/)?' + _MODEL.pattern
_DIRECTIVE = re.compile(
    r'(?:\b(?:must|require|requires|required|mandatory|no fallback for)\s+'
    r'|(?:^|:)\s*(?:[-*]\s*)?(?:use|select|choose|pin|restrict)\s+)'
    + _QUALIFIER + _ROUTE_NAME
    + r'|' + _ROUTE_NAME + r'(?:/[\w-]+)?\s+(?:(?:review\s+|is\s+)?(?:required|mandatory|only)\b|must\s+be\s+used\b)',
    re.IGNORECASE,
)
# Provider identities are routing restrictions only in an obligation's route slot,
# not incidental provider/API implementation work.
_PROVIDER_NAME = r'\b(?:' + '|'.join(re.escape(p) for p in sorted(_PROVIDER_MODELS, key=lambda p: (-len(p), p))) + r')\b'
_PROVIDER_DIRECTIVE = re.compile(
    r'(?:\b(?:must|require|requires|required|mandatory|no fallback for)\s+'
    r'|(?:^|:)\s*(?:[-*]\s*)?(?:use|pin|restrict|select only|choose only)\s+)'
    + _QUALIFIER + _PROVIDER_NAME
    + r'(?:\s+(?:provider|route|reviewer))?(?=\s*(?:[.!?;,]|$|for (?:this|the) (?:review|task|card|run)\b))'
    + r'|(?:^|:)\s*' + _PROVIDER_NAME + r'\s+(?:(?:provider|route|reviewer)\s+)?'
    r'(?:(?:is\s+)?(?:required|mandatory|only)|must\s+be\s+used)'
    r'(?=\s*(?:[.!?;,]|$|for (?:this|the) (?:review|task|card|run)\b))',
    re.IGNORECASE,
)
_EVIDENCE = 'Historical run instruction (superseded and not active): '
_TEXT_GUIDANCE = ('; for superseded historical evidence only, put the unchanged line in a JSON string after '
                  + _EVIDENCE + '(do not quote active instructions)')
# Replace only the negated obligation phrase, never its surrounding clause: a
# trailing positive mandate must still be checked normally.
_NEGATED_OBLIGATION = re.compile(
    r'\b(?:no longer\s+(?:requires?|required|mandatory)|does? not\s+require|'
    r'non[- ]mandatory)\b', re.IGNORECASE)
_REMOVED_OBLIGATION = re.compile(
    r'^(\s*(?:[-*]\s*)?(?:remove|delete)\s+(?:the\s+)?(?:skill\s+)?'
    r'(?:text|instruction|wording)\s+that\s+)(?:requires?|mandates?)\b', re.IGNORECASE)


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
    if any(value is not None and not isinstance(value, str) for value in (model, provider)):
        raise ValueError(ERROR)
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
        for source_line in (text or '').splitlines():
            line = _active_text(source_line)
            if _REQUIRED.search(line):
                raise ValueError(f'{ERROR}; refused line: {source_line}{_TEXT_GUIDANCE}')
            # Comments carry authorship, not authority over text after their end.
            line = re.sub(r'<!-- implementer-models:.*?-->', '', line)
            # Inspect rendered inline identities, retaining the original stored bytes.
            line = re.sub(r'(`+|\*\*|__|\*|_)(.+?)\1', r'\2', line)
            for clause in re.split(r'(?<=[.!?;])\s+', line):
                clause = _NEGATED_OBLIGATION.sub('optional', clause)
                clause = _REMOVED_OBLIGATION.sub(r'\1mentions', clause)
                # Remove only the observed failed-route phrase, not a trailing
                # mandate ("...unavailable; use only <model>" still refuses).
                clause = re.sub(r'\bPinned route\s+' + _ROUTE_NAME + r'\s+is unavailable\b',
                                'Observed unavailable route', clause, flags=re.IGNORECASE)
                if not (_MODEL.search(clause) or _PROVIDER_DIRECTIVE.search(clause)):
                    continue
                # Mask only negative/exclusion wording up to a new positive
                # obligation, never the rest of a clause containing that mandate.
                clause = re.sub(
                    r'\b(?:(?:never|do not|must not)\s+(?:pin|require|restrict)'
                    r'|(?:implementer models|forbidden overlaps|excluded models|exclude)\s*:'
                    r'|differ from|not be reviewed by|other than|distinct from|not (?:the )?same as)'
                    r'.*?(?=\b(?:must(?!\s+not)|requires?|required|mandatory|'
                    r'no fallback for|(?:use|select|choose|pin|restrict)\s+(?:only\s+)?)\b|$)',
                    ' ', clause, flags=re.IGNORECASE)
                if re.fullmatch(
                    r'\s*(?:the )?(?:old|legacy|previous|historical) '
                    r'(?:configuration|policy|instruction|card|review|run|session) '
                    r'(?:required|mandated|pinned)\s+' + _MODEL.pattern + r'(?:\s+only)?[.!?;]?\s*',
                    clause, re.IGNORECASE,
                ):
                    continue
                # A bare requested-review title is operative; a sentence that
                # merely attributes a completed review is not. Do not scan for
                # "independent Opus" anywhere inside arbitrary provenance prose.
                named_review = re.fullmatch(
                    r'\s*(?:(?:primary|independent|exact[- ]?head)[ -]*)+'
                    + _MODEL.pattern + r'\s+(?:exacthead\s+)?review(?:\s+required)?'
                    r'(?:\s*[,;+].*)?[.!]?\s*', clause, re.IGNORECASE)
                required_review = re.search(
                    r'\b(?:needs?|requires?|must get)\s+(?:a\s+)?(?:fresh\s+)?'
                    r'(?:independent\s+)?review\s*\(\s*' + _ROUTE_NAME,
                    clause, re.IGNORECASE)
                if _DIRECTIVE.search(clause) or _PROVIDER_DIRECTIVE.search(clause) or named_review or required_review:
                    raise ValueError(f'{ERROR}; refused line: {source_line}{_TEXT_GUIDANCE}')


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


def forwarded_title(title, fallback):
    """Keep ordinary recovery titles; legacy mandates belong in body evidence."""
    try:
        validate_text(title)
    except ValueError:
        return fallback
    return title


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
