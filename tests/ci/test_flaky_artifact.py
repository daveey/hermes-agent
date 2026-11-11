"""scripts/ci/flaky_artifact.py: metadata-only flaky artifact schema, exporter, ZIP decoding and download."""
from __future__ import annotations

import http.server
import io
import json
import os
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import pytest

from scripts.ci import flaky_artifact as fa
from tests.ci.flaky_artifact_fakes import SHA, SRC, make_zip, meta

_ROOT = Path(__file__).resolve().parents[2]
SECRET = "SECRET-excerpt-9f3a"


def _env(**over):
    env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "5", "GITHUB_RUN_ATTEMPT": "2", "FLAKY_HEAD_SHA": SHA}
    env.update(over)
    return env


def _source(tmp_path, flaky, version=1):
    path = tmp_path / "slice-1.json"
    path.write_text(json.dumps({"version": version, "flaky": flaky}))
    return path


def _build(path, **env):
    return fa.build_metadata(path, env=_env(**env), source_sha=SRC, slice_no=1, slices=2)


# --- exporter -------------------------------------------------------------------


def test_export_keeps_only_canonical_identity_and_provenance(tmp_path):
    path = _source(tmp_path, [
        {"test_id": f"tests/a/test_x.py::TestC::test_a[{SECRET}-param]", "retried": "tests",
         "first_attempt_excerpt": f"E   {SECRET}", "extra": SECRET},
        {"test_id": "tests/a/test_x.py::TestC::test_a[other]", "retried": "tests", "first_attempt_excerpt": SECRET},
        {"test_id": "tests/b/test_y.py", "retried": "file"},
    ])
    out = _build(path)
    assert out == {"version": 1, "repository": "o/r", "run_id": 5, "run_attempt": 2, "head_sha": SHA,
                   "source_sha": SRC, "slice": 1, "slices": 2, "complete": True,
                   "flaky": [{"test_id": "tests/a/test_x.py::TestC::test_a", "status": "passed_on_retry", "retried": "tests"},
                             {"test_id": "tests/b/test_y.py", "status": "passed_on_retry", "retried": "file"}]}
    assert SECRET not in json.dumps(out) and "param" not in json.dumps(out)


@pytest.mark.parametrize("test_id", [
    "tests/../x.py", "tests/a b/x.py", "src/x.py", "tests/x.txt", "tests/x.py::bad name", "tests/é.py",
    "tests/" + "a" * 600 + ".py", "tests/x.py::", "tests//x.py", "/tests/x.py", "tests/x.py\n",
    "tests/x.py::test_a/../b", "tests/.hidden/x.py", 7, None,
])
def test_unrepresentable_identity_makes_the_report_incomplete_not_truncated(tmp_path, test_id):
    path = _source(tmp_path, [{"test_id": "tests/ok.py", "retried": "file"}, {"test_id": test_id, "retried": "tests"}])
    out = _build(path)
    assert out["complete"] is False and out["flaky"] == []


def _write_bytes(tmp, name, data):
    (tmp / name).write_bytes(data)
    return tmp / name


@pytest.mark.parametrize("make", [
    lambda tmp: tmp / "absent.json",
    lambda tmp: _source(tmp, [], version=2),
    lambda tmp: _source(tmp, {"not": "a list"}),
    lambda tmp: _source(tmp, [{"test_id": "tests/x.py", "retried": "sometimes"}]),
    lambda tmp: _write_bytes(tmp, "bad.json", b"{nope"),
    lambda tmp: _write_bytes(tmp, "list.json", b"[]"),
    lambda tmp: _write_bytes(tmp, "nokey.json", b'{"version": 1}'),
])
def test_missing_or_malformed_source_report_is_incomplete_never_empty_success(tmp_path, make):
    out = _build(make(tmp_path))
    assert out["complete"] is False and out["flaky"] == []
    fa.validate_metadata(out)


def test_oversize_source_report_is_incomplete(tmp_path, monkeypatch):
    path = _source(tmp_path, [])
    monkeypatch.setattr(fa, "MAX_SOURCE_REPORT", 4)
    assert _build(path)["complete"] is False


def test_empty_flaky_list_is_a_complete_empty_report(tmp_path):
    out = _build(_source(tmp_path, []))
    assert out["complete"] is True and out["flaky"] == []


@pytest.mark.parametrize("env", [{"GITHUB_REPOSITORY": "no-slash"}, {"GITHUB_RUN_ID": "x"}, {"GITHUB_RUN_ID": "0"},
                                 {"FLAKY_HEAD_SHA": "ABC"}, {"GITHUB_RUN_ATTEMPT": "-1"}])
