#!/usr/bin/env python3
"""Metadata-only flaky-test artifact: strict schema, CI exporter, safe download/decoding.

Each Python CI slice runs this exporter after the tests (``--report`` is the runner's local flaky
report, never uploaded) and uploads the single small ``flaky-metadata.json`` it writes. The hub cron
(``flaky_kanban.py --from-github``) downloads those artifacts instead of reading check-run annotations.

Only a bounded canonical test identity (``tests/<path>.py[::Class::test]``) plus safe provenance
(repository, run id/attempt, head/source sha, slice) ever enters the artifact. Failure excerpts,
parameter suffixes and every other report field are discarded. Every rejection here carries a fixed
code (``ArtifactError.code``), never the offending content.

    python3 scripts/ci/flaky_artifact.py --report PATH --output PATH --slice N --slices M

Environment (workflow-supplied): GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT, FLAKY_HEAD_SHA.
A missing/oversize/malformed source report writes ``complete=false`` with an empty list and exits 0 so
the upload preserves explicit "unavailable" evidence; any other metadata failure exits non-zero.
"""
from __future__ import annotations

import argparse
import http.client
import io
import json
import os
import re
import stat
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

VERSION = 1
MEMBER = "flaky-metadata.json"
ARTIFACT_PREFIX = "flaky-metadata-v1"
MAX_JSON = 2 * 1024 * 1024
MAX_ZIP = 2 * 1024 * 1024
MAX_RATIO = 200
MAX_SOURCE_REPORT = 16 * 1024 * 1024
MAX_FLAKY = 10000
MAX_TEST_ID = 512
MAX_SLICES = 64
MAX_INT = 2**63 - 1
ALLOWED_HOST_SUFFIXES = (".blob.core.windows.net", ".actions.githubusercontent.com")
API_HOST = "api.github.com"

KEYS = frozenset({"version", "repository", "run_id", "run_attempt", "head_sha", "source_sha", "slice", "slices",
                  "complete", "flaky"})
ENTRY_KEYS = frozenset({"test_id", "status", "retried"})
STATUS = "passed_on_retry"
RETRIED = ("tests", "file")

_REPO = re.compile(r"[A-Za-z0-9_.\-]{1,100}/[A-Za-z0-9_.\-]{1,100}", re.ASCII)
_SHA = re.compile(r"[0-9a-f]{40}", re.ASCII)
_TEST_ID = re.compile(r"tests/(?:[A-Za-z0-9_\-]+/)*[A-Za-z0-9_\-]+\.py(?:::[A-Za-z_][A-Za-z0-9_]*)*", re.ASCII)


class ArtifactError(Exception):
    """Fixed-code failure: missing|expired|invalid|incomplete|download_unavailable. Never carries content."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def artifact_name(run_id: int, attempt: int, slice_no: int) -> str:
    return f"{ARTIFACT_PREFIX}-{run_id}-{attempt}-{slice_no}"


# --- schema ---------------------------------------------------------------------


def _int_in(value: Any, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def canonical_test_id(raw: Any) -> str | None:
    """Raw runner node id -> canonical identity, or None when it cannot be represented exactly."""
    if not isinstance(raw, str):
        return None
    test_id = raw.split("[", 1)[0]
    if len(test_id) > MAX_TEST_ID or not test_id.isascii() or _TEST_ID.fullmatch(test_id) is None:
        return None
    return test_id


def _valid_entry(entry: Any) -> bool:
    return (isinstance(entry, dict) and set(entry) == ENTRY_KEYS and entry["status"] == STATUS
            and entry["retried"] in RETRIED and isinstance(entry["test_id"], str)
            and canonical_test_id(entry["test_id"]) == entry["test_id"])


def _valid_header(data: Any) -> bool:
    ok = isinstance(data, dict) and set(data) == KEYS
    ok = ok and data["version"] == VERSION and type(data["version"]) is int
    ok = ok and isinstance(data["repository"], str) and _REPO.fullmatch(data["repository"]) is not None
    ok = ok and _int_in(data["run_id"], 1, MAX_INT) and _int_in(data["run_attempt"], 1, MAX_INT)
    ok = ok and all(isinstance(data[k], str) and _SHA.fullmatch(data[k]) for k in ("head_sha", "source_sha"))
    ok = ok and _int_in(data["slices"], 1, MAX_SLICES) and _int_in(data["slice"], 1, MAX_SLICES)
    return ok and data["slice"] <= data["slices"] and type(data["complete"]) is bool


def validate_metadata(data: Any) -> dict[str, Any]:
    """Strict exact-key schema check; returns ``data`` or raises ``ArtifactError('invalid')``."""
    flaky = data.get("flaky") if isinstance(data, dict) else None
    ok = _valid_header(data) and isinstance(flaky, list) and len(flaky) <= MAX_FLAKY
    flaky = flaky if isinstance(flaky, list) else []
    ok = ok and all(_valid_entry(e) for e in flaky) and (data["complete"] or not flaky)
    ok = ok and len({e["test_id"] for e in flaky}) == len(flaky)
    if not ok:
        raise ArtifactError("invalid")
    return data


def check_expected(meta: dict[str, Any], *, repo: str, run_id: int, attempt: int, head_sha: str,
                   slice_no: int, slices: int) -> None:
    """The artifact belongs to exactly this run attempt/slice; ``source_sha`` is provenance only."""
    same = (meta["repository"].lower() == repo.lower() and meta["run_id"] == run_id
            and meta["run_attempt"] == attempt and meta["head_sha"] == head_sha
            and meta["slice"] == slice_no and meta["slices"] == slices)
    if not same:
        raise ArtifactError("invalid")
    if not meta["complete"]:
        raise ArtifactError("incomplete")


# --- exporter -------------------------------------------------------------------


def _read_source_report(path: Path) -> list[dict[str, str]] | None:
    """Canonical entries from the runner report, or None when it is missing/oversize/malformed/unrepresentable."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_SOURCE_REPORT + 1)
        if len(raw) > MAX_SOURCE_REPORT:
            return None
        report = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(report, dict) or report.get("version") != 1 or type(report.get("version")) is not int:
        return None
    flaky = report.get("flaky")
    if not isinstance(flaky, list):
        return None
    found: dict[str, str] = {}
    for item in flaky:
        test_id = canonical_test_id(item.get("test_id")) if isinstance(item, dict) else None
        retried = item.get("retried") if isinstance(item, dict) else None
        if test_id is None or retried not in RETRIED:
            return None
        found.setdefault(test_id, retried)
    if len(found) > MAX_FLAKY:
        return None
    return [{"test_id": t, "status": STATUS, "retried": found[t]} for t in sorted(found)]


