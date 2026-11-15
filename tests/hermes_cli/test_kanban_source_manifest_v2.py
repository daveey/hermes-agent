"""Core reads the Board v2 selected-artifact contract without advertising it yet."""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_workspace_source as ws
from tests.hermes_cli.test_kanban_workspace_source import manifest, custody


def artifact(uid="a:one", **changes):
    return {"attachment_uid": uid, "filename": "report.txt", "sha256": "a" * 64, **changes}


def test_v2_roundtrip_canonical_selected_artifacts():
    entries = [artifact("a:z"), artifact("a:a", filename="résumé.txt")]
    value = manifest(version=2, source_kind="scratch_external", artifacts=entries)
    parsed = ws.parse_manifest(json.dumps(value, ensure_ascii=False))
    assert parsed.version == 2 and parsed.source_kind == "scratch_external"
    assert parsed.artifacts == (entries[1], entries[0])
    assert parsed.raw["artifacts"] == [entries[1], entries[0]]
    assert value["artifacts"] == entries  # do not mutate a caller's grant
    assert ws.parse_manifest(parsed.raw) == parsed
    assert ws.parse_manifest(manifest()).version == 1
    assert ws.parse_manifest(manifest()).artifacts == ()
    assert parsed != ws.parse_manifest(manifest(version=2, source_kind="scratch_external", artifacts=[]))


@pytest.mark.parametrize("entries", [None, {}, "", [None], [artifact(extra=True)],
    [artifact(sha256="A" * 64)], [artifact(sha256="a" * 63)], [artifact(sha256=1)],
    [artifact("bad/uid")], [artifact("")], [artifact("a" * 129)],
    [artifact(filename="")], [artifact(filename=".")], [artifact(filename="..")],
    [artifact(filename="a/b")], [artifact(filename="a\\b")], [artifact(filename="a\x00b")],
    [artifact(filename=[])], [artifact(), artifact()], [artifact(str(i)) for i in range(33)]])
def test_v2_artifact_schema_refuses(entries):
    with pytest.raises(ws.ManifestInvalid):
        ws.parse_manifest(manifest(version=2, artifacts=entries))


@pytest.mark.parametrize("version,kind", [(1, "scratch_external"), (3, "task_worktree"),
    (True, "task_worktree"), (2.0, "task_worktree"), (2, []), (2, {})])
def test_manifest_version_and_kind_are_strict(version, kind):
    with pytest.raises(ws.ManifestInvalid):
        ws.parse_manifest(manifest(version=version, source_kind=kind))


def test_utf8_size_not_ascii_escaped_size():
    value = manifest(version=2, artifacts=[artifact(filename="é" * 1500)])
    assert len(json.dumps(value, ensure_ascii=False).encode("utf-8")) < ws.MANIFEST_MAX_BYTES
    assert len(json.dumps(value).encode("utf-8")) > ws.MANIFEST_MAX_BYTES
    assert ws.parse_manifest(value).artifacts[0]["filename"] == "é" * 1500
    value["artifacts"][0]["filename"] = "é" * 4500
    with pytest.raises(ws.ManifestInvalid, match="max"):
        ws.parse_manifest(value)


def test_custody_preserves_protocol_and_retained_predecessor():
    parsed = ws.parse_custody(custody(source_protocol=2, retained_run_uid="old-run", state="retained"))
    assert parsed.source_protocol == 2 and parsed.retained_run_uid == "old-run"
    legacy = ws.parse_custody(custody())
    assert legacy.source_protocol == 1 and legacy.retained_run_uid is None


@pytest.mark.parametrize("changes", [{"source_protocol": True}, {"source_protocol": 3},
    {"source_protocol": "2"}, {"source_protocol": 2.0}, {"retained_run_uid": {}},
    {"retained_run_uid": ""}])
def test_malformed_custody_identity_is_not_portable(changes):
    parsed = ws.parse_custody(custody(**changes))
    assert parsed.state == "retained" and parsed.reason == "malformed_custody"
    assert parsed.source_holder is None
