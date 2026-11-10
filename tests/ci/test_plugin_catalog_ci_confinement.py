"""The catalog admission gate must validate the pinned clone's own files only.

These tests execute the real ``run:`` blocks of plugin-catalog-ci.yml against
fixture git repos: the changed-entries step against a PR merge commit, and the
pinned-source step against an origin repo served under an https URL through a
git ``url.insteadOf`` rewrite. One case per closed bypass, plus the legitimate
flows that must keep passing.

Both jobs take only catalog bytes from the PR: they export them from the merge
commit's tree, drop the PR checkout and check out the merge commit's first
parent (the trusted base) before running any tool. The pipeline tests below run
that whole sequence, with ``actions/checkout`` emulated by a git clone, and
prove a PR's own scanner, sandbox wrapper, validator, action or core never runs.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import hermes_yaml as yaml
import pytest

pytestmark = pytest.mark.platforms("linux")

ROOT = Path(__file__).resolve().parents[2]
# CATALOG_CI_WORKFLOW points the harness at another workflow copy (e.g.
# `git show origin/main:...`) to prove a case fails on the old gate.
WORKFLOW = Path(os.environ.get(
    "CATALOG_CI_WORKFLOW", ROOT / ".github/workflows/plugin-catalog-ci.yml"))
REPO_URL = "https://fixture.invalid/repo"
PINNED_JOB = "pinned-source-validate"


def _job_steps(job: str | None = None):
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for name, spec in data["jobs"].items():
        if job is None or name == job:
            yield from spec["steps"]


def _find_step(name_prefix: str, job: str | None = None) -> dict:
    for step in _job_steps(job):
        if step.get("name", "").startswith(name_prefix):
            return step
    raise LookupError(name_prefix)


def _step(name_prefix: str, job: str | None = None) -> str:
    # The workflow expands ${{ github.base_ref }} before bash sees it.
    return _find_step(name_prefix, job)["run"].replace("${{ github.base_ref }}", "main")


def _step_env(name_prefix: str, job: str | None = None) -> dict:
    return {k: str(v) for k, v in (_find_step(name_prefix, job).get("env") or {}).items()}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, msg: str = "x") -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)
    return _git(repo, "rev-parse", "HEAD")


def _run(script_text: str, tmp_path: Path, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    # bash <file>, not bash -c: the run text mentions "update", which trips the
    # live-system guard's hermes-update heuristic when it sits inside the argv.
    script = tmp_path / "step.sh"
    script.write_text(script_text, encoding="utf-8")
    return subprocess.run(
        ["bash", str(script)], env=env, cwd=cwd,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300, check=False)


# ── pinned-source step ────────────────────────────────────────────────────────

@pytest.fixture
def origin(tmp_path):
    repo = tmp_path / "origin"
    (repo / "plugin").mkdir(parents=True)
    (repo / "plugin" / "plugin.yaml").write_text("name: fixture\n", encoding="utf-8")
    (repo / "plugin.yaml").write_text("name: root-fixture\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _commit(repo)
    (tmp_path / "mktmp").mkdir()  # mktemp lands here so escape paths are predictable
    return repo


def _cand(tmp_path: Path) -> Path:
    """Where the changed-entries step exports the PR's entry bytes."""
    return tmp_path / "cand"


def _rel(tmp_path: Path, entry: Path) -> str:
    return entry.relative_to(_cand(tmp_path)).as_posix()


def _entry(tmp_path: Path, name: str, sha: str, **fields) -> Path:
    path = _cand(tmp_path) / "plugin-catalog" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(
        {"repo": REPO_URL, "sha": sha, "subdir": "plugin", **fields}), encoding="utf-8")
    return path


def _planted(tmp_path: Path) -> Path:
    planted = tmp_path / "planted"
    planted.mkdir(exist_ok=True)
    (planted / "plugin.yaml").write_text("name: planted\n", encoding="utf-8")
    return planted


SANDBOX_STUB = """\
#!/usr/bin/env python3
# Stand-in for scripts/ci/plugin_validation_sandbox.py: record the call, then run
# the command after `--` unconfined with the caller's full environment, as the
# old unsandboxed step did. SANDBOX_STUB_EXIT forces a setup failure instead.
import json, os, sys
with open(os.environ["SANDBOX_STUB_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
if os.environ.get("SANDBOX_STUB_EXIT"):
    sys.exit(int(os.environ["SANDBOX_STUB_EXIT"]))
argv = sys.argv[1:]
os.execvp(argv[argv.index("--") + 1], argv[argv.index("--") + 1:])
"""


def _gate(tmp_path: Path, origin: Path, entries: list, *,
          hermes_stub: str = "#!/bin/sh\nexit 0\n", sandbox: str = "stub",
          extra_env: dict | None = None, workspace: Path | None = None,
          candidates: Path | None = None) -> subprocess.CompletedProcess:
    """Run the pinned-source step. sandbox="stub" swaps in a recording,
    unconfined wrapper (no bubblewrap needed); sandbox="real" runs the real
    wrapper from this checkout, with `hermes` inside the PM home as setup-pm
    puts it. `workspace` runs the step from that checkout as-is (its own
    scanner and wrapper); entries are paths under `candidates` (default:
    the fixture candidate dir) or already candidate-relative strings."""
    pm_home = tmp_path / "pmhome"
    bin_dir = (pm_home if sandbox == "real" else tmp_path) / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "hermes"
    stub.write_text(hermes_stub, encoding="utf-8")
    stub.chmod(0o755)
    cand = candidates or _cand(tmp_path)
    cand.mkdir(parents=True, exist_ok=True)
    if workspace is None:
        workspace = ROOT
        if sandbox == "stub":
            workspace = tmp_path / "workspace"
            (workspace / "scripts" / "ci").mkdir(parents=True, exist_ok=True)
            scan = workspace / "scripts" / "scan_plugin_payloads.py"
            if not scan.exists():
                scan.symlink_to(ROOT / "scripts" / "scan_plugin_payloads.py")
            (workspace / "scripts" / "ci" / "plugin_validation_sandbox.py").write_text(
                SANDBOX_STUB, encoding="utf-8")
    names = [e if isinstance(e, str) else e.relative_to(cand).as_posix() for e in entries]
    env = {
        **os.environ,
        "CHANGED_FILES": "\n".join(names),
        "CATALOG_CANDIDATES": str(cand),
        "TMPDIR": str(tmp_path / "mktmp"),
        # The test interpreter first: the step's `python3 -I` parser needs
        # ruamel.yaml from site-packages, as setup-pm provides it in CI.
        "PATH": f"{bin_dir}:{Path(sys.executable).parent}:{os.environ['PATH']}",
        # CI runs the step from the hermes-agent checkout; `-I` must ignore this.
        "PYTHONPATH": str(ROOT),
        "GIT_TERMINAL_PROMPT": "0",
        "GITHUB_WORKSPACE": str(workspace),
        "HERMES_HOME": str(pm_home),
        "SANDBOX_STUB_LOG": str(tmp_path / "sandbox-calls.jsonl"),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.{origin.as_uri()}.insteadOf",
        "GIT_CONFIG_VALUE_0": REPO_URL,
        **(extra_env or {}),
    }
    pm_home.mkdir(exist_ok=True)
    (tmp_path / "mktmp").mkdir(exist_ok=True)
    return _run(_step("Clone each entry"), tmp_path, tmp_path, env)


def _traversal_subdir(tp, origin, sha):
    # mktemp gives TMPDIR/tmp.XXX; one '..' reaches TMPDIR, then walk to planted/.
    subdir = "../" + os.path.relpath(_planted(tp), tp / "mktmp")
    return _entry(tp, "evil.yaml", sha, subdir=subdir), "must be a relative path"


def _symlinked_subdir(tp, origin, sha):
    (origin / "link").symlink_to(_planted(tp))
    return _entry(tp, "evil.yaml", _commit(origin), subdir="link"), "outside the pinned clone"


def _symlink_behind_in_clone_link(tp, origin, sha):
    # find does not descend into plugin/ext (an in-clone link), so a scan of
    # PLUGIN_DIR alone never sees lib/evil pointing out of the clone.
    (origin / "lib").mkdir()
    (origin / "lib" / "evil").symlink_to(_planted(tp) / "plugin.yaml")
    (origin / "plugin" / "ext").symlink_to("../lib")
    return _entry(tp, "evil.yaml", _commit(origin)), "symlink in the repo resolves outside"