def build_metadata(report: Path, *, env: dict[str, str], source_sha: str, slice_no: int, slices: int) -> dict[str, Any]:
    entries = _read_source_report(report)
    try:
        run_id, attempt = int(env["GITHUB_RUN_ID"]), int(env["GITHUB_RUN_ATTEMPT"])
        repo, head_sha = env["GITHUB_REPOSITORY"], env["FLAKY_HEAD_SHA"]
    except (KeyError, ValueError):
        raise ArtifactError("invalid") from None
    meta = {"version": VERSION, "repository": repo, "run_id": run_id, "run_attempt": attempt,
            "head_sha": head_sha, "source_sha": source_sha, "slice": slice_no, "slices": slices,
            "complete": entries is not None, "flaky": entries or []}
    return validate_metadata(meta)


def _git_head() -> str:
    proc = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def export_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--slice", type=int, required=True, dest="slice_no")
    ap.add_argument("--slices", type=int, required=True)
    args = ap.parse_args(argv)
    try:
        meta = build_metadata(args.report, env=dict(os.environ), source_sha=_git_head(),
                              slice_no=args.slice_no, slices=args.slices)
        payload = json.dumps(meta, sort_keys=True, separators=(",", ":")) + "\n"
        if len(payload.encode("utf-8")) > MAX_JSON:
            meta.update(complete=False, flaky=[])
            payload = json.dumps(meta, sort_keys=True, separators=(",", ":")) + "\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.output.with_name(args.output.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(args.output)
    except (ArtifactError, OSError):
        print("flaky_artifact: FAILED metadata export")
        return 1
    state = "complete" if meta["complete"] else "incomplete (source report unavailable)"
    print(f"flaky_artifact: slice {args.slice_no}/{args.slices} {state}, {len(meta['flaky'])} flaky test id(s)")
    return 0


# --- consumer: ZIP decoding -----------------------------------------------------


def _reject_dup_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate")
        out[key] = value
    return out


def _no_constants(_name: str) -> Any:
    raise ValueError("constant")


def decode_zip(data: bytes) -> dict[str, Any]:
    """The validated metadata from an in-memory artifact ZIP. Nothing is extracted to disk."""
    if len(data) > MAX_ZIP:
        raise ArtifactError("invalid")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
            if len(infos) != 1:
                raise ArtifactError("invalid")
            info = infos[0]
            mode = info.external_attr >> 16
            bad = (info.filename != MEMBER or info.is_dir() or info.flag_bits & 0x1
                   or stat.S_IFMT(mode) not in (0, stat.S_IFREG) or info.file_size > MAX_JSON
                   or info.file_size > MAX_RATIO * max(info.compress_size, 1))
            if bad:
                raise ArtifactError("invalid")
            with zf.open(info) as member:
                raw = member.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise ArtifactError("invalid")
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_dup_keys, parse_constant=_no_constants)
    except ArtifactError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError, RuntimeError, EOFError, RecursionError,
            NotImplementedError, zlib.error):
        raise ArtifactError("invalid") from None
    return validate_metadata(parsed)


# --- consumer: download ---------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _fetch(url: str, headers: dict[str, str], limit: int) -> tuple[int, str | None, bytes]:
    """One HTTP GET without following redirects: ``(status, Location, body up to limit + 1 bytes)``."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=60) as resp:
            return resp.status, resp.headers.get("Location"), resp.read(limit + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location") if exc.headers else None, b""


def _signed_url_ok(url: Any) -> bool:
    if not isinstance(url, str) or not url.isascii():
        return False
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and "@" not in parts.netloc and host is not None
            and host.endswith(ALLOWED_HOST_SUFFIXES) and port in (None, 443))


def download_zip(repo: str, artifact_id: int, token: str) -> bytes:
    """Artifact ZIP bytes through the canonical API URL (authenticated) and its signed redirect (not)."""
    if not (_REPO.fullmatch(repo) and _int_in(artifact_id, 1, MAX_INT)):
        raise ArtifactError("invalid")
    api_url = f"https://{API_HOST}/repos/{repo}/actions/artifacts/{artifact_id}/zip"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "hermes-ci-flaky-cards"}
    try:
        status, location, _ = _fetch(api_url, headers, 0)
        if status == 410:
            raise ArtifactError("expired")
        if status not in (301, 302, 303, 307, 308) or location is None or not _signed_url_ok(location):
            raise ArtifactError("download_unavailable")
        status, _, body = _fetch(location, {"User-Agent": "hermes-ci-flaky-cards"}, MAX_ZIP)
    except (OSError, ValueError, http.client.HTTPException):
        raise ArtifactError("download_unavailable") from None
    if status != 200:
        raise ArtifactError("download_unavailable")
    if len(body) > MAX_ZIP:
        raise ArtifactError("invalid")
    return body


if __name__ == "__main__":
    sys.exit(export_main())
