"""Canonical static manifest of the LEGACY profile-sessions wire payloads. [daveey]

Declares, without inventing a new schema, what the unchanged legacy serializers
emit on ``GET /api/profiles/sessions`` (list), ``/sessions/sidebar`` and
``/sessions/stamps``: row projections, the route selectors (query parameters)
and the response envelopes. A fact producer's ReducerSpec reads these names;
it must not redefine an open legacy mapping as an unrestricted new dict.

Sources the declarations were extracted from (drift-tested in
``tests/hermes_cli/test_sessions_projection_manifest*.py``):

* ``hermes_state_common.SCHEMA_SQL`` ``sessions`` table (PRAGMA table_info);
* ``SessionDB.list_sessions_rich`` / ``_list_row`` / ``_session_row_dict``,
  ``_SESSION_COMPACT_EXCLUDED`` (compact_rows), ``hermes_state_session_list.
  project_compression_tips``, ``hermes_state_local.annotate_local_lineages``,
  ``hermes_state_outcome.stamp_outcome_rows`` and ``session_unread``;
* route stamps ``web_routers.profiles._tag_rows`` and
  ``session_listing.stamp_automated``;
* ``web_server_gateway._strip_session_list_rows`` (every non-full row);
* the seven activity annotator families (kanban, goal, cron, background,
  turn, subagent, review).

Type expressions (``check``): ``string``, ``integer``, ``number`` (int or float,
never bool), ``boolean``, ``null``, ``true``, ``const:<text>``,
``enum:<a>,<b>``, ``array<T>``, ``map<T>`` (string keys), ``shape:<Name>``
(a closed SHAPES entry) and ``open:<Name>`` (an inherent legacy open mapping
whose canonical type lives at OPEN_MAPPINGS[name]; an enumerated discriminator
is checked only when allowed values are declared, otherwise any dict is retained
as legacy passthrough, including unknown keys). Alternatives are joined with ``|``.
Digit-only constants are numeric (``const:0`` rejects strings and booleans).

This is a field/type inventory, not a serializer or JSON key-order specification.
In particular sidebar errors/storage precede optional profiles_failed on the
wire; use the canonical assembler to preserve insertion order. Use
response_violations with the bound selector, not the unbound response union,
to enforce compact versus full privacy. Broadcast eligibility applies only
within an authorized profile/selector scope, never across arbitrary consumers.

SQLite affinities are declared types, not guarantees: a legacy store holding an
off-type value in a column is emitted unchanged by the legacy reader and will
fail ``check``; that is drift to report, not to coerce.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping

ALWAYS = "always"
CONDITIONAL = "conditional"


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str
    presence: str  # ALWAYS | CONDITIONAL
    source: str
    absent: str = ""  # when a CONDITIONAL field is absent (or ALWAYS field caveats)


@dataclass(frozen=True)
class Projection:
    name: str
    fields: tuple[FieldSpec, ...]
    broadcast: bool  # eligible within authorized profile/selector scope (not global)
    served_by: tuple[str, ...]
    note: str = ""

    def by_name(self) -> dict[str, FieldSpec]:
        return {spec.name: spec for spec in self.fields}


@dataclass(frozen=True)
class SelectorParam:
    name: str
    type: str
    default: Any
    required: bool = False
    constraints: tuple[tuple[str, Any], ...] = ()
    validated: str = ""


REQUIRED = object()  # selector default sentinel for required query params

# ── sessions table (SCHEMA_SQL) ──────────────────────────────────────────
# (name, declared sqlite type, notnull, PRAGMA default text, pk, JSON type)
SESSIONS_TABLE_COLUMNS: tuple[tuple[str, str, int, str | None, int, str], ...] = (
    ("id", "TEXT", 0, None, 1, "string"),
    ("runtime_revision", "INTEGER", 1, "0", 0, "integer"),
    ("runtime_generation", "INTEGER", 1, "0", 0, "integer"),
    ("source", "TEXT", 1, None, 0, "string"),
    ("created_source", "TEXT", 0, None, 0, "string|null"),
    ("user_id", "TEXT", 0, None, 0, "string|null"),
    ("session_key", "TEXT", 0, None, 0, "string|null"),
    ("chat_id", "TEXT", 0, None, 0, "string|null"),
    ("chat_type", "TEXT", 0, None, 0, "string|null"),
    ("thread_id", "TEXT", 0, None, 0, "string|null"),
    ("display_name", "TEXT", 0, None, 0, "string|null"),
    ("origin_json", "TEXT", 0, None, 0, "string|null"),
    ("expiry_finalized", "INTEGER", 0, "0", 0, "integer|null"),
    ("model", "TEXT", 0, None, 0, "string|null"),
    ("model_config", "TEXT", 0, None, 0, "string|null"),
    ("system_prompt", "TEXT", 0, None, 0, "string|null"),
    ("system_prompt_hash", "TEXT", 0, None, 0, "string|null"),
    ("parent_session_id", "TEXT", 0, None, 0, "string|null"),
    ("started_at", "REAL", 1, None, 0, "number"),
    ("ended_at", "REAL", 0, None, 0, "number|null"),
    ("end_reason", "TEXT", 0, None, 0, "string|null"),
    ("message_count", "INTEGER", 0, "0", 0, "integer|null"),
    ("tool_call_count", "INTEGER", 0, "0", 0, "integer|null"),
    ("input_tokens", "INTEGER", 0, "0", 0, "integer|null"),
    ("output_tokens", "INTEGER", 0, "0", 0, "integer|null"),
    ("cache_read_tokens", "INTEGER", 0, "0", 0, "integer|null"),
    ("cache_write_tokens", "INTEGER", 0, "0", 0, "integer|null"),
    ("reasoning_tokens", "INTEGER", 0, "0", 0, "integer|null"),
    ("cwd", "TEXT", 0, None, 0, "string|null"),
    ("git_branch", "TEXT", 0, None, 0, "string|null"),
    ("git_repo_root", "TEXT", 0, None, 0, "string|null"),
    ("git_metadata_generation", "INTEGER", 1, "0", 0, "integer"),
    ("billing_provider", "TEXT", 0, None, 0, "string|null"),
    ("billing_base_url", "TEXT", 0, None, 0, "string|null"),
    ("billing_mode", "TEXT", 0, None, 0, "string|null"),
    ("estimated_cost_usd", "REAL", 0, None, 0, "number|null"),
    ("actual_cost_usd", "REAL", 0, None, 0, "number|null"),
    ("cost_status", "TEXT", 0, None, 0, "string|null"),
    ("cost_source", "TEXT", 0, None, 0, "string|null"),
    ("pricing_version", "TEXT", 0, None, 0, "string|null"),
    ("title", "TEXT", 0, None, 0, "string|null"),
    ("title_source", "TEXT", 0, None, 0, "string|null"),
    ("last_activity_at", "REAL", 0, None, 0, "number|null"),
    ("last_activity_description", "TEXT", 0, None, 0, "string|null"),
    ("last_activity_provenance", "TEXT", 0, None, 0, "string|null"),
    ("api_call_count", "INTEGER", 0, "0", 0, "integer|null"),
    ("handoff_state", "TEXT", 0, None, 0, "string|null"),
    ("handoff_platform", "TEXT", 0, None, 0, "string|null"),
    ("handoff_error", "TEXT", 0, None, 0, "string|null"),
    ("compression_failure_cooldown_until", "REAL", 0, None, 0, "number|null"),
    ("compression_failure_error", "TEXT", 0, None, 0, "string|null"),
    ("compression_fallback_streak", "INTEGER", 1, "0", 0, "integer"),
    ("compression_ineffective_count", "INTEGER", 1, "0", 0, "integer"),
    ("compression_recovery_deadline", "REAL", 0, None, 0, "number|null"),
    ("compression_overload_streak", "INTEGER", 1, "0", 0, "integer"),
    ("profile_name", "TEXT", 0, None, 0, "string|null"),
    ("transport_profile", "TEXT", 0, None, 0, "string|null"),
    ("rewind_count", "INTEGER", 1, "0", 0, "integer"),
    ("archived", "INTEGER", 1, "0", 0, "integer"),
    ("auto_archived", "INTEGER", 1, "0", 0, "integer"),
    ("pinned", "INTEGER", 1, "0", 0, "integer"),
    ("hidden", "INTEGER", 1, "0", 0, "integer"),
    ("last_read_at", "REAL", 0, None, 0, "number|null"),
    ("tool_names", "TEXT", 0, None, 0, "string|null"),
    ("transcript_revision", "INTEGER", 1, "0", 0, "integer"),
    ("outcome_json", "TEXT", 0, None, 0, "string|null"),
    ("outcome_at", "REAL", 0, None, 0, "number|null"),
    ("muted", "INTEGER", 1, "0", 0, "integer"),
)

# ``SessionDB._SESSION_COMPACT_EXCLUDED``: absent from compact_rows SQL.
COMPACT_EXCLUDED_COLUMNS = frozenset({"system_prompt", "system_prompt_hash", "git_metadata_generation"})
# ``web_server_gateway._SESSION_LIST_HEAVY_FIELDS``: popped from every non-full row.
LIST_STRIPPED_FIELDS = ("system_prompt", "model_config")
# Keys ``project_compression_tips`` copies from the live tip onto the root's row.
COMPRESSION_TIP_FIELDS = (
    "id", "ended_at", "end_reason", "message_count", "tool_call_count", "title", "last_active",
    "preview", "model", "system_prompt", "cwd", "git_branch", "git_repo_root", "outcome_json")
# Internal SQL columns the serializers pop; never on the wire.
POPPED_INTERNAL_KEYS = frozenset({"_preview_raw", "_effective_last_active", "_system_prompt_resolved"})
# ``sessions_projection.SESSION_STAMP_COLUMNS`` (also legacy ``_SESSION_STAMP_COLUMNS``).
STAMP_FIXED_COLUMNS = ("cwd", "git_branch", "git_repo_root", "outcome", "muted")
SIDEBAR_SLICES = ("recents", "cron", "messaging")
CAPS = {"kanban_waiting_on": 50, "kanban_blocked_on": 50, "kanban_external_cards": 10,
        "background_processes": 8}

# Route-stamped columns override the stored integer flags with booleans.
_ROW_OVERRIDES = {
    "archived": FieldSpec("archived", "boolean", ALWAYS, "web_routers.profiles._tag_rows: bool(archived)"),
    "pinned": FieldSpec("pinned", "boolean", ALWAYS, "web_routers.profiles._tag_rows: bool(pinned)"),
    "muted": FieldSpec("muted", "boolean", ALWAYS, "hermes_state_outcome.stamp_outcome_rows: bool(muted)"),
    "tool_names": FieldSpec(
        "tool_names", "string|null", ALWAYS, "sessions.tool_names (raw column)",
        "list SQL never resolves it: the value may be a system_prompts hash reference, not tool names"),
    "system_prompt": FieldSpec(
        "system_prompt", "string|null", ALWAYS, "COALESCE(system_prompts.prompt, sessions.system_prompt)",
        "full only; projected from the compression tip when the row is a projected root"),
    "model_config": FieldSpec("model_config", "string|null", ALWAYS,
                              "sessions.model_config (raw JSON text, unparsed)", "full only"),
}


def _column_field(column) -> FieldSpec:
    name, _sqlite, _notnull, _default, _pk, json_type = column
    return _ROW_OVERRIDES.get(name) or FieldSpec(name, json_type, ALWAYS, f"sessions.{name}")


LIST_DERIVED_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("preview", "string", ALWAYS, "list_sessions_rich: _shape_preview(_preview_raw)",
              "'' when no eligible first user message"),
    FieldSpec("last_active", "number", ALWAYS, "list_sessions_rich: _sql_session_last_active"),
    FieldSpec("unread", "boolean", ALWAYS, "SessionDB.session_unread"),
    FieldSpec("outcome", "open:SessionOutcome|null", ALWAYS,
              "stamp_outcome_rows: _parse(outcome_json)", "null when unset/invalid/unknown kind"),
    FieldSpec("_reset_from", "string", CONDITIONAL, "_session_row_dict: model_config._reset_from (stripped)",
              "absent unless model_config JSON holds a nonblank string"),
    FieldSpec("_branched_from", "string", CONDITIONAL, "_session_row_dict: model_config._branched_from",
              "absent unless model_config JSON holds a nonblank string"),
    FieldSpec("_lineage_root_id", "string", CONDITIONAL, "project_compression_tips",
              "absent unless a compression root was projected onto a different live tip"),
    FieldSpec("_lineage_ids", "array<string>", CONDITIONAL,
              "project_compression_tips chain / hermes_state_local.annotate_local_lineages",
              "absent unless compression-projected or a local reset lineage head"),
    FieldSpec("continuation_kind", "const:compression", CONDITIONAL, "project_compression_tips",
              "present exactly with _lineage_root_id"),
)

ROUTE_STAMP_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("profile", "string", ALWAYS, "web_routers.profiles._tag_rows"),
    FieldSpec("is_default_profile", "boolean", ALWAYS, "web_routers.profiles._tag_rows"),
    FieldSpec("is_active", "boolean", ALWAYS, "web_routers.profiles._tag_rows: ended_at None and <300s"),
    FieldSpec("automated", "boolean", ALWAYS, "session_listing.stamp_automated"),
)

COMPACT_ONLY_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("model_provider", "string|null", ALWAYS,
              "_strip_session_list_rows: SessionDB.session_gateway_runtime(row)['provider'] or None",
              "always set because compact rows carry model_config/billing_provider before stripping"),
)

_EXECUTION_OBSERVATIONS = FieldSpec(
    "execution_observations", "array<open:ExecutionObservationV1>", CONDITIONAL,
    "kanban count_snapshot/_merge + turn_process_execution_observations.replace",
    "absent when no family has observations (kanban keeps [] while kanban_active is truthy)")


def _flag(name, family, absent="absent otherwise"):
    return FieldSpec(name, "true", CONDITIONAL, f"{family} annotator", absent)


def _count(name, source, absent="absent when zero"):
    return FieldSpec(name, "integer", CONDITIONAL, source, absent)


_KANBAN = "kanban_session_activity.annotate_kanban_activity"
ANNOTATION_FAMILIES: dict[str, tuple[FieldSpec, ...]] = {
    "kanban": (
        FieldSpec("kanban_observed_at", "number", CONDITIONAL, _KANBAN, "absent when kanban_unknown"),
        _flag("kanban_unknown", "kanban", "present only when the kanban read failed/was unavailable"),
        *(_count(name, _KANBAN, "absent when zero or kanban_unknown") for name in (
            "kanban_active", "kanban_blocked", "kanban_unseen", "kanban_attention", "kanban_remote_stale",
            "kanban_held")),
        _count("kanban_parked", _KANBAN, "present (even 0) iff kanban_active is truthy"),
        _count("kanban_stalled", _KANBAN, "absent unless nonzero and kanban_active"),
        FieldSpec("kanban_waiting_on", "array<string>", CONDITIONAL, _KANBAN, "absent when empty; capped"),
        FieldSpec("kanban_blocked_on", "array<string>", CONDITIONAL,
                  "kanban_activity_identity.stamp_blocked_owners", "absent when empty; capped"),
        FieldSpec("needs_you", "array<shape:NeedsYouEntry>", CONDITIONAL, "kanban_open_asks.stamp_needs_you",
                  "absent when empty"),
        *(_count(name, "kanban_external_activity.stamp_rows") for name in (
            "kanban_external", "kanban_external_unknown", "kanban_external_down")),
        FieldSpec("kanban_external_cards", "array<shape:ExternalCard>", CONDITIONAL,
                  "kanban_external_activity.stamp_rows", "absent when empty; capped"),
        _EXECUTION_OBSERVATIONS,
    ),
    "goal": (_flag("goal_active", "goal_session_activity.annotate_goal_activity"),),
    "cron": (_flag("cron_active", "cron_session_activity.annotate_cron_activity"),),
    "background": (
        _count("background_active", "process_session_activity.annotate_background_activity"),
        FieldSpec("background_processes", "array<shape:BackgroundProcess>", CONDITIONAL,
                  "process_session_activity.annotate_background_activity",
                  "present exactly with background_active; oldest first; capped"),
        _EXECUTION_OBSERVATIONS,
    ),
    "turn": (_flag("turn_live", "turn_session_activity.annotate_turn_activity"), _EXECUTION_OBSERVATIONS),
    "subagent": (_EXECUTION_OBSERVATIONS,),
    "review": (
        FieldSpec("review_wait", "enum:stuck,parked", CONDITIONAL, "review_session_activity.annotate_review_wait",
                  "present exactly with review_wait_pr"),
        FieldSpec("review_wait_pr", "string", CONDITIONAL, "review_session_activity.annotate_review_wait",
                  "https://github.com/<owner>/<repo>/pull/<n>"),
    ),
}
# Legacy call orders (``sessions_projection`` module doc).
LIST_ANNOTATION_ORDER = ("kanban", "goal", "cron", "background", "turn", "subagent", "review")
SIDEBAR_ANNOTATION_ORDER = ("subagent", "kanban", "goal", "cron", "background", "turn", "review")
STAMPS_ANNOTATION_ORDER = SIDEBAR_ANNOTATION_ORDER


def annotation_fields() -> tuple[FieldSpec, ...]:
    """Union of the seven families; a field shared by families must be declared identically."""
    merged: dict[str, FieldSpec] = {}
    for family in LIST_ANNOTATION_ORDER:
        for spec in ANNOTATION_FAMILIES[family]:
            if merged.setdefault(spec.name, spec) != spec:
                raise ValueError(f"annotation field {spec.name} declared inconsistently")
    return tuple(merged.values())


def _row_fields(columns: Iterable, extra: Iterable[FieldSpec]) -> tuple[FieldSpec, ...]:
    out = [_column_field(column) for column in columns]
    return tuple(out + list(extra) + list(LIST_DERIVED_FIELDS) + list(ROUTE_STAMP_FIELDS) + list(annotation_fields()))


LEGACY_COMPACT_SESSION_PROJECTION = Projection(
    "LegacyCompactSessionProjection",
    _row_fields((c for c in SESSIONS_TABLE_COLUMNS
                 if c[0] not in COMPACT_EXCLUDED_COLUMNS and c[0] not in LIST_STRIPPED_FIELDS),
                COMPACT_ONLY_FIELDS),
    broadcast=True,
    served_by=("sessions(full=False).sessions[]", "sessions_sidebar.<slice>.sessions[]"),
    note="Broadcast only within the authorized profile/selector scope; not a global feed.",
)
LEGACY_FULL_SESSION_PROJECTION = Projection(
    "LegacyFullSessionProjection",
    _row_fields(SESSIONS_TABLE_COLUMNS, ()),
    broadcast=False,
    served_by=("sessions(full=True).sessions[]",),
    note="Only the explicit full=True list selector; carries system_prompt/model_config and the internal "
         "system_prompt_hash/git_metadata_generation columns. Never a generic broadcast.",
)
LEGACY_SESSION_STAMP_PROJECTION = Projection(
    "LegacySessionStampProjection",
    (FieldSpec("id", "string", ALWAYS, "SessionDB.get_session"),
     FieldSpec("profile", "string", ALWAYS, "get_profiles_session_stamps profile name"),
     FieldSpec("cwd", "string|null", ALWAYS, "sessions.cwd"),
     FieldSpec("git_branch", "string|null", ALWAYS, "sessions.git_branch"),
     FieldSpec("git_repo_root", "string|null", ALWAYS, "sessions.git_repo_root"),
     FieldSpec("outcome", "open:SessionOutcome|null", ALWAYS, "stamp_outcome_rows"),
     FieldSpec("muted", "boolean", ALWAYS, "stamp_outcome_rows"),
     *annotation_fields()),
    broadcast=True,
    served_by=("sessions_stamps.sessions[]",),
    note="Broadcast only within authorized profile/selector scope. Annotation keys appear only when NEW "
         "relative to the pre-annotation row; private (_*) keys never.",
)
PROJECTIONS = {p.name: p for p in (
    LEGACY_COMPACT_SESSION_PROJECTION, LEGACY_FULL_SESSION_PROJECTION, LEGACY_SESSION_STAMP_PROJECTION)}

# ── closed nested shapes and documented open mappings ────────────────────
SHAPES: dict[str, tuple[FieldSpec, ...]] = {
    "NeedsYouEntry": (FieldSpec("kind", "const:card", ALWAYS, "kanban_open_asks.open_human_asks"),
                      FieldSpec("ref", "string", ALWAYS, "owner-qualified card key, '' when padded"),
                      FieldSpec("question", "const:", ALWAYS, "always '' (deliberately narrowed)")),
    "ExternalCard": (FieldSpec("task_id", "string", ALWAYS, "kanban_external_activity"),
                     FieldSpec("title", "string", ALWAYS, "tasks.title or ''"),
                     FieldSpec("external", "open:ExternalCardState", ALWAYS, "kanban_external_watch.card_states"),
                     FieldSpec("eta", "shape:ExternalCardEta", CONDITIONAL, "kanban_external_activity._eta_waits",
                               "absent unless the card waits on a ci / deploy / clock park")),
    "ExternalCardEta": (FieldSpec("wait_kind", "string", ALWAYS, "kanban_wait_info.eta_fields: ci | deploy | clock"),
                        FieldSpec("waited_since", "number|null", ALWAYS, "the park time, epoch seconds"),
                        FieldSpec("expected_s", "number|null", ALWAYS,
                                  "clock: wake_at - park time; ci/deploy: median of the last 20 finished waits of "
                                  "the same kind and target; null below 3 samples"),
                        FieldSpec("estimate_source", "string", ALWAYS, "short text naming the basis")),
    "BackgroundProcess": (FieldSpec("id", "string", ALWAYS, "process registry id"),
                          FieldSpec("command", "string", ALWAYS, "redacted, <=160 chars"),
                          FieldSpec("started_at", "number", CONDITIONAL, "_detail", "absent unless >0"),
                          FieldSpec("pid", "integer", CONDITIONAL, "_detail", "absent unless >0")),
    "ProfileError": (FieldSpec("profile", "string", ALWAYS, "_read_profile_db"),
                     FieldSpec("error", "string", ALWAYS, "str(exc)")),
    "ProfileFailure": (FieldSpec("failed", "true", ALWAYS, "assemble_sidebar"),
                       FieldSpec("retry", "true", ALWAYS, "assemble_sidebar"),
                       FieldSpec("error", "string", ALWAYS, "assemble_sidebar")),
    "UsageTotals": (FieldSpec("tokens", "integer", ALWAYS, "SessionDB.usage_totals"),
                    FieldSpec("cost_usd", "number", ALWAYS, "SessionDB.usage_totals")),
}
OPEN_MAPPINGS = {
    "SessionOutcome": ("hermes_state_outcome (validate_declaration/_outcome); discriminator kind in "
                       "OUTCOME_KINDS", "kind", ("done", "decision", "waiting", "running", "muted", "unresolved")),
    "ExecutionObservationV1": ("hermes_cli.execution_observations.project() v1 output; discriminator "
                               "source", "source", None),
    "ExternalCardState": ("kanban_external_watch.card_states()[tid]['external'] plus 'state'", "state", None),
}

# ── selectors (FastAPI query parameters, exactly as selector_for binds them) ──
SELECTORS: dict[str, tuple[SelectorParam, ...]] = {
    "sessions": (
        SelectorParam("limit", "integer", 20, constraints=(("ge", 0), ("le", 500))),
        SelectorParam("offset", "integer", 0, constraints=(("ge", 0),)),
        SelectorParam("min_messages", "integer", 0),
        SelectorParam("archived", "string", "exclude", validated="enum:exclude,only,include"),
        SelectorParam("order", "string", "recent", validated="enum:created,recent"),
        SelectorParam("profile", "string", "all"),
        SelectorParam("source", "string|null", None),
        SelectorParam("sources", "string|null", None, validated="csv"),
        SelectorParam("exclude_sources", "string|null", None, validated="csv"),
        SelectorParam("full", "boolean", False, validated="True selects LegacyFullSessionProjection"),
        SelectorParam("automated", "string", "include", validated="enum:include,exclude"),
    ),
    "sessions_stamps": (
        SelectorParam("ids", "string", REQUIRED, required=True, constraints=(("max_length", 8000),),
                      validated="csv, deduped, first 100"),
        SelectorParam("profile", "string", "default"),
    ),
    "sessions_sidebar": (
        SelectorParam("recents_profile", "string", "all"),
        SelectorParam("recents_limit", "integer", 20, validated="clamped 1..500"),
        SelectorParam("recents_exclude", "string|null", None, validated="csv"),
        SelectorParam("cron_limit", "integer", 50, validated="clamped 1..500"),
        SelectorParam("messaging_limit", "integer", 100, validated="clamped 1..500"),
        SelectorParam("messaging_exclude", "string|null", None, validated="csv"),
    ),
}
SELECTOR_ROUTES = {"sessions": "/api/profiles/sessions", "sessions_stamps": "/api/profiles/sessions/stamps",
                   "sessions_sidebar": "/api/profiles/sessions/sidebar"}

# ── response envelopes ────────────────────────────────────────────────────
_ERRORS = FieldSpec("errors", "array<shape:ProfileError>", ALWAYS, "per-profile read errors")
_STORAGE = FieldSpec("storage", "map<const:corrupt>", ALWAYS, "_corrupt_profile_stores")
_SLICE_FAILED = (FieldSpec("profiles_failed", "map<shape:ProfileFailure>", CONDITIONAL, "assemble_sidebar",
                           "absent unless some retryable profile failed"),
                 FieldSpec("errors", "array<shape:ProfileError>", CONDITIONAL, "assemble_sidebar",
                           "present exactly with profiles_failed"))
_ROWS = "array<shape:LegacyCompactSessionProjection>"
_FAILED = (FieldSpec("failed", "true", ALWAYS, "assemble_sidebar"), FieldSpec("retry", "true", ALWAYS, ""),
           FieldSpec("errors", "array<shape:ProfileError>", ALWAYS, "retryable errors"))
SHAPES.update({
    "SidebarRecents": (FieldSpec("sessions", _ROWS, ALWAYS, ""),
                       FieldSpec("profiles_truncated", "map<boolean>", ALWAYS, "len(rows) >= cap"),
                       FieldSpec("profiles_usage", "map<shape:UsageTotals>", ALWAYS, ""), *_SLICE_FAILED),
    "SidebarCron": (FieldSpec("sessions", _ROWS, ALWAYS, ""), *_SLICE_FAILED),
    "SidebarMessaging": (FieldSpec("sessions", _ROWS, ALWAYS, ""),
                         FieldSpec("total", "integer", ALWAYS, "pre-window row count, NOT a SQL total"),
                         *_SLICE_FAILED),
    "FailedRecents": (*_FAILED, FieldSpec("profiles_truncated", "map<boolean>", ALWAYS, "always {}"),
                      FieldSpec("profiles_usage", "map<shape:UsageTotals>", ALWAYS, "always {}")),
    "FailedSlice": _FAILED,
    "FailedMessaging": (*_FAILED, FieldSpec("total", "const:0", ALWAYS, "always 0")),
})
for _projection in PROJECTIONS.values():
    SHAPES[_projection.name] = _projection.fields

RESPONSES: dict[str, tuple[FieldSpec, ...]] = {
    "sessions": (
        FieldSpec("sessions", "array<shape:LegacyCompactSessionProjection>|array<shape:LegacyFullSessionProjection>",
                  ALWAYS, "full selector picks the projection"),
        FieldSpec("total", "integer", ALWAYS, "sum(profile_totals)"),
        FieldSpec("profile_totals", "map<integer>", ALWAYS, "SessionDB.session_count per profile read"),
        FieldSpec("limit", "integer", ALWAYS, "selector echo"),
        FieldSpec("offset", "integer", ALWAYS, "selector echo"),
        _ERRORS, _STORAGE),
    "sessions_sidebar": (
        FieldSpec("recents", "shape:SidebarRecents|shape:FailedRecents", ALWAYS, "assemble_sidebar"),
        FieldSpec("cron", "shape:SidebarCron|shape:FailedSlice", ALWAYS, "assemble_sidebar"),
        FieldSpec("messaging", "shape:SidebarMessaging|shape:FailedMessaging", ALWAYS, "assemble_sidebar"),
        _ERRORS, _STORAGE,
        FieldSpec("profiles_failed", "map<shape:ProfileFailure>", CONDITIONAL, "assemble_sidebar",
                  "absent on whole-scan failure or when no profile failed")),
    "sessions_stamps": (
        FieldSpec("sessions", "array<shape:LegacySessionStampProjection>", ALWAYS, "assemble_stamps"),),
}


# ── checking ──────────────────────────────────────────────────────────────
def _split(expr: str, sep: str = "|") -> list[str]:
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(expr):
        depth += (ch == "<") - (ch == ">")
        if ch == sep and depth == 0:
            parts.append(expr[start:i])
            start = i + 1
    return parts + [expr[start:]]


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _one(value, expr: str) -> bool:
    simple = {"string": lambda v: isinstance(v, str), "boolean": lambda v: isinstance(v, bool),
              "integer": lambda v: isinstance(v, int) and not isinstance(v, bool), "number": _number,
              "null": lambda v: v is None, "true": lambda v: v is True}
    if expr in simple:
        return simple[expr](value)
    head, _, rest = expr.partition(":")
    if head == "const":
        if rest.isdigit():
            return _number(value) and value == int(rest)
        return isinstance(value, str) and value == rest
    if head == "enum":
        return isinstance(value, str) and value in rest.split(",")
    if head == "shape":
        return isinstance(value, dict) and not shape_violations(value, SHAPES[rest])
    if head == "open":
        _doc, key, allowed = OPEN_MAPPINGS[rest]
        return isinstance(value, dict) and (allowed is None or value.get(key) in allowed)
    for wrapper, container in (("array<", list), ("map<", dict)):
        if expr.startswith(wrapper) and expr.endswith(">"):
            inner = expr[len(wrapper):-1]
            items = value if container is list else (value.values() if isinstance(value, dict) else ())
            return isinstance(value, container) and all(
                check(item, inner) for item in items) and (container is list or all(isinstance(k, str) for k in value))
    raise ValueError(f"unknown manifest type expression {expr!r}")


def check(value: Any, expr: str) -> bool:
    """Whether ``value`` satisfies the manifest type expression ``expr``."""
    return any(_one(value, alt) for alt in _split(expr))


def shape_violations(value: Mapping[str, Any], fields: Iterable[FieldSpec]) -> list[str]:
    """Closed-shape check: unknown keys, missing ALWAYS keys and type mismatches (empty = valid)."""
    specs = {spec.name: spec for spec in fields}
    out = [f"unknown field {key!r}" for key in value if key not in specs]
    for name, spec in specs.items():
        if name not in value:
            if spec.presence == ALWAYS:
                out.append(f"missing field {name!r}")
        elif not check(value[name], spec.type):
            out.append(f"{name!r}={value[name]!r} is not {spec.type}")
    return out


def row_violations(row: Mapping[str, Any], projection: Projection) -> list[str]:
    return shape_violations(row, projection.fields)


def response_violations(value: Mapping[str, Any], endpoint: str,
                        selector: Mapping[str, Any]) -> list[str]:
    """Check an envelope against a route-bound, already validated selector.

    The unbound RESPONSES union is inventory, not a privacy check: only an
    explicit boolean full=True authorizes the full list projection.
    """
    fields = RESPONSES[endpoint]
    if endpoint == "sessions":
        full = selector.get("full", False)
        if not isinstance(full, bool):
            raise ValueError("full selector must be a boolean")
        projection = LEGACY_FULL_SESSION_PROJECTION if full else LEGACY_COMPACT_SESSION_PROJECTION
        fields = tuple(replace(spec, type=f"array<shape:{projection.name}>")
                       if spec.name == "sessions" else spec for spec in fields)
    return shape_violations(value, fields)
