"""Read-only fleet roster evidence validation; never discover by successful replies alone.

A source is a gateway/profile/board/database tuple, not a local integer or a host alias.
Discovery and membership receipts are separate from capture receipts. Version 1 refuses
changing membership rather than guessing coverage for a joined or departed executor.
"""
from __future__ import annotations

IDENTITY = ("source_gateway", "profile", "board_uid", "database_uid")
PINS = ("process_epoch", "core_revision", "config_sha256")


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _time(value) -> bool:
    return type(value) is int and value >= 0


def _problem(problems: list, code: str, detail: str) -> None:
    problems.append({"code": code, "detail": detail})


def _rows(value, section: str, problems: list) -> dict:
    result = {}
    if not isinstance(value, list):
        _problem(problems, "invalid_sources", section)
        return result
    for index, row in enumerate(value):
        if not isinstance(row, dict) or not all(_text(row.get(k)) for k in IDENTITY):
            _problem(problems, "invalid_identity", f"{section}[{index}]")
            continue
        identity = tuple(row[k] for k in IDENTITY)
        if identity in result:
            _problem(problems, "duplicate_identity", f"{section}: {identity!r}")
        else:
            result[identity] = row
    return result


def _snapshot(value, section: str, at_ms, problems: list) -> dict:
    if not isinstance(value, dict):
        _problem(problems, "discovery_incomplete", section)
        return {}
    if value.get("complete") is not True:
        _problem(problems, "discovery_incomplete", section)
    if value.get("unknown_sources") != []:
        _problem(problems, "discovery_incomplete", f"{section}: unresolved or absent unknown-source inventory")
    if not _text(value.get("provenance")):
        _problem(problems, "missing_provenance", section)
    if not _time(value.get("observed_ms")) or value["observed_ms"] != at_ms:
        _problem(problems, "discovery_cut_mismatch", section)
    rows = _rows(value.get("sources"), section, problems)
    for identity, row in rows.items():
        if not all(_text(row.get(k)) for k in PINS):
            _problem(problems, "missing_source_pin", f"{section}: {identity!r}")
    return rows


def _membership(value, start, end, problems: list) -> None:
    if not isinstance(value, dict):
        _problem(problems, "membership_incomplete", "missing membership journal receipt")
        return
    if value.get("complete") is not True:
        _problem(problems, "membership_incomplete", "membership journal is incomplete")
    if not _text(value.get("provenance")):
        _problem(problems, "missing_provenance", "membership")
    if not _time(value.get("start_ms")) or not _time(value.get("end_ms")) \
            or value["start_ms"] != start or value["end_ms"] != end:
        _problem(problems, "membership_incomplete", "membership does not cover the window")
    events = value.get("events")
    if not isinstance(events, list):
        _problem(problems, "membership_incomplete", "missing membership events")
    elif events:
        _problem(problems, "membership_changed", "membership events require interval reconciliation")


def _coverage(row: dict, identity: tuple, start, end, problems: list) -> None:
    detail = repr(identity)
    if row.get("status") != "ok":
        _problem(problems, "source_unavailable", detail)
    if not _text(row.get("provenance")):
        _problem(problems, "missing_provenance", f"coverage: {detail}")
    if not _time(row.get("start_ms")) or not _time(row.get("end_ms")) \
            or row["start_ms"] != start or row["end_ms"] != end:
        _problem(problems, "coverage_gap", detail)
    if not all(_text(row.get(k)) for k in ("opening_cut", "closing_cut")):
        _problem(problems, "missing_cut", detail)


def validate(value) -> dict:
    """Return explicit incomplete evidence for missing, malformed or unsupported rosters.

    Expected sources come from the independently declared fleet inventory, including offline
    sources. Discovery snapshots must enumerate the entire scope, not just responding hosts.
    Provenance refers to retained read-only source evidence; this checks its structure and
    consistency, not the authenticity of an operator's assertion of discovery completeness.
    """
    problems: list[dict] = []
    result = {"version": 1, "complete": False, "problems": problems, "sources": []}
    if not isinstance(value, dict) or value.get("version") != 1:
        _problem(problems, "invalid_roster", "expected a version 1 roster object")
        return result
    result["unknown_sources"] = {
        section: value.get(section, {}).get("unknown_sources")
        for section in ("opening", "closing") if isinstance(value.get(section), dict)
    }
    window = value.get("window")
    window = window if isinstance(window, dict) else {}
    start, end = window.get("start_ms"), window.get("end_ms")
    if not _time(start) or not _time(end) or end <= start:
        _problem(problems, "invalid_window", "a nonempty bounded window is required")
    expected = _rows(value.get("expected_sources"), "expected_sources", problems)
    opening = _snapshot(value.get("opening"), "opening", start, problems)
    closing = _snapshot(value.get("closing"), "closing", end, problems)
    coverage = _rows(value.get("coverage"), "coverage", problems)
    _membership(value.get("membership"), start, end, problems)
    identities = set(expected) | set(opening) | set(closing) | set(coverage)
    if not identities:
        _problem(problems, "empty_roster", "no known sources; not empty success")
    if set(opening) != set(closing):
        _problem(problems, "membership_changed", "opening and closing source identities differ")
    for identity in sorted(identities):
        result["sources"].append({"identity": list(identity), "expected": expected.get(identity),
                                  "opening": opening.get(identity), "closing": closing.get(identity),
                                  "coverage": coverage.get(identity)})
        for section, rows in (("expected_sources", expected), ("opening", opening),
                              ("closing", closing), ("coverage", coverage)):
            if identity not in rows:
                _problem(problems, "source_missing", f"{section}: {identity!r}")
        if identity in opening and identity in closing:
            if any(opening[identity].get(k) != closing[identity].get(k) for k in PINS):
                _problem(problems, "source_changed", repr(identity))
        if identity in coverage:
            _coverage(coverage[identity], identity, start, end, problems)
            if identity in opening and any(coverage[identity].get(k) != opening[identity].get(k) for k in PINS):
                _problem(problems, "source_changed", f"coverage: {identity!r}")
    result["complete"] = not problems
    return result