def _tag_object_sha(tp, origin, sha):
    _git(origin, "tag", "-a", "v1", "-m", "x")
    return _entry(tp, "evil.yaml", _git(origin, "rev-parse", "v1")), "is not a commit object"


def _newline_sha(tp, origin, sha):
    return _entry(tp, "evil.yaml", sha + "\n"), "sha must be exactly 40 lowercase hex"


def _log_forging_repo(tp, origin, sha):
    return (_entry(tp, "evil.yaml", sha, repo=REPO_URL + "\n::error::forged"),
            "repo must be an https:// git URL")


def _symlinked_entry_file(tp, origin, sha):
    real = _entry(tp, "real.yaml", sha)
    link = real.with_name("link.yaml")
    link.symlink_to(real.name)
    return link, "must be a regular file"


def _entry_outside_the_candidates(tp, origin, sha):
    # A listed name that climbs out of the candidate dir is never read.
    _entry(tp, "real.yaml", sha)
    return "plugin-catalog/../plugin-catalog/real.yaml", "must be a regular file"


def _self_updater_in_spaced_dir(tp, origin, sha):
    (origin / "plugin" / "evil dir").mkdir()
    (origin / "plugin" / "evil dir" / "dl.js").write_text(
        "releases/download fs.writeFileSync(", encoding="utf-8")
    return _entry(tp, "evil.yaml", _commit(origin)), "self-updating code"


def _bundled_native_binary(tp, origin, sha):
    # A PE binary committed in the plugin tree (README rule 17). Header, not ext.
    (origin / "plugin" / "runtime").mkdir()
    (origin / "plugin" / "runtime" / "host.exe").write_bytes(b"MZ\x90\x00" + b"\x00" * 64)
    return _entry(tp, "evil.yaml", _commit(origin)), "unscannable executable payload"


def _committed_bytecode(tp, origin, sha):
    (origin / "plugin" / "mod.pyc").write_bytes(b"\x00" * 16)
    return _entry(tp, "evil.yaml", _commit(origin)), "unscannable executable payload"


def _log_forging_filename(tp, origin, sha):
    (origin / "plugin" / "evil\n::error::forged.js").write_text(
        "releases/latest writeFile(", encoding="utf-8")
    return _entry(tp, "evil.yaml", _commit(origin)), "self-updating code"


def _shadowed_parser_import(tp, origin, sha):
    # cwd holds a shlex.py; without `python3 -I` it shadows the stdlib (first
    # imported by the parser itself) and swallows the verdict.
    (tp / "shlex.py").write_text(
        "def quote(s):\n"
        "    if s.startswith('repo must'):\n"
        "        s = ''\n"
        "    return \"'\" + s.replace(\"'\", \"'\\\\''\") + \"'\"\n", encoding="utf-8")
    return _entry(tp, "evil.yaml", sha, repo=origin.as_uri()), "repo must be an https:// git URL"


@pytest.mark.parametrize("case", [
    _traversal_subdir, _symlinked_subdir, _symlink_behind_in_clone_link, _tag_object_sha,
    _newline_sha, _log_forging_repo, _symlinked_entry_file, _entry_outside_the_candidates,
    _self_updater_in_spaced_dir, _bundled_native_binary, _committed_bytecode,
    _log_forging_filename, _shadowed_parser_import,
], ids=lambda f: f.__name__.lstrip("_"))
def test_bypass_fails_the_gate(tmp_path, origin, case):
    sha = _git(origin, "rev-parse", "HEAD")
    entry, expected = case(tmp_path, origin, sha)
    res = _gate(tmp_path, origin, [entry])
    out = res.stdout + res.stderr
    assert res.returncode != 0, out
    assert expected in out
    assert "PASS" not in res.stdout
    # PR-controlled values must never smuggle a workflow command into the log.
    assert not any(line.startswith("::error::forged") for line in res.stdout.splitlines())


def _attested_binary(origin: Path) -> str:
    """Commit a PE binary at plugin/runtime/host.exe and return its catalog path."""
    (origin / "plugin" / "runtime").mkdir()
    (origin / "plugin" / "runtime" / "host.exe").write_bytes(b"MZ\x90\x00" + b"\x00" * 64)
    _commit(origin)
    return "runtime/host.exe"


def _gh_stub(tmp_path: Path, rc: int) -> Path:
    """A `gh` that records its argv and answers `attestation verify` with ``rc``."""
    log = tmp_path / "gh.calls"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "gh"
    stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nexit {rc}\n', encoding="utf-8")
    stub.chmod(0o755)
    return log


def test_an_attested_binary_is_verified_against_the_pin_and_then_allowed(tmp_path, origin):
    path = _attested_binary(origin)
    sha = _git(origin, "rev-parse", "HEAD")
    log = _gh_stub(tmp_path, 0)
    entry = _entry(tmp_path, "ok.yaml", sha, attested_binaries=[path])
    res = _gate(tmp_path, origin, [entry])
    assert res.returncode == 0, res.stdout + res.stderr
    assert f"PASS: {_rel(tmp_path, entry)}" in res.stdout
    [call] = log.read_text(encoding="utf-8").splitlines()
    # The gate strips only a github.com prefix from the repo, so the fixture URL passes whole.
    assert call.startswith("attestation verify ")
    assert call.endswith(f"/plugin/{path} --repo {REPO_URL} --source-digest {sha} "
                         "--predicate-type https://slsa.dev/provenance/v1")


def test_an_attested_binary_without_provenance_fails_the_gate(tmp_path, origin):
    path = _attested_binary(origin)
    _gh_stub(tmp_path, 1)
    entry = _entry(tmp_path, "evil.yaml", _git(origin, "rev-parse", "HEAD"), attested_binaries=[path])
    res = _gate(tmp_path, origin, [entry])
    assert res.returncode != 0
    assert "no build-provenance attestation for runtime/host.exe" in res.stdout
    assert "PASS" not in res.stdout


def test_an_attested_self_updater_still_fails_the_self_updater_gate(tmp_path, origin):
    # Provenance proves origin, not behaviour: listing an updater script as attested must not
    # remove it from the tree the self-updater gate reads.
    (origin / "plugin" / "update.js").write_text(
        "fetch('releases/latest'); fs.writeFileSync(target, body)\n", encoding="utf-8")
    _commit(origin)
    _gh_stub(tmp_path, 0)
    entry = _entry(tmp_path, "evil.yaml", _git(origin, "rev-parse", "HEAD"),
                   attested_binaries=["update.js"])
    res = _gate(tmp_path, origin, [entry])
    assert res.returncode != 0
    assert "self-updating code" in res.stdout
    assert "PASS" not in res.stdout


@pytest.mark.parametrize("declaration", [
    '{$id: "https://raw.githubusercontent.com/ajv-validator/ajv/master/lib/refs/data.json#"}',
    '{"$id": "https://raw.githubusercontent.com/ajv-validator/ajv/master/lib/refs/data.json#"}',
    '{$id:"https://raw.githubusercontent.com/ajv-validator/ajv/master/lib/refs/data.json#"}',
    "{$ref: 'https://raw.githubusercontent.com/ajv-validator/ajv/master/lib/refs/data.json#'}",
], ids=["spaced", "quoted-key", "minified", "ref-single-quote"])
def test_ajv_bundled_meta_schema_url_is_not_a_self_updater(tmp_path, origin, declaration):
    # ajv ships its draft-06 meta-schema with $id/$ref pointing at a raw.githubusercontent.com
    # URL, in whatever quoting/spacing the bundler emitted. The string is an inert JSON-schema
    # identifier ajv matches by value against its own in-memory schema -- it never drives a
    # request -- so a plugin that bundles ajv AND writes its own data files (both halves in one
    # bundled file) must still pass.
    (origin / "plugin" / "bundle.js").write_text(
        f"{declaration}\nfs.writeFileSync(target, body)\n", encoding="utf-8")
    sha = _commit(origin)
    entry = _entry(tmp_path, "ok.yaml", sha)
    res = _gate(tmp_path, origin, [entry])
    assert res.returncode == 0, res.stdout + res.stderr
    assert f"PASS: {entry.relative_to(tmp_path / 'cand')}" in res.stdout


