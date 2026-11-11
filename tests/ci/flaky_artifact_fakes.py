"""Fake GitHub Actions API + metadata artifacts for the flaky-card poll tests."""
from __future__ import annotations

import io
import json
import zipfile
from typing import Any

from scripts.ci import flaky_artifact as fa

API = "https://api.github.com/repos/o/r"
SHA = "a" * 40
SRC = "b" * 40


def meta(run_id: int = 5, attempt: int = 1, slice_no: int = 1, slices: int = 1, tests: tuple[str, ...] = (),
         complete: bool = True, head_sha: str = SHA, **over: Any) -> dict[str, Any]:
    data = {"version": 1, "repository": "o/r", "run_id": run_id, "run_attempt": attempt, "head_sha": head_sha,
            "source_sha": SRC, "slice": slice_no, "slices": slices, "complete": complete,
            "flaky": [{"test_id": t, "status": "passed_on_retry", "retried": "tests" if "::" in t else "file"}
                      for t in tests]}
    data.update(over)
    return data


def make_zip(payload: Any, member: str = fa.MEMBER, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        zf.writestr(member, raw)
    return buf.getvalue()


def job(job_id: int, slice_no: int = 1, slices: int = 1, conclusion: str = "success", status: str = "completed",
        name: str | None = None) -> dict[str, Any]:
    return {"id": job_id, "name": name or f"Python tests / Run tests ({slice_no}/{slices})",
            "status": status, "conclusion": conclusion, "html_url": "https://example.invalid/untrusted"}


def artifact(art_id: int, run_id: int = 5, attempt: int = 1, slice_no: int = 1, expired: bool = False,
             name: str | None = None) -> dict[str, Any]:
    return {"id": art_id, "name": name or fa.artifact_name(run_id, attempt, slice_no), "expired": expired,
            "archive_download_url": "https://evil.invalid/zip"}


class FakeGitHub:
    """Runs, jobs, artifacts and ZIP bodies keyed by id. ``fail`` maps a URL substring to an exception."""

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []
        self.jobs: dict[tuple[int, int], list[dict[str, Any]]] = {}
        self.artifacts: dict[int, list[dict[str, Any]]] = {}
        self.zips: dict[int, bytes | Exception] = {}
        self.fail: dict[str, Exception] = {}
        self.urls: list[str] = []
        self.downloads: list[int] = []

    def run(self, run_id: int = 5, attempt: int = 1, head_sha: str = SHA) -> dict[str, Any]:
        run = {"id": run_id, "run_attempt": attempt, "head_sha": head_sha, "head_branch": "evil branch text",
               "html_url": "https://example.invalid/run"}
        self.runs.append(run)
        return run

    def simple(self, run_id: int = 5, tests: tuple[str, ...] = (), art_id: int = 100) -> None:
        """One run, one slice, one complete artifact."""
        self.run(run_id)
        self.jobs[(run_id, 1)] = [job(9)]
        self.artifacts[run_id] = [artifact(art_id, run_id)]
        self.zips[art_id] = make_zip(meta(run_id, tests=tests))

    def gh(self, url: str, token: str) -> Any:
        self.urls.append(url)
        for needle, exc in self.fail.items():
            if needle in url:
                raise exc
        base, _, query = url.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        per_page, page = int(params.get("per_page", 100)), int(params.get("page", 1))
        window = slice((page - 1) * per_page, page * per_page)
        if base == f"{API}/actions/runs":
            return {"workflow_runs": sorted(self.runs, key=lambda r: -r["id"])[window]}
        parts = base.removeprefix(f"{API}/actions/runs/").split("/")
        if parts[1:2] == ["attempts"] and parts[-1] == "jobs":
            return {"jobs": self.jobs.get((int(parts[0]), int(parts[2])), [])[window]}
        if parts[1:] == ["artifacts"]:
            return {"artifacts": self.artifacts.get(int(parts[0]), [])[window]}
        raise AssertionError("unexpected API call")

    def download(self, repo: str, artifact_id: int, token: str) -> bytes:
        self.downloads.append(artifact_id)
        body = self.zips[artifact_id]
        if isinstance(body, Exception):
            raise body
        return body
