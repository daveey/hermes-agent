"""Fail-closed drift tests for the static legacy sessions payload manifest. [daveey]

Pins ``hermes_cli.sessions_projection_manifest`` against (a) the declared
``sessions`` schema, (b) the serializer constants and the keys their source
writes, (c) real disposable SessionDB output through the unchanged legacy
routes, (d) the seven annotator families (source keys and executed outputs)
and (e) the mounted route signatures that ``selector_for`` binds.
"""
import ast
import inspect
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import sessions_projection_manifest as manifest
from tests.hermes_cli import test_profiles_sidebar_scope as scope_helpers

profiles_on_disk = scope_helpers.profiles_on_disk
uncached_sidebar = scope_helpers._uncached_sidebar_endpoints

ROOT = Path(__file__).resolve().parents[2]
COMPACT = manifest.LEGACY_COMPACT_SESSION_PROJECTION
FULL = manifest.LEGACY_FULL_SESSION_PROJECTION
STAMP = manifest.LEGACY_SESSION_STAMP_PROJECTION
FULL_ONLY = {"system_prompt", "model_config", "system_prompt_hash", "git_metadata_generation"}
ANNOTATION_NAMES = {spec.name for spec in manifest.annotation_fields()}


# ── (a) schema ──────────────────────────────────────────────────────────
def test_columns_equal_schema_sql_pragma_in_order():
    from hermes_state_common import SCHEMA_SQL

    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    live = [(name, ctype, notnull, default, pk)
            for _cid, name, ctype, notnull, default, pk in conn.execute("PRAGMA table_info(sessions)")]
    assert live == [column[:5] for column in manifest.SESSIONS_TABLE_COLUMNS]
    affinity = {"TEXT": "string", "INTEGER": "integer", "REAL": "number"}
    for name, ctype, notnull, _default, pk, json_type in manifest.SESSIONS_TABLE_COLUMNS:
        expected = affinity[ctype] + ("" if notnull or pk else "|null")
        assert json_type == expected, name


def test_compact_sql_columns_are_exactly_the_manifest_compact_columns():
    from hermes_state import SessionDB

    selected = [col.removeprefix("s.") for col in SessionDB._compact_session_cols().split(", ")]
    declared = [c[0] for c in manifest.SESSIONS_TABLE_COLUMNS if c[0] not in manifest.COMPACT_EXCLUDED_COLUMNS]
    assert selected == declared
    assert SessionDB._SESSION_COMPACT_EXCLUDED == manifest.COMPACT_EXCLUDED_COLUMNS


# ── (b) serializer constants and source-written keys ────────────────────
def test_serializer_constants_match_sources():
    from hermes_cli import kanban_external_activity, kanban_session_activity, process_session_activity
    from hermes_cli import sessions_projection, web_server_gateway
    from hermes_cli.web_routers import profiles
    import hermes_state_health
    import hermes_state_outcome

    assert web_server_gateway._SESSION_LIST_HEAVY_FIELDS == manifest.LIST_STRIPPED_FIELDS
    assert sessions_projection.SESSION_STAMP_COLUMNS == manifest.STAMP_FIXED_COLUMNS
    assert profiles._SESSION_STAMP_COLUMNS == manifest.STAMP_FIXED_COLUMNS
    assert sessions_projection.SIDEBAR_SLICES == manifest.SIDEBAR_SLICES
    assert manifest.OPEN_MAPPINGS["SessionOutcome"][2] == hermes_state_outcome.OUTCOME_KINDS
    assert manifest.CAPS == {"kanban_waiting_on": kanban_session_activity.WAITING_ON_CAP,
                             "kanban_blocked_on": kanban_session_activity.WAITING_ON_CAP,
                             "kanban_external_cards": kanban_external_activity.CARDS_CAP,
                             "kanban_timed_cards": kanban_session_activity._identity.timers.CARDS_CAP,
                             "background_processes": process_session_activity._MAX_ROW_PROCESSES}
    assert hermes_state_health.STORAGE_CORRUPT == "corrupt"