def test_bad_provenance_env_is_a_metadata_failure(tmp_path, env):
    with pytest.raises(fa.ArtifactError):
        _build(_source(tmp_path, []), **env)


def test_cli_writes_metadata_and_exits_zero_for_an_unavailable_report(tmp_path):
    out = tmp_path / "meta" / "flaky-metadata.json"
    proc = subprocess.run([sys.executable, str(_ROOT / "scripts/ci/flaky_artifact.py"), "--report",
                           str(tmp_path / "absent.json"), "--output", str(out), "--slice", "2", "--slices", "3"],
                          capture_output=True, text=True, cwd=_ROOT, env={**os.environ, **_env()}, check=False)
    assert proc.returncode == 0, proc.stderr
    data = fa.validate_metadata(json.loads(out.read_text()))
    assert data["complete"] is False and data["slice"] == 2 and data["slices"] == 3


def test_cli_marks_oversize_serialized_metadata_incomplete(tmp_path):
    entries = [{"test_id": f"tests/{i:05d}_" + "a" * 490 + ".py", "retried": "file"}
               for i in range(fa.MAX_FLAKY)]
    assert all(fa.canonical_test_id(e["test_id"]) for e in entries)
    report = _source(tmp_path, entries)
    out = tmp_path / fa.MEMBER
    proc = subprocess.run([sys.executable, str(_ROOT / "scripts/ci/flaky_artifact.py"), "--report",
                           str(report), "--output", str(out), "--slice", "1", "--slices", "1"],
                          capture_output=True, text=True, cwd=_ROOT, env={**os.environ, **_env()}, check=False)
    assert proc.returncode == 0, proc.stderr
    assert len(out.read_bytes()) <= fa.MAX_JSON
    data = fa.decode_zip(make_zip(out.read_bytes()))
    assert data["complete"] is False and data["flaky"] == []


def test_cli_exits_nonzero_on_metadata_failure(tmp_path, capsys, monkeypatch):
    for key, value in _env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(fa, "_git_head", lambda: SRC)
    path = _source(tmp_path, [{"test_id": "tests/x.py", "retried": "file"}])
    rc = fa.export_main(["--report", str(path), "--output", str(tmp_path / "o.json"), "--slice", "3", "--slices", "2"])
    assert rc == 1 and not (tmp_path / "o.json").exists()
    assert "FAILED" in capsys.readouterr().out
    rc = fa.export_main(["--report", str(path), "--output", str(tmp_path / "o.json"), "--slice", "1", "--slices", "2"])
    assert rc == 0 and json.loads((tmp_path / "o.json").read_text())["flaky"][0]["test_id"] == "tests/x.py"


# --- schema ---------------------------------------------------------------------


def test_valid_metadata_passes():
    assert fa.validate_metadata(meta(tests=("tests/x.py::test_a", "tests/y.py"), slices=64, slice_no=64))


@pytest.mark.parametrize("change", [
    {"version": 2}, {"version": True}, {"version": 1.0}, {"extra": 1}, {"repository": "bad repo/x"},
    {"repository": "o/r/z"}, {"run_id": True}, {"run_id": 0}, {"run_id": 2**63}, {"run_id": "5"},
    {"run_attempt": 0}, {"head_sha": "A" * 40}, {"head_sha": "a" * 39}, {"source_sha": None},
    {"slice": 0}, {"slice": 3, "slices": 2}, {"slices": 65, "slice": 1}, {"slice": True},
    {"complete": 1}, {"complete": "true"}, {"flaky": None}, {"flaky": "x"},
    {"complete": False, "flaky": [{"test_id": "tests/x.py", "status": "passed_on_retry", "retried": "file"}]},
    {"flaky": [{"test_id": "tests/x.py", "status": "failed", "retried": "file"}]},
    {"flaky": [{"test_id": "tests/x.py", "status": "passed_on_retry", "retried": "all"}]},
    {"flaky": [{"test_id": "tests/x.py", "status": "passed_on_retry", "retried": "file", "excerpt": "x"}]},
    {"flaky": [{"test_id": "tests/x.py", "status": "passed_on_retry"}]},
    {"flaky": [{"test_id": "tests/x.py[p]", "status": "passed_on_retry", "retried": "file"}]},
    {"flaky": [{"test_id": "tests/x.py", "status": "passed_on_retry", "retried": "file"}] * 2},
    {"flaky": ["tests/x.py"]},
])
def test_schema_rejects_with_a_fixed_code_and_no_content(change):
    with pytest.raises(fa.ArtifactError) as err:
        fa.validate_metadata({**meta(), **change})
    assert err.value.code == "invalid" and str(err.value) == "invalid"