@pytest.mark.parametrize("filename", [".js", "..js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx", ".mts", ".cts", "dl.js"])
def test_dot_named_self_updaters_are_scanned(tmp_path, origin, filename):
    (origin / "plugin" / filename).write_text(
        "fetch('releases/download'); fs.writeFileSync(target, body)\n", encoding="utf-8")
    entry = _entry(tmp_path, "evil.yaml", _commit(origin))
    res = _gate(tmp_path, origin, [entry])
    assert res.returncode != 0
    assert "self-updating code" in res.stdout
    assert "PASS" not in res.stdout


_AJV_ID = "https://raw.githubusercontent.com/ajv-validator/ajv/master/lib/refs/data.json#"


@pytest.mark.parametrize("source", [
    # a traversal out of lib/refs
    "fetch('https://raw.githubusercontent.com/ajv-validator/ajv/master/lib/refs/../../payload.js')",
    # a traversal out of data.json itself: only the "#" ends the exempt id
    "fetch('https://raw.githubusercontent.com/ajv-validator/ajv/master/lib/refs/data.json"
    "/../../../../../../evil/repo/main/plugin.js')",
    # the exempt id must not hide a real fetch after it on the same minified line
    f"{{$id:'{_AJV_ID}'}};fetch('https://raw.githubusercontent.com/o/r/main/plugin.js')",
    # the exempt literal reused as an executable fetch base, not a schema declaration
    f"const u = new URL('/evil/repo/main/plugin.js', '{_AJV_ID}'); fetch(u)",
    # ... or returned by an arrow function whose parameter happens to be named $id: `=>` is not a
    # declaration separator, so the id is not stripped and the real fetch still shows
    f"const f = $id => '{_AJV_ID}'; fetch(new URL('/evil/repo/main/plugin.js', f()))",
    # only ajv's own id is inert: any other raw.githubusercontent URL in a $id still counts
    "const s = {$id: 'https://raw.githubusercontent.com/o/r/main/plugin.js'}; fetch(s.$id)",
], ids=["refs_traversal", "data_json_traversal", "beside_ajv_id", "fetch_base", "arrow_fn_base",
        "other_url_declared"])
def test_a_real_fetch_near_or_disguised_as_the_ajv_id_still_fails(tmp_path, origin, source):
    # The exemption removes ONLY the inert ajv $id/$ref declaration; any real GitHub fetch that
    # remains -- a sibling/traversal path, a fetch beside the id, or the id reused as a fetch
    # base -- still trips the gate.
    (origin / "plugin" / "sneaky.js").write_text(
        f"{source}\nfs.writeFileSync(target, body)\n", encoding="utf-8")
    sha = _commit(origin)
    entry = _entry(tmp_path, "evil.yaml", sha)
    res = _gate(tmp_path, origin, [entry])
    assert res.returncode != 0
    assert "self-updating code" in res.stdout
    assert "PASS" not in res.stdout


def test_an_attested_binaries_path_that_climbs_out_fails_the_gate(tmp_path, origin):
    log = _gh_stub(tmp_path, 0)
    entry = _entry(tmp_path, "evil.yaml", _git(origin, "rev-parse", "HEAD"),
                   attested_binaries=["../../etc/passwd"])
    res = _gate(tmp_path, origin, [entry])
    assert res.returncode != 0
    assert "is not a plain relative path" in res.stdout
    assert not log.exists()


def test_one_entry_cannot_skip_or_impersonate_the_others(tmp_path, origin):
    """Plugin code that drains stdin must not consume the remaining entry list,
    and an entry whose parser dies must fail itself rather than inherit the
    previous entry's repo/sha."""
    sha = _git(origin, "rev-parse", "HEAD")
    ok, crash, ok2 = (_entry(tmp_path, n, sha) for n in ("ok.yaml", "crash.yaml", "ok2.yaml"))
    python_stub = tmp_path / "bin" / "python3"
    python_stub.parent.mkdir()
    # Unresolved interpreter path on purpose: .venv/bin/python resolves to the
    # base interpreter, which would drop the venv's site-packages under -I.
    python_stub.write_text(
        f'#!/bin/sh\ncase "$*" in */crash.yaml) exit 1;; esac\nexec "{sys.executable}" "$@"\n',
        encoding="utf-8")
    python_stub.chmod(0o755)
    res = _gate(tmp_path, origin, [ok, crash, ok2],
                hermes_stub="#!/bin/sh\ncat >/dev/null\nexit 0\n")
    assert res.returncode != 0, res.stdout + res.stderr
    assert "catalog entry parser failed" in res.stdout
    assert f"PASS: {_rel(tmp_path, ok)}" in res.stdout
    assert f"PASS: {_rel(tmp_path, ok2)}" in res.stdout
    assert f"PASS: {_rel(tmp_path, crash)}" not in res.stdout


@pytest.mark.parametrize("fields", [
    {"subdir": "plugin"},
    {"subdir": ""},
    {"subdir": None},
    {"subdir": "plugin", "alias": True},
], ids=["subdir-entry", "repo-root", "null-subdir", "in-clone-symlink"])
def test_legit_entry_passes_the_gate(tmp_path, origin, fields):
    if fields.pop("alias", False):
        (origin / "plugin" / "alias.yaml").symlink_to("plugin.yaml")
        _commit(origin)
    entry = _entry(tmp_path, "ok.yaml", _git(origin, "rev-parse", "HEAD"), **fields)
    res = _gate(tmp_path, origin, [entry])
    assert res.returncode == 0, res.stdout + res.stderr
    assert f"PASS: {_rel(tmp_path, entry)}" in res.stdout


@pytest.mark.parametrize("mut", [
    {}, {"subdir": None}, {"subdir": "../x"}, {"subdir": 5},
    {"repo": "https://x/y\n::error::x"}, {"sha": "a" * 40 + "\n"},
])
def test_structural_and_pinned_gates_agree(tmp_path, origin, mut):
    """The field rules live in both the parse step and the structural validator
    (different environments); the same entry must get the same verdict."""
    data = {"name": "parity-plugin", "description": "d", "maintainer": "o",
            "repo": REPO_URL, "sha": _git(origin, "rev-parse", "HEAD"),
            "subdir": "plugin", **mut}
    entry = _cand(tmp_path) / "plugin-catalog" / "parity.yaml"
    entry.parent.mkdir(parents=True)
    entry.write_text(yaml.safe_dump(data), encoding="utf-8")
    structural = subprocess.run(
        [sys.executable, str(ROOT / "scripts/validate_plugin_catalog.py"), str(entry)],
        capture_output=True, text=True, check=False)
    gate = _gate(tmp_path, origin, [entry])
    assert (structural.returncode == 0) == (gate.returncode == 0), (
        f"{structural.stdout}{structural.stderr}\n{gate.stdout}{gate.stderr}")


# ── changed-entries step ──────────────────────────────────────────────────────

def _write(repo: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _rename(repo):
    _git(repo, "mv", "plugin-catalog/old.yaml", "plugin-catalog/renamed.yaml")


def _typechange(repo):
    (repo / "plugin-catalog" / "old.yaml").unlink()
    (repo / "plugin-catalog" / "old.yaml").symlink_to("/etc/hostname")


NEW = {"plugin-catalog/new.yaml": "name: new\n"}
UPSTREAM_URL = "https://github.com/NousResearch/hermes-agent.git"


def _pr_merge(repo: Path) -> None:
    """Shape of GitHub's pull_request checkout: a merge commit of branch `pr`
    whose first parent is the base tip, checked out detached."""
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "pr")
    _git(repo, "checkout", "-q", "--detach")


def _runner_temp(tmp_path: Path) -> Path:
    path = tmp_path / "runner-temp"
    path.mkdir(exist_ok=True)
    return path


def _outputs(tmp_path: Path) -> dict:
    """The step's $GITHUB_OUTPUT, heredoc values included."""
    lines = (tmp_path / "gh-output.txt").read_text(encoding="utf-8").splitlines()
    out, i = {}, 0
    while i < len(lines):
        key, sep, value = lines[i].partition("=")
        if not sep and "<<" in lines[i]:
            key, delim = lines[i].split("<<", 1)
            end = lines.index(delim, i + 1)
            out[key] = lines[i + 1:end]
            i = end + 1
            continue
        out[key] = value
        i += 1
    return out