def _function(path, qualname):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    parts = qualname.split(".")
    body = tree.body
    node = None
    for part in parts:
        node = next(n for n in body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == part)
        body = node.body
    return node


def _loop_literals(func):
    """``{loop var: {str constants}}`` for ``for k in ("a", ...)`` / ``for k, v in (("a", x), ...)``."""
    out = {}
    for node in ast.walk(func):
        if not (isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List))):
            continue
        target = node.target.elts[0] if isinstance(node.target, ast.Tuple) else node.target
        values = set()
        for elt in node.iter.elts:
            first = elt.elts[0] if isinstance(elt, ast.Tuple) else elt
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                values.add(first.value)
        if isinstance(target, ast.Name) and values:
            out.setdefault(target.id, set()).update(values)
    return out


def _key_writes(func, bases=None):
    """(stored, popped, dynamic) keys written on dict names (``bases`` restricts the subscripted name).

    Constant keys and literal-tuple loop keys resolve; any other key expression is ``dynamic``
    (its unparsed source) so a new computed write cannot slip past the audit."""
    stored, popped, dynamic = set(), set(), set()
    loops = _loop_literals(func)

    def resolve(key, sink):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            sink.add(key.value)
        elif isinstance(key, ast.Name) and key.id in loops:
            sink.update(loops[key.id])
        else:
            dynamic.add(ast.unparse(key))

    for node in ast.walk(func):
        targets = []
        if isinstance(node, ast.Assign):
            for target in node.targets:
                targets.extend(target.elts if isinstance(target, ast.Tuple) else [target])
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
        for target in targets:
            if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                    and (bases is None or target.value.id in bases)):
                resolve(target.slice, stored)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"pop", "setdefault"} and isinstance(node.func.value, ast.Name)
                and (bases is None or node.func.value.id in bases) and node.args):
            resolve(node.args[0], popped if node.func.attr == "pop" else stored)
    return stored, popped, dynamic


def test_compression_tip_copy_list_matches_source():
    func = _function("hermes_state_session_list.py", "project_compression_tips")
    loops = [node.iter for node in ast.walk(func) if isinstance(node, ast.For)
             and isinstance(node.target, ast.Name) and node.target.id == "key"]
    assert len(loops) == 1
    assert tuple(ast.literal_eval(loops[0])) == manifest.COMPRESSION_TIP_FIELDS


SERIALIZER_WRITERS = (
    ("hermes_state.py", "SessionDB._session_row_dict", {"data"}),
    ("hermes_state_sessions.py", "SessionSessionsMixin._list_row", {"s"}),
    ("hermes_state_sessions.py", "SessionSessionsMixin.list_sessions_rich", {"s", "session"}),
    ("hermes_state_session_list.py", "project_compression_tips", {"merged"}),
    ("hermes_state_local.py", "annotate_local_lineages", {"row"}),
    ("hermes_state_outcome.py", "stamp_outcome_rows", {"row"}),
    ("hermes_cli/web_routers/profiles.py", "_tag_rows", {"s"}),
    ("hermes_cli/session_listing.py", "stamp_automated", {"row"}),
    ("hermes_cli/web_server_gateway.py", "_strip_session_list_rows", {"s"}),
)


def _qualname(path, name):
    if "." not in name:
        return name
    _cls, meth = name.split(".")
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    owner = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
                 and any(isinstance(m, ast.FunctionDef) and m.name == meth for m in n.body))
    return f"{owner.name}.{meth}"


# Computed key writes, each audited: _session_row_dict resolves ``_{column}_resolved`` into
# system_prompt/tool_names (both declared columns); the strip pops _SESSION_LIST_HEAVY_FIELDS.
AUDITED_DYNAMIC_WRITES = {
    "SessionDB._session_row_dict": {"f'_{column}_resolved'"},
    "_strip_session_list_rows": {"key"},
}


