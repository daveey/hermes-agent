"""Finite producer and alarm inventory for the queue-fairness evidence. [daveey]

``scan`` walks named source trees once and returns every line that sets a flat top priority
(``priority=1000``, ``"priority": 1000``, ``p1000``) or mentions an alarm key prefix, plus a hash of each tree.
``check`` compares those matches with a hand-written classification: one row per match saying what the
producer or detector is and how it is disposed. A match with no row, or a row whose match is gone, makes the
inventory incomplete; nothing is classified by its title or guessed. Trees differ per executor, so the
operator runs ``scan`` on each one and ``check`` once per distinct tree hash.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable

from hermes_cli.kanban_admission_score import ALARM_KEY_PREFIXES
from hermes_cli.kanban_fairness_schema import VERSION

# Keys that look like alarms but that the scorer does not floor: they must be classified, not assumed.
ALARM_CANDIDATE_PREFIXES = ("worker-resources-unknown:", "turn-urgency-health:")
_PRIORITY = re.compile(r"""priority["']?\s*[=:]\s*["']?1000\b|\bp1000\b""", re.IGNORECASE)
_SUFFIXES = {".py", ".md", ".json", ".yaml", ".yml", ".toml", ".sh", ""}
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".worktrees", "dist", "build"}
_MAX_FILE_BYTES = 2 * 1024 * 1024
DISPOSITIONS = ("inherits_parent", "explicit_user_urgency", "flat_default", "not_a_producer", "floored",
                "not_floored")


def _files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in _SUFFIXES and not _SKIP_DIRS.intersection(path.parts) \
                and path.stat().st_size <= _MAX_FILE_BYTES:
            yield path


def match_id(tree: str, path: str, text: str) -> str:
    """Stable across line moves: the tree's name, the file and the matched line's text."""
    return hashlib.sha256(f"{tree}\0{path}\0{text}".encode()).hexdigest()[:16]


def scan(trees: dict[str, Path]) -> dict:
    """``{"trees": {name: {root, sha256, files}}, "producers": [...], "alarms": [...]}``."""
    prefixes = ALARM_KEY_PREFIXES + ALARM_CANDIDATE_PREFIXES
    out: dict = {"version": VERSION, "trees": {}, "producers": [], "alarms": []}
    for name, root in sorted(trees.items()):
        digest, count = hashlib.sha256(), 0
        for path in _files(root):
            data = path.read_bytes()
            rel = path.relative_to(root).as_posix()
            digest.update(f"{rel}\0{hashlib.sha256(data).hexdigest()}\n".encode())
            count += 1
            for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                text = line.strip()
                row = {"tree": name, "path": rel, "line": number, "text": text[:240],
                       "id": match_id(name, rel, text)}
                if _PRIORITY.search(line):
                    out["producers"].append(row)
                found = [p for p in prefixes if p in line]
                if found:
                    out["alarms"].append({**row, "prefixes": found})
        out["trees"][name] = {"root": str(root), "sha256": digest.hexdigest(), "files": count}
    return out


def check(scanned: dict, classification: dict) -> dict:
    """Every match needs exactly one valid classification row, and every row a live match."""
    problems: list[dict] = []
    result = {"version": VERSION, "trees": scanned["trees"], "problems": problems}
    for kind in ("producers", "alarms"):
        rows, seen = {}, set()
        for row in classification.get(kind) or []:
            if not isinstance(row, dict) or row.get("disposition") not in DISPOSITIONS \
                    or not row.get("family") or not row.get("id"):
                problems.append({"kind": kind, "problem": "invalid_row", "row": row})
                continue
            rows[row["id"]] = row
        classified = []
        for match in scanned[kind]:
            row = rows.get(match["id"])
            if row is None:
                problems.append({"kind": kind, "problem": "unclassified", "match": match})
                continue
            seen.add(match["id"])
            classified.append({**match, "family": row["family"], "disposition": row["disposition"],
                               "note": row.get("note")})
        for missing in sorted(set(rows) - seen):
            problems.append({"kind": kind, "problem": "stale_row", "row": rows[missing]})
        result[kind] = classified
    result["complete"] = not problems
    return result