def _find_changed(tmp_path: Path, repo: Path, upstream: Path | None = None):
    """Run the changed-entries step with its workflow env. The step's upstream
    URL is rewritten to the `upstream` fixture, or to a missing path when None."""
    out = tmp_path / "gh-output.txt"
    out.write_text("", encoding="utf-8")
    env = _step_env("Find changed catalog")
    target = upstream if upstream is not None else tmp_path / "no-upstream"
    env = {
        **os.environ, **env, "GITHUB_OUTPUT": str(out),
        "RUNNER_TEMP": str(_runner_temp(tmp_path)),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.{target.as_uri()}.insteadOf",
        # Fallback so the harness also runs an older workflow copy (RED proof).
        "GIT_CONFIG_VALUE_0": env.get("UPSTREAM_REPO", UPSTREAM_URL),
    }
    res = _run(_step("Find changed catalog"), tmp_path, repo, env)
    return res, [f for f in _outputs(tmp_path).get("files", []) if f]


def _assert_candidates_are_the_tree_bytes(tmp_path: Path, repo: Path, files: list[str]) -> None:
    """Each listed entry is exported as a regular file with its exact blob, or
    (a non-regular tree entry) not at all; nothing else is exported."""
    cand = Path(_outputs(tmp_path)["candidates"])
    exported = {p.relative_to(cand).as_posix() for p in cand.rglob("*") if not p.is_dir()}
    assert not any((cand / p).is_symlink() for p in exported)
    regular = set()
    for f in files:
        mode = _git(repo, "ls-tree", "--format=%(objectmode)", "HEAD", "--", f)
        if mode in ("100644", "100755"):
            regular.add(f)
            blob = subprocess.run(["git", "cat-file", "blob", f"HEAD:{f}"], cwd=repo,
                                  check=True, capture_output=True).stdout
            assert (cand / f).read_bytes() == blob, f
    assert exported == regular


@pytest.mark.parametrize("change, rc, listed, text", [
    (_rename, 0, ["plugin-catalog/renamed.yaml"], None),
    (_typechange, 0, ["plugin-catalog/old.yaml"], None),
    (lambda r: _write(r, {**NEW, "scripts/tool.py": "x = 2\n"}), 1, [], "scripts/tool.py"),
    (lambda r: _write(r, {"plugin-catalog/old.yaml": "name: old\nsha: b\n"}), 0,
     ["plugin-catalog/old.yaml"], None),
    (lambda r: _write(r, {**NEW, "contributors/emails/a@b.c": "someone\n"}), 0,
     ["plugin-catalog/new.yaml"], None),
    (lambda r: _write(r, {"plugin-catalog/README.md": "rules v2\n",
                          "website/docs/catalog.md": "docs\n",
                          ".github/workflows/x.yml": "on: push\n"}), 0, [], None),
], ids=["rename", "typechange", "entry-plus-tooling", "sha-bump",
        "entry-plus-email-map", "readme-docs-ci-only"])
def test_changed_entries_step(tmp_path, change, rc, listed, text):
    """Renames and typechanges must reach the pinned gate (a typechanged entry
    as a missing candidate, which the gate rejects); an entry PR that also
    touches tooling fails before any clone, while email maps and entry-free
    policy PRs stay green."""
    repo = tmp_path / "pr"
    _write(repo, {"plugin-catalog/old.yaml": "name: old\n", "plugin-catalog/README.md": "rules\n"})
    _git(repo, "init", "-qb", "main")
    _commit(repo, "base")
    _git(repo, "checkout", "-qb", "pr")
    change(repo)
    _commit(repo, "pr")
    _pr_merge(repo)
    res, files = _find_changed(tmp_path, repo)
    assert res.returncode == rc, res.stdout + res.stderr
    assert files == listed
    if text:
        assert text in res.stdout
    if listed:
        _assert_candidates_are_the_tree_bytes(tmp_path, repo, listed)
        assert _outputs(tmp_path)["base"] == _git(repo, "rev-parse", "HEAD^1")


def test_a_typechanged_entry_fails_the_pinned_gate(tmp_path, origin):
    repo = tmp_path / "pr"
    _write(repo, {"plugin-catalog/old.yaml": "name: old\n"})
    _git(repo, "init", "-qb", "main")
    _commit(repo, "base")
    _git(repo, "checkout", "-qb", "pr")
    _typechange(repo)
    _commit(repo, "pr")
    _pr_merge(repo)
    res, files = _find_changed(tmp_path, repo)
    assert res.returncode == 0, res.stdout + res.stderr
    gate = _gate(tmp_path, origin, files, candidates=Path(_outputs(tmp_path)["candidates"]))
    assert gate.returncode != 0
    assert "must be a regular file" in gate.stdout
    assert "PASS" not in gate.stdout


# ── upstream sync through the changed-entries step ───────────────────────────

CORE = "".join(f"line {i}\n" for i in range(40))


def _sync_fixture(tmp_path: Path, pr_change=None) -> tuple[Path, Path]:
    """Upstream U0 -> U1 -> U2 and a fork synced to U1 whose PR branch merges U2.

    U1 re-pins old.yaml (the fork's base already has it); U2 adds up.yaml,
    re-pins old.yaml again and moves + edits a core file, so the merge mixes
    entries with core and a git rename candidate. `pr_change` runs on the PR
    branch after the upstream merge.
    """
    up = tmp_path / "upstream"
    _write(up, {"plugin-catalog/old.yaml": "name: old\n", "core/tool.py": CORE})
    _git(up, "init", "-qb", "main")
    _commit(up, "U0")
    _write(up, {"plugin-catalog/old.yaml": "name: old\nsha: 2\n"})
    _commit(up, "U1")
    fork = tmp_path / "pr"
    _git(tmp_path, "clone", "-q", str(up), str(fork))
    _write(fork, {"fork.txt": "fork-local\n"})
    _commit(fork, "fork main")
    _write(up, {"plugin-catalog/up.yaml": "name: up\nsha: 1\n",
                "plugin-catalog/old.yaml": "name: old\nsha: 3\n"})
    _git(up, "mv", "core/tool.py", "core/moved.py")
    (up / "core" / "moved.py").write_text(CORE + "edited\n", encoding="utf-8")
    _commit(up, "U2")
    _git(fork, "checkout", "-qb", "pr")
    _git(fork, "fetch", "-q", str(up), "main")
    _git(fork, "merge", "-q", "--no-ff", "-m", "sync", "FETCH_HEAD")
    if pr_change:
        pr_change(fork)
        _commit(fork, "pr change")
    _pr_merge(fork)
    _git(fork, "remote", "remove", "origin")
    return up, fork


def _own_entry(repo):
    _write(repo, {"plugin-catalog/evil.yaml": "name: evil\nsha: 6\n"})


def _edited_upstream_entry(repo):
    _write(repo, {"plugin-catalog/up.yaml": "name: up\nsha: evil\n"})


def _stale_upstream_version(repo):
    # Exactly U0's old.yaml: admitted upstream once, but not what U2 pins now.
    _write(repo, {"plugin-catalog/old.yaml": "name: old\n"})


def _mode_swapped_upstream_entry(repo):
    # Same blob id as upstream's up.yaml, but a symlink (mode 120000).
    (repo / "plugin-catalog" / "up.yaml").unlink()
    (repo / "plugin-catalog" / "up.yaml").symlink_to("name: up\nsha: 1\n")


SYNCED = ["plugin-catalog/old.yaml", "plugin-catalog/up.yaml"]


def test_upstream_sync_of_entries_with_core_passes(tmp_path):
    """Upstream's admitted entries may land beside upstream's core, and still
    reach the pinned-source gate as exactly upstream's bytes."""
    up, fork = _sync_fixture(tmp_path)
    res, files = _find_changed(tmp_path, fork, upstream=up)
    assert res.returncode == 0, res.stdout + res.stderr
    assert files == SYNCED
    assert "Upstream sync: every changed entry is upstream" in res.stdout
    _assert_candidates_are_the_tree_bytes(tmp_path, fork, files)


def _rejected(res, files, *, entries=()):
    out = res.stdout
    assert res.returncode == 1, out + res.stderr
    assert files == []
    assert "may only touch plugin-catalog/**" in out
    listed = out.split("Entries that are not upstream's change", 1)[1]
    for e in entries:
        assert e in listed, out