def test_serializer_source_writes_only_declared_keys():
    known = {spec.name for spec in FULL.fields} | {spec.name for spec in COMPACT.fields}
    seen_stored, seen_popped = set(), set()
    for path, name, bases in SERIALIZER_WRITERS:
        stored, popped, dynamic = _key_writes(_function(path, _qualname(path, name)), bases)
        assert stored or popped or dynamic, f"{path}:{name} no longer writes row keys; re-audit the manifest"
        assert dynamic == AUDITED_DYNAMIC_WRITES.get(name, set()), name
        seen_stored |= stored
        seen_popped |= popped
    assert seen_stored - known == set()
    assert seen_popped <= manifest.POPPED_INTERNAL_KEYS
    assert {"_preview_raw", "_effective_last_active"} <= seen_popped


# ── (d) annotator families: source keys and audited row-mutating calls ──
FAMILY_SOURCES = {
    "kanban": (("hermes_cli/kanban_session_activity.py", "annotate_kanban_activity"),
               ("hermes_cli/kanban_session_activity.py", "_mark_kanban_unknown"),
               ("hermes_cli/kanban_activity_projection.py", "project_kanban_activity"),
               ("hermes_cli/kanban_activity_projection.py", "project_kanban_unknown"),
               ("hermes_cli/kanban_activity_identity.py", "stamp_blocked_owners"),
               ("hermes_cli/kanban_activity_identity.py", "mark_unknown"),
               ("hermes_cli/kanban_session_activity_timers.py", "stamp"),
               ("hermes_cli/kanban_open_asks.py", "stamp_needs_you"),
               ("hermes_cli/kanban_execution_observations.py", "count_snapshot"),
               ("hermes_cli/kanban_execution_observations.py", "_merge"),
               ("hermes_cli/kanban_external_activity.py", "stamp_boards"),
               ("hermes_cli/kanban_external_activity.py", "stamp_rows"),
               ("hermes_cli/kanban_external_activity.py", "stamp_replica_counts")),
    "goal": (("hermes_cli/goal_session_activity.py", "annotate_goal_activity"),
             ("hermes_cli/goal_session_activity.py", "project_goal_activity")),
    "cron": (("hermes_cli/cron_session_activity.py", "annotate_cron_activity"),
             ("hermes_cli/cron_session_activity.py", "project_cron_activity")),
    "background": (("hermes_cli/process_session_activity.py", "annotate_background_activity"),
                   ("hermes_cli/process_session_activity.py", "project_background_activity"),
                   ("hermes_cli/turn_process_execution_observations.py", "replace")),
    "turn": (("hermes_cli/turn_session_activity.py", "annotate_turn_activity"),
             ("hermes_cli/turn_session_activity.py", "project_turn_activity"),
             ("hermes_cli/turn_process_execution_observations.py", "replace")),
    "subagent": (("hermes_cli/subagent_session_activity.py", "annotate_subagent_activity"),
                 ("hermes_cli/subagent_session_activity.py", "project_subagent_activity"),
                 ("hermes_cli/turn_process_execution_observations.py", "replace")),
    "review": (("hermes_cli/review_session_activity.py", "annotate_review_wait"),
               ("hermes_cli/review_session_activity.py", "project_review_row")),
}
# Every call receiving the row(s) inside an audited function; any new one needs a manifest re-audit.
AUDITED_ROW_CALLS = {
    # Replica stamping only increments the already-declared external/unknown fields;
    # its complete writer is included in FAMILY_SOURCES so new keys remain guarded.
    "_external_activity.stamp_replica_counts", "row_ids",
    "identity.mark_unknown", "count_snapshot", "_external_activity.stamp_boards", "identity.row_ids",
    "identity.stamp_blocked_owners", "identity.timers.stamp", "_mark_kanban_unknown", "stamp_needs_you", "_merge", "ids_of",
    "stamp_rows", "external_activity.stamp_rows", "_row_ids", "replace", "_row_prs", "enumerate",
    "project_kanban_activity", "project_kanban_unknown", "project_goal_activity", "project_cron_activity",
    "project_background_activity", "project_turn_activity", "project_subagent_activity", "project_review_row"}


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _dotted(node.value) + "." + node.attr
    return "?"


