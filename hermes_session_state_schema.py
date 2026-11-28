"""SessionState v2: the one per-lineage session status object (design item 1). [daveey]

Spec: ``docs/designs/session-state-v2.md``; machine schema ``schemas/session_state_v2.schema.json`` (generated from
:data:`JSON_SCHEMA`; a test pins the committed file to it). The dataclasses below are the canonical Python shape:
the reducer builds a :class:`SessionState` and publishes ``to_dict()``; consumers read the dict.

Ownership: ``SessionStateStore`` (``hermes_session_state_store``) is the only writer. ``rev`` and ``computed_at``
are store-owned (never part of the digest that gates ``rev``); everything else is reducer output.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

VERSION = 2
BLOCKED_ON = ("human", "card", "ci", "clock", "deploy", "capacity", "agent", "unknown")
SECTIONS = ("needs_you", "stuck", "running", "waiting", "ready", "done")
MODES = ("running", "idle", "failed")
COLOURS = ("red", "amber", "blue", "light_blue", "purple", "green", "grey")
FRESHNESS = ("live", "stale", "unknown")
ACTIONS = ("unblock", "answer", "archive", "release")
STORE_OWNED = frozenset({"rev", "computed_at"})


@dataclass
class Identity:
    gateway: str
    profile: str
    lineage: str  # compression-lineage root session id: the object's key
    tip: str  # the lineage's live tip when computed


@dataclass
class Session:
    mode: str  # running | idle | failed
    turn: dict[str, Any] = field(default_factory=dict)  # {state, generation, reason?}
    processes: list[dict[str, Any]] = field(default_factory=list)  # [{id, state}]
    subagents: list[dict[str, Any]] = field(default_factory=list)  # [{id, state}]


@dataclass
class Card:
    key: str  # owner-qualified: "<board>/<id>" locally, the replica's key for a remote card
    id: str
    board: str
    gateway: str
    title: str
    lifecycle: str  # the card's status (todo/ready/running/blocked/scheduled/review/triage/done/archived/unknown)
    blocked_on: Optional[str]  # one of BLOCKED_ON; None when nothing blocks it (running, done)
    detail: str
    parents: list[str] = field(default_factory=list)  # parent keys (dependency closure members)
    linked: bool = True  # False: present only as a dependency of a linked card
    via: Optional[str] = None  # created | subscribed | None (closure-only)
    run: Optional[dict[str, Any]] = None  # {id, started_at}
    source: dict[str, Any] = field(default_factory=lambda: {"freshness": "live", "synced_at": None})
    block_episode: Optional[str] = None  # identity of the current block episode (its first blocking event)
    resolution: Optional[dict[str, Any]] = None  # the resolution recorded for block_episode, when any
    red: bool = False  # a live human ask or overdue triage that no resolution covers
    eta: Optional[float] = None


@dataclass
class Ask:
    id: str
    question: str
    card: Optional[str] = None
    choices: list[dict[str, Any]] = field(default_factory=list)
    state: str = "open"


@dataclass
class Resolution:
    request_id: str
    card: str
    action: str  # one of ACTIONS
    choice_id: Optional[str]
    label: Optional[str]
    by: str
    at: float
    delivery: str  # committed | failed
    episode: Optional[str] = None  # the block episode it resolved


@dataclass
class Header:
    status: str  # needs_you | stuck | running | waiting | attention | unknown | done | muted | idle
    colour: str  # one of COLOURS
    stripe: str  # the legacy presentation stripe the P1 adapter reduced
    reason_codes: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)  # every SECTIONS key
    pips: list[dict[str, Any]] = field(default_factory=list)  # [{section, count}] nonempty, in SECTIONS order


@dataclass
class SessionState:
    identity: Identity
    session: Session
    header: Header
    cards: dict[str, Card] = field(default_factory=dict)
    asks: list[Ask] = field(default_factory=list)
    resolutions: list[Resolution] = field(default_factory=list)
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)  # {name: {coverage, reason}}
    next_change_at: Optional[float] = None
    legacy: dict[str, Any] = field(default_factory=dict)  # shadow: the v1 envelope this was reduced from
    version: int = VERSION
    rev: int = 0
    computed_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_STR = {"type": "string"}
_NSTR = {"type": ["string", "null"]}
_NUM = {"type": "number"}
_NNUM = {"type": ["number", "null"]}
_OBJ = {"type": "object"}
_NOBJ = {"type": ["object", "null"]}
_ITEMS = {"type": "array", "items": _OBJ}
_CARD_KEYS = ["key", "id", "board", "gateway", "title", "lifecycle", "blocked_on", "detail", "parents", "linked",
              "via", "run", "source", "block_episode", "resolution", "red", "eta"]
_RESOLUTION_KEYS = ["request_id", "card", "action", "choice_id", "label", "by", "at", "delivery", "episode"]

JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:hermes:session-state:v2",
    "title": "SessionState v2: one status object per session lineage",
    "type": "object",
    "required": ["version", "identity", "rev", "computed_at", "next_change_at", "session", "cards", "asks",
                 "resolutions", "header", "sources", "legacy"],
    "additionalProperties": False,
    "properties": {
        "version": {"const": VERSION},
        "rev": {"type": "integer", "minimum": 0},
        "computed_at": _NUM,
        "next_change_at": _NNUM,
        "identity": {"type": "object", "required": ["gateway", "profile", "lineage", "tip"],
                     "additionalProperties": False,
                     "properties": {k: {"type": "string", "minLength": 1}
                                    for k in ("gateway", "profile", "lineage", "tip")}},
        "session": {"type": "object", "required": ["mode", "turn", "processes", "subagents"],
                    "additionalProperties": False,
                    "properties": {"mode": {"enum": list(MODES)}, "turn": _OBJ, "processes": _ITEMS,
                                   "subagents": _ITEMS}},
        "cards": {"type": "object", "additionalProperties": {
            "type": "object", "additionalProperties": False, "required": _CARD_KEYS,
            "properties": {
                "key": _STR, "id": _STR, "board": _STR, "gateway": _STR, "title": _STR, "lifecycle": _STR,
                "blocked_on": {"enum": [*BLOCKED_ON, None]}, "detail": _STR,
                "parents": {"type": "array", "items": _STR}, "linked": {"type": "boolean"}, "via": _NSTR,
                "run": _NOBJ, "block_episode": _NSTR, "resolution": _NOBJ, "red": {"type": "boolean"},
                "eta": _NNUM,
                "source": {"type": "object", "required": ["freshness", "synced_at"],
                           "properties": {"freshness": {"enum": list(FRESHNESS)}, "synced_at": _NNUM}}}}},
        "asks": {"type": "array", "items": {
            "type": "object", "required": ["id", "question", "card", "choices", "state"],
            "additionalProperties": False,
            "properties": {"id": _STR, "question": _STR, "card": _NSTR, "choices": _ITEMS, "state": _STR}}},
        "resolutions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": _RESOLUTION_KEYS,
            "properties": {"request_id": _STR, "card": _STR, "action": {"enum": list(ACTIONS)},
                           "choice_id": _NSTR, "label": _NSTR, "by": _STR, "at": _NUM,
                           "delivery": {"enum": ["committed", "failed"]}, "episode": _NSTR}}},
        "header": {"type": "object", "required": ["status", "colour", "stripe", "reason_codes", "counts", "pips"],
                   "additionalProperties": False,
                   "properties": {"status": _STR, "colour": {"enum": list(COLOURS)}, "stripe": _STR,
                                  "reason_codes": {"type": "array", "items": _STR},
                                  "counts": {"type": "object", "additionalProperties": {"type": "integer"}},
                                  "pips": _ITEMS}},
        "sources": {"type": "object", "additionalProperties": {
            "type": "object", "required": ["coverage"],
            "properties": {"coverage": {"enum": ["complete", "unknown"]}, "reason": _NSTR}}},
        "legacy": _OBJ,
    },
}

_TYPES = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "object": dict, "array": list,
          "null": type(None)}


def _type_ok(value: Any, name: str) -> bool:
    if name in ("number", "integer") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES[name])


def _check_object(value: dict, schema: dict, path: str, out: list[str]) -> None:
    props = schema.get("properties", {})
    out.extend(f"{path}.{key}: required" for key in schema.get("required", ()) if key not in value)
    extra = schema.get("additionalProperties", True)
    for key, item in value.items():
        if key in props:
            _check(item, props[key], f"{path}.{key}", out)
        elif extra is False:
            out.append(f"{path}.{key}: unexpected")
        elif isinstance(extra, dict):
            _check(item, extra, f"{path}.{key}", out)


def _check(value: Any, schema: dict, path: str, out: list[str]) -> None:
    """The JSON Schema subset :data:`JSON_SCHEMA` uses (type, const, enum, required, properties,
    additionalProperties, items, minimum, minLength); no external dependency."""
    if "const" in schema and value != schema["const"]:
        out.append(f"{path}: expected {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: {value!r} not in {schema['enum']!r}")
    types = schema.get("type")
    if types is not None:
        names = types if isinstance(types, list) else [types]
        if not any(_type_ok(value, n) for n in names):
            out.append(f"{path}: expected {types}")
            return
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        out.append(f"{path}: shorter than {schema['minLength']}")
    if "minimum" in schema and isinstance(value, (int, float)) and not isinstance(value, bool) \
            and value < schema["minimum"]:
        out.append(f"{path}: below {schema['minimum']}")
    if isinstance(value, dict):
        _check_object(value, schema, path, out)
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _check(item, schema["items"], f"{path}[{index}]", out)


def validate(obj: Any) -> list[str]:
    """Schema violations of one SessionState v2 dict (empty = valid)."""
    out: list[str] = []
    _check(obj, JSON_SCHEMA, "$", out)
    return out