@pytest.mark.parametrize("change, culprit", [
    (_own_entry, "plugin-catalog/evil.yaml"),
    (_edited_upstream_entry, "plugin-catalog/up.yaml"),
    (_stale_upstream_version, "plugin-catalog/old.yaml"),
    (_mode_swapped_upstream_entry, "plugin-catalog/up.yaml"),
], ids=lambda c: c if isinstance(c, str) else c.__name__.lstrip("_"))
def test_hostile_mixed_admission_inside_a_sync_fails(tmp_path, change, culprit):
    """Riding an upstream merge must not admit an entry upstream never had at
    that commit: any one such entry restores the data-only rule for the PR."""
    up, fork = _sync_fixture(tmp_path, change)
    res, files = _find_changed(tmp_path, fork, upstream=up)
    _rejected(res, files, entries=[culprit])


def test_unreachable_upstream_fails_closed(tmp_path):
    _, fork = _sync_fixture(tmp_path)
    res, files = _find_changed(tmp_path, fork, upstream=None)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "could not resolve upstream main" in res.stdout
    assert files == []


def _entry_rename_with_edit(tmp_path: Path) -> tuple[None, Path]:
    repo = tmp_path / "pr"
    _write(repo, {"plugin-catalog/old.yaml": "name: old\n" + "# pad\n" * 20})
    _git(repo, "init", "-qb", "main")
    _commit(repo, "base")
    _git(repo, "checkout", "-qb", "pr")
    _git(repo, "mv", "plugin-catalog/old.yaml", "plugin-catalog/renamed.yaml")
    (repo / "plugin-catalog" / "renamed.yaml").write_text(
        "name: old\nsha: 2\n" + "# pad\n" * 20, encoding="utf-8")
    _commit(repo, "pr")
    _pr_merge(repo)
    return None, repo


@pytest.mark.parametrize("scenario, listed", [
    (_entry_rename_with_edit, ["plugin-catalog/renamed.yaml"]),
    (_sync_fixture, SYNCED),
], ids=["entry-rename-with-edit", "upstream-sync"])
def test_blobless_checkout_of_a_private_fork_needs_no_credentials(tmp_path, scenario, listed):
    """CI checks out blobless and then drops the token, so a lazy blob fetch
    from the private fork hits a credential prompt. The step must answer from
    trees and HEAD's checked-out blobs alone: here the promisor remote is
    unreachable, as the token-less https remote is in CI."""
    up, src = scenario(tmp_path)
    _git(src, "config", "uploadpack.allowFilter", "true")
    head = _git(src, "rev-parse", "HEAD")
    clone = tmp_path / "blobless"
    _git(tmp_path, "clone", "-q", "--filter=blob:none", "--no-checkout", src.as_uri(), str(clone))
    _git(clone, "checkout", "-q", "--detach", head)
    _git(clone, "remote", "set-url", "origin", "https://fixture.invalid/private-fork.git")
    # HEAD^1's blobs really are missing: reading one would need the remote.
    missing = _git(clone, "rev-list", "--objects", "--missing=print", "HEAD^1")
    assert any(line.startswith("?") for line in missing.splitlines())
    res, files = _find_changed(tmp_path, clone, upstream=up)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "promisor remote" not in res.stderr
    assert files == listed
    _assert_candidates_are_the_tree_bytes(tmp_path, clone, files)


# ── upstream delta admission: entries strict, the rest never executed ─────────
# Regressions for the PR #434 review: matching entry blobs alone could not tell
# an upstream sync from a PR that restores a fork-deleted entry or overwrites a
# fork-local pin. The rest of a sync (core, tooling, conflict resolutions) is
# admitted: the gate runs none of it (see the trusted-base pipeline below).

U0_FILES = {
    "plugin-catalog/a.yaml": "name: a\nsha: 1\n",
    "plugin-catalog/b.yaml": "name: b\nsha: 1\n",
    "core/tool.py": CORE,
    "scripts/scan_plugin_payloads.py": "scan v0\n",
    ".github/actions/setup-pm/action.yml": "setup v0\n",
}


def _delta_fixture(tmp_path: Path, *, fork=None, upstream=None, pr=None,
                   merge_opts: tuple = (), files: dict | None = None) -> tuple[Path, Path]:
    """Upstream U0 (`files`), a fork synced to U0 whose main applies `fork`,
    upstream U1 applying `upstream` (none: upstream never advances), and a PR
    branch that merges upstream main with `merge_opts` and then applies `pr`."""
    up = tmp_path / "upstream"
    _write(up, files or U0_FILES)
    _git(up, "init", "-qb", "main")
    _commit(up, "U0")
    repo = tmp_path / "pr"
    _git(tmp_path, "clone", "-q", str(up), str(repo))
    _write(repo, {"fork.txt": "fork-local\n"})
    if fork:
        fork(repo)
    _commit(repo, "fork main")
    if upstream:
        upstream(up)
        _commit(up, "U1")
    _git(repo, "checkout", "-qb", "pr")
    _git(repo, "fetch", "-q", str(up), "main")
    _git(repo, "merge", "-q", "--no-ff", *merge_opts, "-m", "sync", "FETCH_HEAD")
    if pr:
        pr(repo)
        _commit(repo, "pr change")
    _pr_merge(repo)
    _git(repo, "remote", "remove", "origin")
    return up, repo


def _up_repins_a(r):
    _write(r, {"plugin-catalog/a.yaml": "name: a\nsha: 2\n"})


def _up_repins_a_and_tooling(r):
    _write(r, {"plugin-catalog/a.yaml": "name: a\nsha: 2\n",
               "scripts/scan_plugin_payloads.py": "scan v1\n",
               ".github/actions/setup-pm/action.yml": "setup v1\n",
               "core/tool.py": CORE + "upstream\n", "core/new.py": "new\n"})


def test_upstream_delta_including_gate_tooling_is_admitted(tmp_path):
    """Upstream's own scanner/action/core changes may land verbatim with its entries."""
    up, repo = _delta_fixture(tmp_path, upstream=_up_repins_a_and_tooling)
    res, files = _find_changed(tmp_path, repo, upstream=up)
    assert res.returncode == 0, res.stdout + res.stderr
    assert files == ["plugin-catalog/a.yaml"]
    assert "Upstream sync: every changed entry is upstream" in res.stdout


def _fork_deletes_a(r):
    (r / "plugin-catalog" / "a.yaml").unlink()


def _restore_a(r):
    _write(r, {"plugin-catalog/a.yaml": U0_FILES["plugin-catalog/a.yaml"]})


def test_restoring_a_fork_deleted_entry_without_an_upstream_advance_fails(tmp_path):
    up, repo = _delta_fixture(
        tmp_path, fork=_fork_deletes_a,
        pr=lambda r: (_restore_a(r), _write(r, {"core/tool.py": CORE + "evil\n"})))
    res, files = _find_changed(tmp_path, repo, upstream=up)
    _rejected(res, files, entries=["plugin-catalog/a.yaml"])
    assert "does not advance the base's upstream merge-base" in res.stdout


def test_restoring_a_fork_deleted_entry_inside_a_real_sync_fails(tmp_path):
    up, repo = _delta_fixture(
        tmp_path, fork=_fork_deletes_a,
        upstream=lambda r: _write(r, {"core/tool.py": CORE + "upstream\n"}), pr=_restore_a)
    res, files = _find_changed(tmp_path, repo, upstream=up)
    _rejected(res, files, entries=["plugin-catalog/a.yaml"])


def test_a_sync_cannot_overwrite_a_fork_local_pin_with_upstreams(tmp_path):
    """The fork re-pinned a.yaml; upstream re-pins it too. Taking upstream's
    version verbatim would silently drop the fork's decision."""
    up, repo = _delta_fixture(
        tmp_path, fork=lambda r: _write(r, {"plugin-catalog/a.yaml": "name: a\nsha: fork\n"}),
        upstream=lambda r: (_up_repins_a(r), _write(r, {"core/tool.py": CORE + "upstream\n"})),
        merge_opts=("-X", "theirs"))
    res, files = _find_changed(tmp_path, repo, upstream=up)
    _rejected(res, files, entries=["plugin-catalog/a.yaml"])


def test_a_sync_cannot_revert_a_fork_local_pin_upstream_never_changed(tmp_path):
    up, repo = _delta_fixture(
        tmp_path, fork=lambda r: _write(r, {"plugin-catalog/a.yaml": "name: a\nsha: fork\n"}),
        upstream=lambda r: _write(r, {"plugin-catalog/b.yaml": "name: b\nsha: 2\n",
                                      "core/tool.py": CORE + "upstream\n"}),
        pr=_restore_a)
    res, files = _find_changed(tmp_path, repo, upstream=up)
    _rejected(res, files, entries=["plugin-catalog/a.yaml"])
    assert "plugin-catalog/b.yaml" not in res.stdout.split("Entries that", 1)[1]