@pytest.mark.parametrize("family", manifest.LIST_ANNOTATION_ORDER)
def test_annotator_source_keys_equal_family_declaration(family):
    stored, popped, calls = set(), set(), set()
    for path, name in FAMILY_SOURCES[family]:
        func = _function(path, name)
        s, p, dynamic = _key_writes(func, {"row"})
        assert dynamic == set(), f"{path}:{name} computes a row key; resolve it in the manifest"
        stored |= s
        popped |= p
        calls |= {_dotted(c.func) for c in ast.walk(func) if isinstance(c, ast.Call)
                  and any(isinstance(a, ast.Name) and a.id in ("row", "rows") for a in c.args)}
    declared = {spec.name for spec in manifest.ANNOTATION_FAMILIES[family]}
    assert stored | popped == declared
    assert calls <= AUDITED_ROW_CALLS


def test_seven_families_and_legacy_orders():
    assert set(manifest.ANNOTATION_FAMILIES) == set(manifest.LIST_ANNOTATION_ORDER)
    assert len(manifest.LIST_ANNOTATION_ORDER) == 7
    assert sorted(manifest.SIDEBAR_ANNOTATION_ORDER) == sorted(manifest.LIST_ANNOTATION_ORDER)
    assert all(spec.presence == manifest.CONDITIONAL for spec in manifest.annotation_fields())
    from hermes_cli import sessions_projection

    doc = " ".join(sessions_projection.__doc__.split())
    assert "kanban(display=True), goal, cron, background, turn, subagent, review" in doc
    assert "subagent, kanban(display=True), goal, cron, background, turn, review" in doc


# ── (c) real disposable SessionDB output through the legacy routes ──────
@pytest.fixture
def seeded(profiles_on_disk, monkeypatch):
    from hermes_state import SessionDB
    import hermes_state_outcome as outcome
    from hermes_cli.web_routers import profiles

    db = SessionDB(db_path=profiles_on_disk["default"] / "state.db")
    try:
        db.create_session("plain", source="cli", system_prompt="full-only prompt",
                          model_config={"provider": "openai"}, cwd="/work")
        db.append_message(session_id="plain", role="user", content="hello")
        outcome.declare_outcome("plain", {"kind": "done", "summary": "ok"}, db=db)
        outcome.set_muted("plain", True, db=db)
        db.set_session_pinned("plain", True)
        db.create_session("root", source="cli", system_prompt="root prompt")
        db.append_message(session_id="root", role="user", content="root")
        db.end_session("root", "compression")
        db.create_session("tip", source="cli", parent_session_id="root")
        db.append_message(session_id="tip", role="user", content="tip")
        db.create_session("branch", source="telegram",
                          model_config={"_branched_from": "plain", "_reset_from": "root"})
        db.append_message(session_id="branch", role="user", content="b")
        db.create_session("job", source="cron")
        db.append_message(session_id="job", role="user", content="tick")
    finally:
        db.close()
    worker = SessionDB(db_path=profiles_on_disk["worker"] / "state.db")
    try:
        worker.create_session("w", source="cli")
        worker.append_message(session_id="w", role="user", content="w")
    finally:
        worker.close()
    for attr in ("annotate_kanban_activity", "annotate_goal_activity", "annotate_cron_activity",
                 "annotate_background_activity", "annotate_turn_activity", "annotate_subagent_activity",
                 "annotate_review_wait"):
        monkeypatch.setattr(profiles, attr, lambda rows, **_kw: rows)
    return profiles


def _non_annotation(projection):
    return {spec.name for spec in projection.fields} - ANNOTATION_NAMES


def _assert_rows(rows, projection):
    assert rows
    for row in rows:
        assert manifest.row_violations(row, projection) == [], row["id"]
        assert not set(row) & manifest.POPPED_INTERNAL_KEYS
    observed = set().union(*map(set, rows))
    assert observed == _non_annotation(projection), "every declared non-annotation field must be exercised"


