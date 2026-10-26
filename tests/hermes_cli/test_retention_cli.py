"""Observation commands preserve candidates and cannot enable apply."""
import json

import pytest

from hermes_cli.retention_cli import main


@pytest.mark.platforms("linux")
def test_observation_cli_retains_and_reports(tmp_path, capsys):
    cache = tmp_path / "cache"
    cache.mkdir()
    marker = cache / "unique-input"
    marker.write_bytes(b"preserve me")
    before = marker.stat()
    names_before = sorted(p.name for p in tmp_path.iterdir())
    assert main(["observe", "--workspace", str(tmp_path), "--candidate", "cache"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["candidate_count"] == 1
    assert report["totals"]["eligible_bytes"] == 0
    assert report["deletion_authorized"] is False
    assert marker.read_bytes() == b"preserve me"
    assert before.st_ino == marker.stat().st_ino
    assert before.st_mtime_ns == marker.stat().st_mtime_ns
    assert sorted(p.name for p in tmp_path.iterdir()) == names_before


def test_invalid_cli_candidate(capsys, tmp_path):
    assert main(["observe", "--workspace", str(tmp_path), "--candidate", "../foreign"]) == 1
    assert json.loads(capsys.readouterr().out)["deletion_authorized"] is False


def test_apply_is_disabled_even_with_environment(monkeypatch, capsys):
    monkeypatch.setenv("HERMES_RETENTION_APPLY", "1")
    assert main(["apply"]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "outcome": "retained", "reason": "apply_not_enabled",
        "deletion_authorized": False, "removed_bytes": 0,
    }


@pytest.mark.platforms("linux")
def test_bare_reftable_is_retained(tmp_path, capsys):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "HEAD").write_text("ref: refs/heads/main")
    (cache / "objects").mkdir()
    (cache / "reftable").mkdir()
    assert main(["observe", "--workspace", str(tmp_path), "--candidate", "cache"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert "possible_bare_repository" in {e["code"] for e in report["errors"]}
    assert report["totals"]["allocated_bytes"] is None