def _fork_edits_core_head(r):
    _write(r, {"core/tool.py": "fork line\n" + CORE[CORE.index("\n") + 1:]})


@pytest.mark.parametrize("merge_opts", [(), ("-X", "theirs")], ids=["three-way-merge", "upstream-verbatim"])
def test_a_sync_resolving_a_fork_modified_core_path_is_admitted(tmp_path, merge_opts):
    """Real syncs resolve core conflicts the fork's own way. That resolution is
    never executed by the gate, so it does not hold back upstream's entries."""
    up, repo = _delta_fixture(
        tmp_path, fork=_fork_edits_core_head,
        upstream=lambda r: (_up_repins_a(r), _write(r, {"core/tool.py": CORE + "upstream\n"})),
        merge_opts=merge_opts)
    res, files = _find_changed(tmp_path, repo, upstream=up)
    assert res.returncode == 0, res.stdout + res.stderr
    assert files == ["plugin-catalog/a.yaml"]


# ── the gate runs only the trusted base's tools ───────────────────────────────

def _tool_files() -> dict[str, str]:
    """The base's gate tooling: the real scanner and validator, the recording
    sandbox stand-in, and placeholders for an action and core."""
    return {
        "scripts/scan_plugin_payloads.py":
            (ROOT / "scripts" / "scan_plugin_payloads.py").read_text(encoding="utf-8"),
        "scripts/validate_plugin_catalog.py":
            (ROOT / "scripts" / "validate_plugin_catalog.py").read_text(encoding="utf-8"),
        "scripts/ci/plugin_validation_sandbox.py": SANDBOX_STUB,
        ".github/actions/setup-pm/action.yml": "setup v0\n",
        ".github/actions/retry/action.yml": "retry v0\n",
        "core/tool.py": CORE,
    }


def _pinned(sha: str, name: str = "a") -> str:
    return yaml.safe_dump({"name": name, "description": "d", "maintainer": "o",
                           "repo": REPO_URL, "sha": sha, "subdir": "plugin"})


def _marker_script(marker: Path) -> str:
    """Python an attacker would put in a tool: leaves proof it ran, then
    reports success."""
    return f"open({str(marker)!r}, 'w').write('ran')\nimport sys\nsys.exit(0)\n"


def _pr_checkout(tmp_path: Path, src: Path) -> Path:
    """What the PR job's first actions/checkout leaves: the merge commit."""
    ws = tmp_path / "ws"
    _git(tmp_path, "clone", "-q", "--no-checkout", str(src), str(ws))
    _git(ws, "checkout", "-q", "--detach", _git(src, "rev-parse", "HEAD"))
    return ws


def _switch_to_trusted_base(tmp_path: Path, src: Path, ws: Path, job: str, base: str,
                            *, confirmed: bool = True) -> subprocess.CompletedProcess:
    """Run the job's drop step, emulate `Check out the trusted base` (an
    actions/checkout of the step's ref into the emptied workspace), then run
    the job's confirm step with its workflow env."""
    env = {**os.environ, "GITHUB_WORKSPACE": str(ws)}
    drop = _run(_step("Drop the PR checkout", job), tmp_path, ws, env)
    assert drop.returncode == 0, drop.stdout + drop.stderr
    assert ws.is_dir() and not any(ws.iterdir())
    checkout = _find_step("Check out the trusted base", job)
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["ref"] == _step_env("Confirm the trusted checkout", job)["TRUSTED_BASE"]
    _git(tmp_path, "clone", "-q", "--no-checkout", str(src), str(ws))
    _git(ws, "checkout", "-q", "--detach", base)
    confirm = _run(_step("Confirm the trusted checkout", job), tmp_path, ws,
                   {**env, **_step_env("Confirm the trusted checkout", job), "TRUSTED_BASE": base})
    if confirmed:
        assert confirm.returncode == 0, confirm.stdout + confirm.stderr
    return confirm


def _pinned_pipeline(tmp_path, origin, src, up, *, trusted: bool = True):
    """Changed-entries step on the PR checkout, the switch to the trusted base,
    then the pinned-source step from whatever the workspace then holds."""
    ws = _pr_checkout(tmp_path, src)
    res, files = _find_changed(tmp_path, ws, upstream=up)
    assert res.returncode == 0, res.stdout + res.stderr
    out = _outputs(tmp_path)
    assert "candidates" in out, (res.stdout, res.stderr, out)
    if trusted:
        _switch_to_trusted_base(tmp_path, src, ws, PINNED_JOB, out["base"])
    gate = _gate(tmp_path, origin, files, workspace=ws, candidates=Path(out["candidates"]))
    return files, out, ws, gate


INJECTIONS = {
    "scanner": "scripts/scan_plugin_payloads.py",
    "sandbox-wrapper": "scripts/ci/plugin_validation_sandbox.py",
    "validator": "scripts/validate_plugin_catalog.py",
    "setup-pm-action": ".github/actions/setup-pm/action.yml",
    "core-edit": "core/tool.py",
    "new-core-file": "hermes_cli/new_module.py",
}


def _injecting_sync(tmp_path, origin, pin: str, injected: dict[str, str]):
    # The base pins the plugin's first commit; upstream re-pins it to `pin`.
    sha0 = _git(origin, "rev-list", "--max-parents=0", "HEAD")
    files = {**_tool_files(), "plugin-catalog/a.yaml": _pinned(sha0),
             "plugin-catalog/b.yaml": _pinned(sha0, "b")}
    return _delta_fixture(
        tmp_path, files=files,
        upstream=lambda r: _write(r, {"plugin-catalog/a.yaml": _pinned(pin),
                                      "core/tool.py": CORE + "upstream\n"}),
        pr=lambda r: _write(r, injected))


@pytest.mark.parametrize("path", INJECTIONS.values(), ids=INJECTIONS.keys())
def test_a_syncs_own_tool_or_core_change_never_runs(tmp_path, origin, path):
    """A sync may carry any core or tooling bytes of its own (the admission
    allows it), but none of them reaches the gate's execution path: the
    workspace the tools run from is exactly the base, the injected code leaves
    no trace, and the base's sandbox stand-in is the one that ran."""
    marker = tmp_path / "INJECTED-CODE-RAN"
    # A new commit of the same plugin tree for upstream to re-pin.
    _git(origin, "commit", "-q", "--allow-empty", "-m", "re-pin")
    pin = _git(origin, "rev-parse", "HEAD")
    up, src = _injecting_sync(tmp_path, origin, pin, {path: _marker_script(marker)})
    files, out, ws, gate = _pinned_pipeline(tmp_path, origin, src, up)
    assert files == ["plugin-catalog/a.yaml"]
    assert gate.returncode == 0, gate.stdout + gate.stderr
    assert "PASS: plugin-catalog/a.yaml" in gate.stdout
    assert not marker.exists()
    base = out["base"]
    assert _git(ws, "rev-parse", "HEAD") == base
    on_base = subprocess.run(["git", "cat-file", "blob", f"{base}:{path}"], cwd=ws,
                             capture_output=True, check=False)
    if on_base.returncode == 0:
        assert (ws / path).read_bytes() == on_base.stdout
    else:
        assert not (ws / path).exists()
    (call,) = _calls(tmp_path)
    assert call[call.index("--") + 1:][:3] == ["hermes", "plugins", "validate"]


def test_a_syncs_own_scanner_cannot_wave_a_payload_through(tmp_path, origin):
    """Upstream's re-pin carries committed bytecode; the PR's scanner would
    pass anything. The base's scanner runs and rejects the entry."""
    marker = tmp_path / "INJECTED-CODE-RAN"
    (origin / "plugin" / "mod.pyc").write_bytes(b"\x00" * 16)
    pin = _commit(origin, "bytecode")
    up, src = _injecting_sync(tmp_path, origin, pin,
                              {"scripts/scan_plugin_payloads.py": _marker_script(marker)})
    files, _, _, gate = _pinned_pipeline(tmp_path, origin, src, up)
    assert files == ["plugin-catalog/a.yaml"]
    assert gate.returncode != 0
    assert "unscannable executable payload" in gate.stdout
    assert "PASS" not in gate.stdout
    assert not marker.exists()