@pytest.mark.parametrize("full", [False, True])
def test_real_list_rows_and_envelope(seeded, full):
    result = seeded.get_profiles_sessions(limit=50, offset=0, full=full)
    assert manifest.response_violations(result, "sessions", {"full": full}) == []
    _assert_rows(result["sessions"], FULL if full else COMPACT)
    if not full:
        assert all(not set(row) & FULL_ONLY for row in result["sessions"])
    tip = next(row for row in result["sessions"] if row["id"] == "tip")
    assert tip["_lineage_root_id"] == "root" and tip["continuation_kind"] == "compression"
    if full:
        plain = next(row for row in result["sessions"] if row["id"] == "plain")
        assert plain["system_prompt"] == "full-only prompt" and isinstance(plain["model_config"], str)


def test_real_sidebar_rows_are_compact_and_envelope_matches(seeded):
    result = inspect.unwrap(seeded.get_profiles_sessions_sidebar)()
    assert manifest.response_violations(result, "sessions_sidebar", {}) == []
    rows = [row for key in manifest.SIDEBAR_SLICES for row in result[key]["sessions"]]
    for row in rows:
        assert manifest.row_violations(row, COMPACT) == []
        assert not set(row) & FULL_ONLY
    assert check_shape(result["recents"], "SidebarRecents")
    assert check_shape(result["messaging"], "SidebarMessaging")


def check_shape(value, name):
    return manifest.shape_violations(value, manifest.SHAPES[name]) == []


def test_real_sidebar_failed_scan_envelope(seeded, monkeypatch):
    def failing(name, home, errors, fn):
        if errors is not None:
            errors.append({"profile": name, "error": "busy"})
    monkeypatch.setattr(seeded, "_read_profile_db", failing)
    result = inspect.unwrap(seeded.get_profiles_sessions_sidebar)()
    assert manifest.response_violations(result, "sessions_sidebar", {}) == []
    assert check_shape(result["recents"], "FailedRecents") and check_shape(result["cron"], "FailedSlice")
    assert check_shape(result["messaging"], "FailedMessaging")


def test_real_stamps_rows_and_envelope(seeded, monkeypatch):
    def goal(rows, **_kw):
        rows[0]["goal_active"] = True
        rows[0]["_private"] = 1
        return rows
    monkeypatch.setattr(seeded, "annotate_goal_activity", goal)
    result = seeded.get_profiles_session_stamps(ids="plain,tip,missing", profile="default")
    assert manifest.response_violations(result, "sessions_stamps", {"ids": "plain,tip,missing", "profile": "default"}) == []
    assert [row["id"] for row in result["sessions"]] == ["plain", "tip"]
    for row in result["sessions"]:
        assert manifest.row_violations(row, STAMP) == []
    assert result["sessions"][0]["goal_active"] is True
    assert set(result["sessions"][1]) == {"id", "profile", *manifest.STAMP_FIXED_COLUMNS}


def test_drift_is_detected_against_real_rows(seeded):
    rows = seeded.get_profiles_sessions(limit=50, offset=0)["sessions"]
    dropped = manifest.Projection("probe", tuple(s for s in COMPACT.fields if s.name != "preview"), True, ())
    assert any("unknown field 'preview'" in v for row in rows for v in manifest.row_violations(row, dropped))
    retyped = manifest.Projection("probe", tuple(
        manifest.FieldSpec(s.name, "integer", s.presence, s.source) if s.name == "pinned" else s
        for s in COMPACT.fields), True, ())
    assert all(manifest.row_violations(row, retyped) for row in rows)


def test_full_projection_is_never_broadcast():
    assert FULL.broadcast is False and COMPACT.broadcast is True
    assert FULL.served_by == ("sessions(full=True).sessions[]",)
    assert FULL_ONLY <= {s.name for s in FULL.fields}
    assert not FULL_ONLY & {s.name for s in COMPACT.fields}
    assert {s.name for s in COMPACT.fields} - {s.name for s in FULL.fields} == {"model_provider"}


