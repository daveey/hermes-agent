"""The catalog admission gate must validate the pinned clone's own files only.

These tests execute the real ``run:`` blocks of plugin-catalog-ci.yml against
fixture git repos: the changed-entries step against a PR merge commit, and the
pinned-source step against an origin repo served under an https URL through a
git ``url.insteadOf`` rewrite. One case per closed bypass, plus the legitimate
flows that must keep passing.
"""

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


def _step(name_prefix: str) -> str:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for job in data["jobs"].values():
        for step in job["steps"]:
            if step.get("name", "").startswith(name_prefix):
                # The workflow expands ${{ github.base_ref }} before bash sees it.
                return step["run"].replace("${{ github.base_ref }}", "main")
    raise LookupError(name_prefix)


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
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False)


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


def _entry(tmp_path: Path, name: str, sha: str, **fields) -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(
        {"repo": REPO_URL, "sha": sha, "subdir": "plugin", **fields}), encoding="utf-8")
    return path


def _planted(tmp_path: Path) -> Path:
    planted = tmp_path / "planted"
    planted.mkdir(exist_ok=True)
    (planted / "plugin.yaml").write_text("name: planted\n", encoding="utf-8")
    return planted


def _gate(tmp_path: Path, origin: Path, entries: list[Path],
          hermes_stub: str = "#!/bin/sh\nexit 0\n") -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "hermes"
    stub.write_text(hermes_stub, encoding="utf-8")
    stub.chmod(0o755)
    env = {
        **os.environ,
        "CHANGED_FILES": "\n".join(str(e) for e in entries),
        "TMPDIR": str(tmp_path / "mktmp"),
        # The test interpreter first: the step's `python3 -I` parser needs
        # ruamel.yaml from site-packages, as setup-pm provides it in CI.
        "PATH": f"{bin_dir}:{Path(sys.executable).parent}:{os.environ['PATH']}",
        # CI runs the step from the hermes-agent checkout; `-I` must ignore this.
        "PYTHONPATH": str(ROOT),
        "GIT_TERMINAL_PROMPT": "0",
        "GITHUB_WORKSPACE": str(ROOT),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.{origin.as_uri()}.insteadOf",
        "GIT_CONFIG_VALUE_0": REPO_URL,
    }
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
    link = tp / "link.yaml"
    link.symlink_to(_entry(tp, "real.yaml", sha))
    return link, "must be a regular file"


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
    # cwd is the PR checkout; without `python3 -I` this shlex.py shadows the
    # stdlib (first imported by the parser itself) and swallows the verdict.
    (tp / "shlex.py").write_text(
        "def quote(s):\n"
        "    if s.startswith('repo must'):\n"
        "        s = ''\n"
        "    return \"'\" + s.replace(\"'\", \"'\\\\''\") + \"'\"\n", encoding="utf-8")
    return _entry(tp, "evil.yaml", sha, repo=origin.as_uri()), "repo must be an https:// git URL"


@pytest.mark.parametrize("case", [
    _traversal_subdir, _symlinked_subdir, _symlink_behind_in_clone_link, _tag_object_sha,
    _newline_sha, _log_forging_repo, _symlinked_entry_file, _self_updater_in_spaced_dir,
    _bundled_native_binary, _committed_bytecode,
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
    assert f"PASS: {ok}" in res.stdout
    assert f"PASS: {ok2}" in res.stdout
    assert f"PASS: {crash}" not in res.stdout


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
    assert f"PASS: {entry}" in res.stdout


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
    entry = tmp_path / "parity.yaml"
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


def _step_env(name_prefix: str) -> dict:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for job in data["jobs"].values():
        for step in job["steps"]:
            if step.get("name", "").startswith(name_prefix):
                return {k: str(v) for k, v in (step.get("env") or {}).items()}
    raise LookupError(name_prefix)


def _pr_merge(repo: Path) -> None:
    """Shape of GitHub's pull_request checkout: a merge commit of branch `pr`
    whose first parent is the base tip, checked out detached."""
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "pr")
    _git(repo, "checkout", "-q", "--detach")


def _find_changed(tmp_path: Path, repo: Path, upstream: Path | None = None):
    """Run the changed-entries step with its workflow env. The step's upstream
    URL is rewritten to the `upstream` fixture, or to a missing path when None."""
    out = tmp_path / "gh-output.txt"
    out.write_text("", encoding="utf-8")
    env = _step_env("Find changed catalog")
    target = upstream if upstream is not None else tmp_path / "no-upstream"
    env = {
        **os.environ, **env, "GITHUB_OUTPUT": str(out),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.{target.as_uri()}.insteadOf",
        # Fallback so the harness also runs an older workflow copy (RED proof).
        "GIT_CONFIG_VALUE_0": env.get("UPSTREAM_REPO", UPSTREAM_URL),
    }
    res = _run(_step("Find changed catalog"), tmp_path, repo, env)
    lines = out.read_text(encoding="utf-8").splitlines()
    return res, [f for f in lines if f and "__EOF__" not in f and f != "files<<__EOF__"]


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
    """Renames and typechanges must reach the pinned gate; an entry PR that also
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
    reach the pinned-source gate."""
    up, fork = _sync_fixture(tmp_path)
    res, files = _find_changed(tmp_path, fork, upstream=up)
    assert res.returncode == 0, res.stdout + res.stderr
    assert files == SYNCED
    assert "Upstream sync: every changed entry matches upstream" in res.stdout


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
    assert res.returncode == 1, res.stdout + res.stderr
    assert "may only touch plugin-catalog/**" in res.stdout
    untrusted = res.stdout.split("Entries not identical to upstream", 1)[1]
    assert culprit in untrusted
    assert files == []


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
    trees alone: here the promisor remote is unreachable, as the token-less
    https remote is in CI."""
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