def test_control_without_the_trusted_checkout_the_injected_scanner_runs(tmp_path, origin):
    """Control for the two cases above: run the same pipeline from the PR's
    checkout and the injected scanner runs and passes the bytecode, so the
    marker's absence there means the trusted checkout kept it out."""
    marker = tmp_path / "INJECTED-CODE-RAN"
    (origin / "plugin" / "mod.pyc").write_bytes(b"\x00" * 16)
    pin = _commit(origin, "bytecode")
    up, src = _injecting_sync(tmp_path, origin, pin,
                              {"scripts/scan_plugin_payloads.py": _marker_script(marker)})
    _, _, _, gate = _pinned_pipeline(tmp_path, origin, src, up, trusted=False)
    assert marker.exists()
    assert gate.returncode == 0, gate.stdout + gate.stderr


def test_executed_actions_resolve_only_after_the_trusted_checkout():
    """`uses: ./...` actions (setup-pm, retry) load from the workspace when the
    step runs and cannot be executed by this harness; in both jobs every one of
    them must come after the switch to the trusted base."""
    for job in ("structural", PINNED_JOB):
        steps = list(_job_steps(job))
        names = [s.get("name", "") for s in steps]
        switch = names.index("Check out the trusted base")
        assert names[switch - 1] == "Drop the PR checkout"
        assert names[switch + 1] == "Confirm the trusted checkout"
        local = [i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("./")]
        assert local, job
        assert all(i > switch for i in local), (job, local, switch)


def test_the_bootstrap_check_lists_every_workspace_tool_the_job_runs():
    """`Confirm the trusted checkout` refuses a base missing any listed tool;
    every `./` action and every workspace script a later step runs is listed."""
    import re
    for job in ("structural", PINNED_JOB):
        steps = list(_job_steps(job))
        names = [s.get("name", "") for s in steps]
        listed = set(_step_env("Confirm the trusted checkout", job)["TRUSTED_TOOLS"].split())
        used = set()
        for s in steps[names.index("Confirm the trusted checkout") + 1:]:
            if str(s.get("uses", "")).startswith("./"):
                used.add(s["uses"][2:] + "/action.yml")
            used.update(re.findall(r"(scripts/[\w./-]+\.py)", s.get("run", "")))
        assert used and used == listed, (job, used, listed)
        assert all((ROOT / t).is_file() for t in listed), job


def _base_without(path: str) -> dict[str, str]:
    """The base's tooling from before `path` was added."""
    files = _tool_files()
    files.pop(path)
    return files


def test_an_entry_on_a_base_without_the_sandbox_fails_closed(tmp_path, origin):
    """An admitted entry whose base predates the sandbox wrapper: the confirm
    step refuses the base rather than run anything, and the PR's own wrapper
    (carried in by the sync) is never a fallback."""
    marker = tmp_path / "INJECTED-CODE-RAN"
    sandbox = "scripts/ci/plugin_validation_sandbox.py"
    sha0 = _git(origin, "rev-list", "--max-parents=0", "HEAD")
    _git(origin, "commit", "-q", "--allow-empty", "-m", "re-pin")
    pin = _git(origin, "rev-parse", "HEAD")
    files = {**_base_without(sandbox), "plugin-catalog/a.yaml": _pinned(sha0)}
    up, src = _delta_fixture(
        tmp_path, files=files,
        upstream=lambda r: _write(r, {"plugin-catalog/a.yaml": _pinned(pin)}),
        pr=lambda r: _write(r, {sandbox: _marker_script(marker)}))
    ws = _pr_checkout(tmp_path, src)
    res, listed = _find_changed(tmp_path, ws, upstream=up)
    assert res.returncode == 0, res.stdout + res.stderr
    assert listed == ["plugin-catalog/a.yaml"]
    out = _outputs(tmp_path)
    confirm = _switch_to_trusted_base(tmp_path, src, ws, PINNED_JOB, out["base"], confirmed=False)
    assert confirm.returncode != 0
    assert f"predates this gate (missing: {sandbox})" in confirm.stdout
    assert not (ws / sandbox).exists()
    assert not marker.exists()


def test_the_pr_that_adds_the_sandbox_bootstraps(tmp_path):
    """The PR introducing the wrapper (with this workflow and its tests) lands on
    a base without it: it changes no entry, so the pinned job stops after
    `Find changed catalog entries` (every later step is gated on its files) and
    the structural job, whose tools the base has, passes on the base's tools."""
    sandbox = "scripts/ci/plugin_validation_sandbox.py"
    src = tmp_path / "src"
    _write(src, _base_without(sandbox) | {"plugin-catalog/a.yaml": _pinned("a" * 40)})
    _git(src, "init", "-qb", "main")
    _commit(src, "base")
    _git(src, "checkout", "-qb", "pr")
    _write(src, {sandbox: SANDBOX_STUB,
                 ".github/workflows/plugin-catalog-ci.yml": WORKFLOW.read_text(encoding="utf-8"),
                 "tests/ci/test_plugin_validation_sandbox.py": "def test(): pass\n"})
    _commit(src, "pr")
    _pr_merge(src)
    res, files = _find_changed(tmp_path, src)
    assert res.returncode == 0, res.stdout + res.stderr
    assert files == []
    assert "candidates" not in _outputs(tmp_path)
    steps = list(_job_steps(PINNED_JOB))
    after = steps[[s.get("name") for s in steps].index("Find changed catalog entries") + 1:]
    assert after and all(s.get("if") == "steps.changed.outputs.files != ''" for s in after)
    structural, ws = _structural(tmp_path, src)
    assert structural.returncode == 0, structural.stdout + structural.stderr
    assert not (ws / sandbox).exists()


# ── structural job: the base's validator judges the PR's catalog ─────────────

def _structural(tmp_path: Path, src: Path, *, trusted: bool = True):
    ws = _pr_checkout(tmp_path, src)
    out_file = tmp_path / "gh-output.txt"
    out_file.write_text("", encoding="utf-8")
    env = {**os.environ, "GITHUB_OUTPUT": str(out_file),
           "RUNNER_TEMP": str(_runner_temp(tmp_path)), "GITHUB_WORKSPACE": str(ws)}
    export = _run(_step("Export the PR's catalog data", "structural"), tmp_path, ws, env)
    if export.returncode != 0:
        return export, ws
    out = _outputs(tmp_path)
    if trusted:
        _switch_to_trusted_base(tmp_path, src, ws, "structural", out["base"])
    validate = _run(_step("Validate catalog files (structural)", "structural"), tmp_path, ws,
                    {**env, "CATALOG_DATA": out["dir"],
                     "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}"})
    return validate, ws


def _structural_fixture(tmp_path: Path, pr_files: dict[str, str], pr=None) -> Path:
    src = tmp_path / "src"
    _write(src, {**_tool_files(), "plugin-catalog/a.yaml": _pinned("a" * 40)})
    _git(src, "init", "-qb", "main")
    _commit(src, "base")
    _git(src, "checkout", "-qb", "pr")
    _write(src, pr_files)
    if pr:
        pr(src)
    _commit(src, "pr")
    _pr_merge(src)
    return src


@pytest.mark.parametrize("entry, ok", [
    (_pinned("b" * 40, "b"), True),
    ("name: NOT VALID\n", False),
], ids=["valid-entry", "invalid-entry"])
def test_a_pr_cannot_replace_the_validator_that_judges_it(tmp_path, entry, ok):
    marker = tmp_path / "INJECTED-CODE-RAN"
    src = _structural_fixture(tmp_path, {
        "plugin-catalog/b.yaml": entry,
        "scripts/validate_plugin_catalog.py": _marker_script(marker)})
    res, _ = _structural(tmp_path, src)
    assert (res.returncode == 0) == ok, res.stdout + res.stderr
    if not ok:
        assert "b.yaml" in res.stdout + res.stderr
    assert not marker.exists()


def test_control_the_structural_harness_detects_the_injected_validator(tmp_path):
    marker = tmp_path / "INJECTED-CODE-RAN"
    src = _structural_fixture(tmp_path, {
        "plugin-catalog/b.yaml": "name: NOT VALID\n",
        "scripts/validate_plugin_catalog.py": _marker_script(marker)})
    res, _ = _structural(tmp_path, src, trusted=False)
    assert res.returncode == 0
    assert marker.exists()