# ── (d) executed annotators ─────────────────────────────────────────────
def _row():
    return {"id": "s", "profile": "default", "_lineage_ids": ["s"], "last_active": 1.0, "message_count": 1}


def _family_check(row, before, family):
    added = set(row) - before
    assert added <= {spec.name for spec in manifest.ANNOTATION_FAMILIES[family]}
    assert manifest.row_violations({k: row[k] for k in added}, manifest.Projection(
        "family", manifest.ANNOTATION_FAMILIES[family], True, ())) == []
    return added


def test_executed_goal_cron_turn_review(monkeypatch):
    from hermes_cli import cron_session_activity as cron, goal_session_activity as goal
    from hermes_cli import review_session_activity as review, turn_session_activity as turn

    monkeypatch.setattr(goal, "active_goal_sessions", lambda: frozenset({"s"}))
    monkeypatch.setattr(cron, "cron_owner_sessions", lambda: frozenset({"s"}))
    monkeypatch.setattr(turn, "live_turn_session_ids", lambda: frozenset({"s"}))
    monkeypatch.setattr(turn, "_leases", {})
    monkeypatch.setattr(review, "_row_prs", lambda rows: {0: [("o", "r", 7)]})
    monkeypatch.setattr(review, "_pr_info", lambda key: {"open": True})
    monkeypatch.setattr(review, "review_state", lambda info, now: "stuck")
    for family, annotate, expected in (
            ("goal", goal.annotate_goal_activity, {"goal_active"}),
            ("cron", cron.annotate_cron_activity, {"cron_active"}),
            ("turn", turn.annotate_turn_activity, {"turn_live"}),
            ("review", review.annotate_review_wait, {"review_wait", "review_wait_pr"})):
        row = _row()
        annotate([row])
        assert _family_check(row, set(_row()), family) == expected


def test_executed_background_and_subagent(monkeypatch):
    from hermes_cli import process_session_activity as process, subagent_session_activity as subagent

    monkeypatch.setattr(process, "_owned_processes", lambda: {"s": frozenset({"p1"})})
    monkeypatch.setattr(process, "_details", {"p1": {"id": "p1", "command": "sleep", "pid": 9}})
    monkeypatch.setattr(process, "_evidence", {})
    row = _row()
    process.annotate_background_activity([row])
    assert _family_check(row, set(_row()), "background") == {"background_active", "background_processes"}
    monkeypatch.setattr(subagent, "_profile_paths", list)
    row = _row()
    subagent.annotate_subagent_activity([row])
    assert _family_check(row, set(_row()), "subagent") == {"execution_observations"}


def test_executed_kanban_unknown_success_and_external(monkeypatch):
    from hermes_cli import kanban_external_activity as external, kanban_session_activity as kanban

    monkeypatch.setattr(kanban, "_annotation_snapshot", lambda display=False: None)
    row = _row()
    kanban.annotate_kanban_activity([row], display=True)
    assert _family_check(row, set(_row()), "kanban") == {"kanban_unknown", "execution_observations"}
    keys = frozenset({("b/t1", "blocked", False), ("b/t2", "active", True)})
    parts = ({"s": 2}, {"s": 1}, {"s": 1}, {}, {"s": frozenset({"t2"})}, {"s": 1}, {"s": 1}, {"s": 1},
             {"s": keys}, {}, {"s": 1}, {})
    monkeypatch.setattr(kanban, "_annotation_snapshot", lambda display=False: parts)
    monkeypatch.setattr(kanban, "_board_db_paths", list)
    row = _row()
    kanban.annotate_kanban_activity([row], display=True)
    added = _family_check(row, set(_row()), "kanban")
    assert {"kanban_observed_at", "kanban_active", "kanban_blocked", "kanban_parked", "kanban_waiting_on",
            "kanban_blocked_on", "needs_you", "execution_observations"} <= added
    card = {"task_id": "t", "title": "", "external": {"state": "external_unknown", "observed_at": 1}}
    external.stamp_rows([row], {"s": {"counts": [1, 1, 1], "cards": [card]}}, lambda r: [r["id"]])
    assert {"kanban_external", "kanban_external_unknown", "kanban_external_down",
            "kanban_external_cards"} <= _family_check(row, set(_row()), "kanban")