def test_schema_rejects_missing_keys_and_non_objects():
    short = meta()
    del short["slice"]
    for bad in (short, [], "x", None, 5):
        with pytest.raises(fa.ArtifactError):
            fa.validate_metadata(bad)


def test_flaky_list_bound():
    tests = tuple(f"tests/t{i}.py" for i in range(fa.MAX_FLAKY + 1))
    assert len(fa.validate_metadata(meta(tests=tests[:-1]))["flaky"]) == fa.MAX_FLAKY
    with pytest.raises(fa.ArtifactError):
        fa.validate_metadata(meta(tests=tests))


def test_check_expected_binds_run_attempt_slice_and_head_but_not_source_sha():
    base = dict(repo="O/R", run_id=5, attempt=1, head_sha=SHA, slice_no=1, slices=2)
    fa.check_expected(meta(slices=2, source_sha="c" * 40), **base)       # source_sha is provenance only
    for change in ({"run_id": 6}, {"attempt": 2}, {"head_sha": "d" * 40}, {"slice_no": 2}, {"slices": 3}, {"repo": "o/x"}):
        with pytest.raises(fa.ArtifactError) as err:
            fa.check_expected(meta(slices=2), **{**base, **change})
        assert err.value.code == "invalid"
    with pytest.raises(fa.ArtifactError) as err:
        fa.check_expected(meta(slices=2, complete=False), **base)
    assert err.value.code == "incomplete"


# --- ZIP ------------------------------------------------------------------------


def test_corrupt_deflate_data_is_a_fixed_invalid_code():
    body = bytearray(make_zip(meta()))
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        info = zf.infolist()[0]
        start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    body[start] = 0x07  # final block with reserved BTYPE=3: decompression, not CRC, fails
    with pytest.raises(fa.ArtifactError) as err:
        fa.decode_zip(bytes(body))
    assert err.value.code == "invalid" and str(err.value) == "invalid"


def test_decode_zip_round_trip():
    assert fa.decode_zip(make_zip(meta(tests=("tests/x.py",))))["flaky"][0]["test_id"] == "tests/x.py"


def _zip_with(infos):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for info, data in infos:
            zf.writestr(info, data)
    return buf.getvalue()


def _encrypted(body):
    """The same archive with the encryption bit set in its central directory (zipfile rewrites it on write)."""
    at = body.index(b"PK\x01\x02") + 8
    return body[:at] + bytes([body[at] | 1]) + body[at + 1:]


def _info(name, mode=0o100644, flags=0):
    info = zipfile.ZipInfo(name)
    info.external_attr = mode << 16
    info.flag_bits |= flags
    return info


@pytest.mark.parametrize("body", [
    b"not a zip",
    b"",
    make_zip(meta(), member="other.json"),
    make_zip(meta(), member="dir/flaky-metadata.json"),
    make_zip(meta(), member="../flaky-metadata.json"),
    make_zip(b"{not json"),
    make_zip(b'{"version": 1, "version": 1}'),
    make_zip(b"[NaN]"),
    make_zip(b"\xff\xfe"),
    make_zip(b"[" * 200000),
    make_zip({**meta(), "extra": SECRET}),
    _zip_with([(_info("flaky-metadata.json"), json.dumps(meta())), (_info("second.json"), "{}")]),
    _zip_with([(_info("flaky-metadata.json", mode=0o120777), "target")]),
    _zip_with([(_info("flaky-metadata.json", mode=0o040755), "")]),
    _encrypted(make_zip(meta())),
    _zip_with([(_info("flaky-metadata.json/"), "")]),
    _zip_with([]),
])
def test_decode_zip_rejects_with_a_fixed_code(body):
    with pytest.raises(fa.ArtifactError) as err:
        fa.decode_zip(body)
    assert err.value.code == "invalid" and SECRET not in str(err.value)


def test_decode_zip_size_and_ratio_limits(monkeypatch):
    body = make_zip(meta())
    with monkeypatch.context() as m:
        m.setattr(fa, "MAX_ZIP", len(body) - 1)
        with pytest.raises(fa.ArtifactError):
            fa.decode_zip(body)
    with monkeypatch.context() as m:
        m.setattr(fa, "MAX_JSON", 10)
        with pytest.raises(fa.ArtifactError):
            fa.decode_zip(body)
    with monkeypatch.context() as m:
        m.setattr(fa, "MAX_RATIO", 1)
        with pytest.raises(fa.ArtifactError):
            fa.decode_zip(make_zip(b"[" + b" " * 5000 + b"]"))                   # compresses far past ratio 1