def test_a_symlink_in_the_catalog_fails_the_structural_export(tmp_path):
    def link(r):
        (r / "plugin-catalog" / "x.yaml").symlink_to("/etc/hostname")
    src = _structural_fixture(tmp_path, {}, pr=link)
    res, _ = _structural(tmp_path, src)
    assert res.returncode != 0
    assert "may only hold regular files" in res.stdout


# ── real fork history: the syncs the gate must keep admitting ─────────────────

REAL_SYNCS = {
    # Merge of upstream c88ccd08c8 into the fork: three upstream re-pins beside
    # 276 core paths, some resolved the fork's own way.
    "2aabfa57b9659d5041f9074199cf90c90de1f12f": [
        "plugin-catalog/birkin-mnemosyne.yaml", "plugin-catalog/kiwi.yaml",
        "plugin-catalog/llmslim.yaml"],
    # Landing of a tested sync branch: core only, no entry change.
    "3234d338163f98492ff72d21d247bbf8c65c1293": [],
}


def _object_store() -> Path | None:
    try:
        common = _git(ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir")
    except subprocess.CalledProcessError:
        return None
    return Path(common) / "objects"


def _missing_history(sha: str) -> str | None:
    store = _object_store()
    if store is None:
        return "not a git checkout"
    r = subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=ROOT,
                       capture_output=True, check=False)
    return None if r.returncode == 0 else f"{sha[:10]} is not in this checkout's history"


def _borrowing_repo(path: Path, refs: dict[str, str]) -> Path:
    """An empty repo that reads this checkout's objects (no copy) and has
    only the given refs."""
    _git(path.parent, "init", "-q", str(path))
    (path / ".git" / "objects" / "info" / "alternates").write_text(f"{_object_store()}\n")
    for ref, sha in refs.items():
        if ref == "HEAD":
            _git(path, "update-ref", "--no-deref", "HEAD", sha)
        else:
            _git(path, "update-ref", ref, sha)
    return path


@pytest.mark.parametrize("sync", REAL_SYNCS, ids=lambda s: s[:10])
def test_real_fork_syncs_are_admitted(tmp_path, sync):
    """Each real sync merge, checked out as the PR merge commit (first parent
    the fork's main, second the upstream it merged), against an upstream whose
    main is that upstream commit: admitted, with exactly upstream's entries
    exported as the only PR bytes."""
    if why := _missing_history(sync):
        pytest.skip(why)
    parents = _git(ROOT, "rev-list", "--parents", "-n", "1", sync).split()[1:]
    pr = _borrowing_repo(tmp_path / "pr", {"refs/heads/main": parents[0], "HEAD": sync})
    up = _borrowing_repo(tmp_path / "upstream", {"refs/heads/main": parents[1]})
    res, files = _find_changed(tmp_path, pr, upstream=up)
    assert res.returncode == 0, res.stdout + res.stderr
    assert files == REAL_SYNCS[sync]
    if files:
        assert "Upstream sync: every changed entry is upstream" in res.stdout
        _assert_candidates_are_the_tree_bytes(tmp_path, pr, files)


# ── plugin code runs only in the sandbox ──────────────────────────────────────

SENTINEL = "ghs_CATALOG_GATE_TOKEN_SENTINEL"
LEAK_PROBE = f"""#!/bin/sh
# Stand-in for the plugin code `hermes plugins validate --install-deps` runs.
plugin="$4"
if env | grep -q {SENTINEL}; then echo "LEAK in env"; exit 1; fi
for f in /proc/[0-9]*/environ; do
  if grep -q {SENTINEL} "$f" 2>/dev/null; then echo "LEAK via $f"; exit 1; fi
done
if touch "$plugin/written-by-plugin" 2>/dev/null; then echo "CLONE WRITABLE"; exit 1; fi
touch "$HERMES_HOME/poisoned-by-plugin" 2>/dev/null || true
echo "probe clean"
"""
PM_ENV = ["PATH", "HERMES_HOME", "HERMES_RUNTIME_DIR", "HERMES_PYTHON", "VIRTUAL_ENV",
          "PYTHONPATH", "PYTHONUTF8"]


def _calls(tmp_path: Path) -> list[list[str]]:
    log = tmp_path / "sandbox-calls.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_validate_runs_only_inside_the_sandbox(tmp_path, origin):
    """The one command that runs plugin code goes through the wrapper, with the
    clone read-only, the PM home as a discarded overlay and only PM variables."""
    entry = _entry(tmp_path, "ok.yaml", _git(origin, "rev-parse", "HEAD"))
    res = _gate(tmp_path, origin, [entry], extra_env={"GH_TOKEN": SENTINEL})
    assert res.returncode == 0, res.stdout + res.stderr
    (call,) = _calls(tmp_path)
    opts, command = call[:call.index("--")], call[call.index("--") + 1:]
    assert command[:4] == ["hermes", "plugins", "validate", "--install-deps"]
    plugin_dir = Path(command[4])
    pairs = list(zip(opts[::2], opts[1::2]))
    ro = [Path(v) for k, v in pairs if k == "--ro"]
    assert any(plugin_dir.is_relative_to(p) and p.is_relative_to(tmp_path / "mktmp") for p in ro), call
    assert [v for k, v in pairs if k == "--overlay"] == [str(tmp_path / "pmhome")]
    assert sorted(v for k, v in pairs if k == "--env") == sorted(PM_ENV)
    assert not any(k == "--setenv" for k, _ in pairs)
    assert SENTINEL not in " ".join(call)


def test_a_sandbox_that_cannot_be_built_fails_the_entry(tmp_path, origin):
    entry = _entry(tmp_path, "ok.yaml", _git(origin, "rev-parse", "HEAD"))
    res = _gate(tmp_path, origin, [entry], extra_env={"SANDBOX_STUB_EXIT": "125"})
    assert res.returncode != 0
    assert "hermes plugins validate failed" in res.stdout
    assert "PASS" not in res.stdout


def test_leak_probe_control_detects_the_token_without_isolation(tmp_path, origin):
    """Control for the isolation case below: run unconfined (the stub wrapper),
    the probe sees the step's GH_TOKEN, so its silence under bwrap means something."""
    entry = _entry(tmp_path, "ok.yaml", _git(origin, "rev-parse", "HEAD"))
    res = _gate(tmp_path, origin, [entry], hermes_stub=LEAK_PROBE, extra_env={"GH_TOKEN": SENTINEL})
    assert res.returncode != 0
    assert "LEAK" in res.stdout


def _bwrap_isolation_unavailable() -> str | None:
    """Judged by bwrap itself with the wrapper's namespace flags, never by the
    wrapper: a broken wrapper must fail the case below, not skip it."""
    import shutil
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return "bubblewrap is not installed"
    argv = [bwrap, "--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent",
            "--ro-bind", "/usr", "/usr", "--proc", "/proc", "--dev", "/dev"]
    for top in ("bin", "lib", "lib64"):
        host = Path("/") / top
        if host.is_symlink():
            argv += ["--symlink", os.readlink(host), f"/{top}"]
        elif host.is_dir():
            argv += ["--ro-bind", str(host), f"/{top}"]
    r = subprocess.run([*argv, "true"], capture_output=True, text=True, timeout=60, check=False)
    if r.returncode != 0:
        return f"this kernel refuses unprivileged user/net namespaces: {r.stderr.strip()[-200:]}"
    return None


@pytest.mark.skipif((_why := _bwrap_isolation_unavailable()) is not None, reason=str(_why))
def test_plugin_code_in_the_real_step_sees_no_token_and_cannot_write(tmp_path, origin):
    """The real step and the real wrapper: the plugin stand-in finds the token
    neither in its environment nor in any visible process, cannot write the
    pinned clone, and its PM-home write is discarded; the entry then passes."""
    entry = _entry(tmp_path, "ok.yaml", _git(origin, "rev-parse", "HEAD"))
    res = _gate(tmp_path, origin, [entry], hermes_stub=LEAK_PROBE, sandbox="real",
                extra_env={"GH_TOKEN": SENTINEL})
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert "probe clean" in out
    assert f"PASS: {_rel(tmp_path, entry)}" in res.stdout
    assert not (tmp_path / "pmhome" / "poisoned-by-plugin").exists()
