"""The producer/alarm inventory refuses an unclassified flat-1000 producer or detector key. [daveey]"""
from __future__ import annotations

from hermes_cli import kanban_fairness_inventory as inventory


def _tree(tmp_path):
    root = tmp_path / "skills"
    (root / "review").mkdir(parents=True)
    (root / "review" / "SKILL.md").write_text(
        "Create the review card with priority=1000 so it runs first.\n"
        "Titles like `p1000 sweep` are raised by hand.\n", encoding="utf-8")
    (root / "watch.py").write_text(
        'KEY = f"worker-claim-watch:{worker}"\n'
        'OTHER = "worker-resources-unknown:" + host\n'
        'args = {"priority": 1000}\n'
        "priority = 10000  # not a flat 1000\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "x.py").write_text("priority=1000\n", encoding="utf-8")
    return root


def test_scan_finds_every_match_and_hashes_the_tree(tmp_path):
    root = _tree(tmp_path)
    scanned = inventory.scan({"state-skills": root})
    assert [(m["path"], m["line"]) for m in scanned["producers"]] == [
        ("review/SKILL.md", 1), ("review/SKILL.md", 2), ("watch.py", 3)]
    assert [(m["path"], m["prefixes"]) for m in scanned["alarms"]] == [
        ("watch.py", ["worker-claim-watch:"]), ("watch.py", ["worker-resources-unknown:"])]
    before = scanned["trees"]["state-skills"]
    assert before["files"] == 2
    (root / "watch.py").write_text("changed\n", encoding="utf-8")
    assert inventory.scan({"state-skills": root})["trees"]["state-skills"]["sha256"] != before["sha256"]


def test_check_needs_one_valid_row_per_match(tmp_path):
    scanned = inventory.scan({"state-skills": _tree(tmp_path)})

    def rows(kind, disposition):
        return [{"id": m["id"], "family": "review", "disposition": disposition} for m in scanned[kind]]

    full = {"producers": rows("producers", "explicit_user_urgency"), "alarms": rows("alarms", "floored")}
    checked = inventory.check(scanned, full)
    assert checked["complete"] and checked["producers"][0]["disposition"] == "explicit_user_urgency"

    missing = {**full, "producers": full["producers"][1:]}
    checked = inventory.check(scanned, missing)
    assert not checked["complete"]
    assert [(p["kind"], p["problem"]) for p in checked["problems"]] == [("producers", "unclassified")]

    unknown_key = {**full, "alarms": full["alarms"][:1]}
    assert [p["match"]["prefixes"] for p in inventory.check(scanned, unknown_key)["problems"]] == [
        ["worker-resources-unknown:"]]

    stale = {**full, "alarms": full["alarms"] + [{"id": "gone", "family": "x", "disposition": "floored"}]}
    assert [p["problem"] for p in inventory.check(scanned, stale)["problems"]] == ["stale_row"]

    invalid = {**full, "producers": [{**full["producers"][0], "disposition": "fine"}] + full["producers"][1:]}
    assert [p["problem"] for p in inventory.check(scanned, invalid)["problems"]] == ["invalid_row", "unclassified"]