# --- download -------------------------------------------------------------------


class Hops:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, headers, limit):
        self.calls.append((url, dict(headers), limit))
        reply = self.responses.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


SIGNED = "https://productionresultssa0.blob.core.windows.net/a/b?sig=x"


def test_download_authenticates_only_the_canonical_api_hop(monkeypatch):
    hops = Hops((302, SIGNED, b""), (200, None, b"ZIPBYTES"))
    monkeypatch.setattr(fa, "_fetch", hops)
    assert fa.download_zip("o/r", 77, "tok") == b"ZIPBYTES"
    (api_url, api_headers, _), (blob_url, blob_headers, limit) = hops.calls
    assert api_url == "https://api.github.com/repos/o/r/actions/artifacts/77/zip"
    assert api_headers["Authorization"] == "Bearer tok"
    assert blob_url == SIGNED and "Authorization" not in blob_headers and limit == fa.MAX_ZIP


@pytest.mark.parametrize("location", [
    None, "http://productionresultssa0.blob.core.windows.net/x", "https://evil.example/x",
    "https://user:pw@productionresultssa0.blob.core.windows.net/x", "https://api.github.com/x",
    "https://blob.core.windows.net.evil.example/x", "ftp://a.blob.core.windows.net/x", "file:///etc/passwd",
    "https://a.blob.core.windows.net:8443/x", "https://evil.example@a.blob.core.windows.net/x",
    "https://a.blob.core.windows.net\\@evil.example/x", "//a.blob.core.windows.net/x",
])
def test_download_rejects_unsafe_redirect_targets_without_calling_them(monkeypatch, location):
    hops = Hops((302, location, b""))
    monkeypatch.setattr(fa, "_fetch", hops)
    with pytest.raises(fa.ArtifactError) as err:
        fa.download_zip("o/r", 77, "tok")
    assert err.value.code == "download_unavailable" and len(hops.calls) == 1


def test_download_accepts_the_actions_host(monkeypatch):
    monkeypatch.setattr(fa, "_fetch", Hops((302, "https://pipelines.actions.githubusercontent.com/x", b""),
                                           (200, None, b"z")))
    assert fa.download_zip("o/r", 1, "t") == b"z"


@pytest.mark.parametrize("responses,code", [
    ([(410, None, b"")], "expired"),
    ([(404, None, b"")], "download_unavailable"),
    ([(403, None, b"")], "download_unavailable"),
    ([(200, None, b"direct body")], "download_unavailable"),
    ([(302, SIGNED, b""), (302, SIGNED, b"")], "download_unavailable"),     # no second redirect is followed
    ([(302, SIGNED, b""), (403, None, b"")], "download_unavailable"),
    ([(302, SIGNED, b""), (200, None, b"x" * (fa.MAX_ZIP + 1))], "invalid"),
    ([OSError(SECRET)], "download_unavailable"),
    ([(302, SIGNED, b""), TimeoutError(SECRET)], "download_unavailable"),
])
def test_download_failures_are_fixed_codes(monkeypatch, responses, code):
    monkeypatch.setattr(fa, "_fetch", Hops(*responses))
    with pytest.raises(fa.ArtifactError) as err:
        fa.download_zip("o/r", 77, "tok")
    assert err.value.code == code and SECRET not in str(err.value) and err.value.__cause__ is None


@pytest.mark.parametrize("repo,art_id", [("bad", 1), ("o/r", 0), ("o/r", True), ("o/r/x", 1)])
def test_download_refuses_malformed_inputs_before_any_request(monkeypatch, repo, art_id):
    hops = Hops()
    monkeypatch.setattr(fa, "_fetch", hops)
    with pytest.raises(fa.ArtifactError):
        fa.download_zip(repo, art_id, "tok")
    assert hops.calls == []


def test_fetch_never_follows_a_redirect():
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/followed")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"x" * 100)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        assert fa._fetch(f"{base}/start", {"Authorization": "Bearer t"}, 10) == (302, "/followed", b"")
        status, _, body = fa._fetch(f"{base}/other", {}, 10)
        assert status == 200 and len(body) == 11                                  # limit + 1: oversize is detectable
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    assert seen == ["/start", "/other"]