# ── (e) selectors ───────────────────────────────────────────────────────
_PY_TYPES = {int: "integer", str: "string", bool: "boolean", str | None: "string|null"}


@pytest.mark.parametrize("name", sorted(manifest.SELECTORS))
def test_selectors_equal_mounted_route_signatures(name):
    from fastapi.params import Param
    from pydantic_core import PydanticUndefined
    from hermes_cli.sessions_fact_routes import selector_for
    from hermes_cli.web_routers import profiles

    route = next(r for r in profiles.sessions_router.routes if r.path == manifest.SELECTOR_ROUTES[name])
    signature = inspect.signature(route.endpoint)
    declared = manifest.SELECTORS[name]
    assert list(signature.parameters) == [p.name for p in declared]
    for param in declared:
        live = signature.parameters[param.name]
        default = live.default.default if isinstance(live.default, Param) else live.default
        assert _PY_TYPES[live.annotation] == param.type, param.name
        assert (default is PydanticUndefined) is param.required, param.name
        assert param.required or default == param.default, param.name
        metadata = getattr(live.default, "metadata", []) if isinstance(live.default, Param) else []
        constraints = {(key, getattr(m, key)) for m in metadata for key in ("ge", "le", "max_length")
                       if getattr(m, key, None) is not None}
        assert constraints == set(param.constraints), param.name
    supplied = {p.name: "x" for p in declared if p.required}
    assert set(selector_for(signature, (), supplied)) == {p.name for p in declared}


# ── validator negative controls ─────────────────────────────────────────
@pytest.mark.parametrize("value,valid", [(0, True), (0.0, True), ("0", False), (False, False), (True, False), (1, False)])
def test_const_zero_is_numeric_not_text_or_boolean(value, valid):
    assert manifest.check(value, "const:0") is valid


def test_response_checker_binds_list_projection_to_selector(seeded):
    compact = seeded.get_profiles_sessions(limit=50, offset=0, full=False)
    full = seeded.get_profiles_sessions(limit=50, offset=0, full=True)
    assert manifest.response_violations(compact, "sessions", {}) == []
    assert manifest.response_violations(full, "sessions", {"full": True}) == []
    assert manifest.response_violations(full, "sessions", {"full": False})
    assert manifest.response_violations(full, "sessions", {})
    assert manifest.response_violations(compact, "sessions", {"full": True})
    for field in FULL_ONLY:
        from copy import deepcopy
        leaked = deepcopy(compact)
        leaked["sessions"][0][field] = full["sessions"][0][field]
        assert manifest.response_violations(leaked, "sessions", {"full": False}), field
    with pytest.raises(ValueError, match="full"):
        manifest.response_violations(full, "sessions", {"full": "false"})


def test_checker_rejects_off_type_values():
    assert manifest.check(1, "integer") and not manifest.check(True, "integer")
    assert not manifest.check(True, "number") and manifest.check(1, "number")
    assert manifest.check(None, "string|null") and not manifest.check(0, "string|null")
    assert not manifest.check({"kind": "bogus"}, "open:SessionOutcome")
    assert manifest.check({"kind": "done", "summary": "x"}, "open:SessionOutcome")
    assert not manifest.check([{"kind": "card", "ref": "r", "question": "q"}], "array<shape:NeedsYouEntry>")
    assert not manifest.check({"a": 1}, "map<boolean>")
    assert manifest.check("parked", "enum:stuck,parked") and not manifest.check("x", "enum:stuck,parked")
    assert manifest.shape_violations({"profile": "a"}, manifest.SHAPES["ProfileError"]) == ["missing field 'error'"]
    with pytest.raises(ValueError):
        manifest.check(1, "bogus")
