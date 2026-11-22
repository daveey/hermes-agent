"""workspace_source core PR-2: the ending preflight and publish (plan v2 §4 steps 1-8, v3 §2/§9, v4 S3/S6) with real
git: a bare origin standing in for GitHub ``acme/widget`` and per-home registered clones (fixtures shared with the
PR-1 resolver tests). The Board is :class:`FakeSourceBoard`; the GitHub identity lookup is the resolver tests' double.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_source_board as kbs
from hermes_cli import kanban_source_preflight as pre
from hermes_cli import kanban_source_publish as pub
from hermes_cli import kanban_workspace_source as kws
from tests.hermes_cli import kanban_source_support as fake
from tests.hermes_cli.test_kanban_source_resolver import commit, git, make_world, use_home

SECRET = "sk-" + "A1b2C3d4E5f6G7h8J9k0"


@pytest.fixture
def world(tmp_path, monkeypatch):
    return make_world(tmp_path, monkeypatch)


class Card:
    """A running, attached card on one home: its worktree ``wt/<id>`` in the home's registered clone."""

    def __init__(self, world, home: str, board: fake.FakeSourceBoard, *, branch: str | None = None):
        self.world, self.board = world, board
        self.conn, self.cfg = use_home(world, home)
        fake.install(board)
        sync.set_mode(self.conn, "primary")
        self.id = kb.create_task(self.conn, title="src card", assignee="worker")
        with kbc.write_txn(self.conn):
            self.conn.execute("UPDATE tasks SET workspace_kind='worktree' WHERE id=?", (self.id,))
        clone = world.homes[home].clone
        self.branch = branch or f"wt/{self.id}"
        self.path = clone / ".worktrees" / self.id
        git(clone, "fetch", "-q", "origin")
        git(clone, "worktree", "add", "-q", "-b", self.branch, str(self.path), "origin/main")
        assert kb.claim_task(self.conn, self.id, ttl_seconds=900, claimer=f"{kb._host_prefix()}:1")
        with kbc.write_txn(self.conn):  # the Board has acknowledged the card (the ordinary custody case)
            self.conn.execute("UPDATE kanban_sync_state SET applying = 1")
            self.conn.execute("UPDATE tasks SET sync_version = 1 WHERE id = ?", (self.id,))
            self.conn.execute("UPDATE kanban_sync_state SET applying = 0")
            self.conn.execute("DELETE FROM kanban_sync_outbox")
        self.run_id = kb.get_task(self.conn, self.id).current_run_id
        self.run_uid = sync.run_uid(self.conn, self.run_id)
        kbs.record_run(self.conn, kbs.AttachedRun(
            run_id=self.run_id, task_id=self.id, run_uid=self.run_uid, mode="legacy_adopt", manifest_revision=0,
            branch=self.branch, path=str(self.path), pre_head=git(self.path, "rev-parse", "HEAD"), manifest=None,
            state="attached"))

    def run(self):
        return kbs.attached_run(self.conn, self.run_id)

    def receipt(self, **kw) -> dict:
        return pre.compute_receipt(self.conn, self.id, self.run(), **kw)

    def remote(self) -> str | None:
        out = git(self.world.origin, "for-each-ref", "--format=%(objectname)", f"refs/heads/{self.branch}")
        return out or None


@pytest.fixture
def board():
    b = fake.FakeSourceBoard()
    yield b
    kbs.reset_board_lacks_source()


@pytest.fixture
def card(world, board):
    return Card(world, "hub", board)


# --- happy path + S3 -----------------------------------------------------------------------------------------------

def test_publish_pushes_and_records_manifest(card, board):
    head = commit(card.path, "feature.txt", "work")
    receipt = card.receipt()
    assert (receipt["state"], receipt["commit"], receipt["manifest_revision"]) == ("published", head, 1)
    assert card.remote() == head
    sent = board.body("publish_source")
    assert sent["expected_revision"] == 0 and sent["run_uid"] == card.run_uid
    m = sent["manifest"]
    assert (m["branch"], m["commit"], m["repository"]["id"], m["manifest_revision"]) == (
        f"refs/heads/{card.branch}", head, 42, 1)
    assert m["published_by"] == {"run_uid": card.run_uid, "machine": sync._state(card.conn)["gateway_id"]}
    kws.parse_manifest(json.dumps(m))  # the Board would accept its shape
    assert "renew" in board.actions()  # the lease fence ran before the push


def test_unchanged_sends_no_publish(card, board):
    commit(card.path, "feature.txt", "work")
    first = card.receipt()
    board.calls.clear()
    again = card.receipt()
    assert again == {"run_uid": card.run_uid, "state": "unchanged", "manifest_revision": 1, "commit": first["commit"]}
    assert "publish_source" not in board.actions() and "renew" not in board.actions()


def test_publish_disabled_on_park_is_retained(card, board, monkeypatch):
    commit(card.path, "feature.txt", "work")
    assert card.receipt(publish=False)["reason"] == "publish_disabled"
    assert card.remote() is None


# --- refusals: retained, nothing pushed --------------------------------------------------------------------------

def _refused(card, code):
    receipt = card.receipt()
    assert receipt["state"] == "retained", receipt
    assert receipt["reason"] == f"publish_error={code}", receipt
    assert card.remote() is None
    assert "publish_source" not in card.board.actions()
    ev = card.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='workspace_source_publish_refused'",
                           (card.id,)).fetchone()
    assert json.loads(ev[0])["code"] == code


def test_dirty_worktree_refused(card):
    commit(card.path, "feature.txt", "work")
    (card.path / "scratch.txt").write_text("untracked")
    _refused(card, "dirty_source")


def test_ignored_private_file_refused(card):
    (card.path / ".gitignore").write_text("secrets/\n__pycache__/\n")
    commit(card.path, ".gitignore", "secrets/\n__pycache__/\n")
    (card.path / "__pycache__").mkdir()
    (card.path / "__pycache__" / "x.pyc").write_text("cache")  # allowlisted cache: fine
    (card.path / "secrets").mkdir()
    (card.path / "secrets" / "token").write_text("private")
    _refused(card, "ignored_private_files")


def test_secret_added_then_removed_across_two_commits_refused(card):
    commit(card.path, "config.py", f"KEY = '{SECRET}'\n")
    commit(card.path, "config.py", "KEY = None\n")
    _refused(card, "secret_scan_refused")


def test_secret_in_commit_message_refused(card):
    (card.path / "a.txt").write_text("a")
    git(card.path, "add", "a.txt")
    git(card.path, "commit", "-q", "-m", f"oops {SECRET}")
    _refused(card, "secret_scan_refused")


@pytest.mark.parametrize("header", ["gpgsig", "gpgsig-sha256", "mergetag"])
def test_signature_shaped_message_line_cannot_hide_the_body(card, header):
    """A message line that looks like a signature header is message text; the indented lines after it are scanned
    (review 5891362210: ``subject\\n\\ngpgsig x\\n token = sk-...`` was published)."""
    (card.path / "a.txt").write_text("a")
    git(card.path, "add", "a.txt")
    git(card.path, "commit", "-q", "--cleanup=verbatim", "-m", f"subject\n\n{header} x\n token = {SECRET}\n")
    _refused(card, "secret_scan_refused")


def _raw_commit(card, headers: str, message: str) -> str:
    """Write a hand-built commit on top of HEAD (extra ``headers`` after committer) and move the branch to it."""
    parent = git(card.path, "rev-parse", "HEAD")
    tree = git(card.path, "rev-parse", "HEAD^{tree}")
    who = "T <t@example.com> 1700000000 +0000"
    raw = f"tree {tree}\nparent {parent}\nauthor {who}\ncommitter {who}\n{headers}\n{message}"
    sha = subprocess.run(["git", "-C", str(card.path), "hash-object", "-t", "commit", "-w", "--stdin"],
                         input=raw.encode(), check=True, capture_output=True).stdout.decode().strip()
    git(card.path, "reset", "-q", "--hard", sha)
    return sha


_ARMOR = ("-----BEGIN PGP SIGNATURE-----\n \n iQEzBAABCAAdFiEEabcdefghijklmnopqrstuvwxyz0123456789+/=\n =AbCd\n"
          " -----END PGP SIGNATURE-----")


def test_mergetag_tag_message_is_scanned(card):
    """An embedded mergetag is pushed content: the raw commit, mergetag included, goes to the matcher."""
    tagged = git(card.path, "rev-parse", "HEAD")
    tag = (f"mergetag object {tagged}\n type commit\n tag v1\n tagger T <t@example.com> 1700000000 +0000\n \n"
           f" release notes {SECRET}\n {_ARMOR}")
    _raw_commit(card, tag, "merge v1\n")
    _refused(card, "secret_scan_refused")


def test_secret_in_signature_block_is_scanned(card):
    """Signature blocks are not skipped: the whole raw commit object is matched."""
    _raw_commit(card, f"gpgsig -----BEGIN PGP SIGNATURE-----\n token = {SECRET}\n -----END PGP SIGNATURE-----",
                "signed\n")
    _refused(card, "secret_scan_refused")


def test_real_signature_armor_does_not_trip_the_scan(card):
    head = _raw_commit(card, f"gpgsig {_ARMOR}", "signed\n")
    assert card.receipt()["state"] == "published" and card.remote() == head


def test_denylisted_rename_refused(card):
    commit(card.path, "notes.txt", "plain")
    git(card.path, "mv", "notes.txt", ".env.production")
    git(card.path, "commit", "-q", "-m", "rename")
    _refused(card, "secret_scan_refused")


def test_secret_scan_ignores_display_redaction_setting(card, monkeypatch):
    """``security.redact_secrets: false`` disables display masking, never the publish gate."""
    from agent import redact
    real = redact.redact_sensitive_text
    monkeypatch.setattr(redact, "redact_sensitive_text",
                        lambda text, *, force=False, **kw: real(text, force=force, **kw) if force else text)
    commit(card.path, "config.py", f"KEY = '{SECRET}'\n")
    _refused(card, "secret_scan_refused")


def test_secret_scan_survives_hostile_diff_config(card):
    """The clone's own config must not blind the scan: colour codes, missing a/ b/ prefixes, external diff drivers."""
    for key, value in (("color.ui", "always"), ("color.diff", "always"), ("diff.noprefix", "true"),
                       ("diff.mnemonicPrefix", "true"), ("diff.external", "/bin/true"), ("core.quotePath", "true")):
        git(card.path, "config", key, value)
    commit(card.path, "config.py", f"KEY = '{SECRET}'\n")
    _refused(card, "secret_scan_refused")


def test_secret_in_nondiff_attribute_file_refused(card):
    """``-diff`` / ``binary`` attributes hide content from a plain patch; the scan reads it as text anyway."""
    commit(card.path, ".gitattributes", "*.cfg -diff\n*.dat binary\n")
    commit(card.path, "app.cfg", f"token = {SECRET}\n")
    _refused(card, "secret_scan_refused")


def _pem(kind: str) -> str:
    body = "\n".join(["b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW"] * 5)
    return f"-----BEGIN {kind}PRIVATE KEY-----\n{body}\n-----END {kind}PRIVATE KEY-----\n"


@pytest.mark.parametrize("name", ["deploy/id_ed25519", "deploy/notes.txt", "deploy/server.cert"])
def test_multiline_private_key_refused(card, name):
    commit(card.path, name, _pem("OPENSSH "))
    _refused(card, "secret_scan_refused")


def test_replace_graft_cannot_hide_a_pushed_commit(card):
    """``git replace --graft`` makes a later commit look parentless to plain git; the push still sends the real
    parent. The scan reads with replace refs off, so the secret commit A is scanned (review 5890513430)."""
    base = git(card.path, "rev-parse", "HEAD")
    commit(card.path, "config.py", f"KEY = '{SECRET}'\n")
    head = commit(card.path, "config.py", "KEY = None\n")
    git(card.path, "replace", "--graft", head, base)
    _refused(card, "secret_scan_refused")


def test_info_grafts_file_cannot_hide_a_pushed_commit(card):
    base = git(card.path, "rev-parse", "HEAD")
    commit(card.path, "config.py", f"KEY = '{SECRET}'\n")
    head = commit(card.path, "config.py", "KEY = None\n")
    common = Path(git(card.path, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    (common / "info").mkdir(exist_ok=True)
    (common / "info" / "grafts").write_text(f"{head} {base}\n")
    _refused(card, "secret_scan_refused")


@pytest.mark.parametrize("line", ["++ token = {s}", "+++ token = {s}", "--- token = {s}", "@@ -1 +1 @@ {s}",
                                  "diff --git a/x b/x {s}"])
def test_added_line_that_looks_like_a_diff_header_is_scanned(card, line):
    """Content is read as a raw blob, so a line shaped like a patch header is still content."""
    commit(card.path, "notes.txt", "intro\n" + line.format(s=SECRET) + "\n")
    _refused(card, "secret_scan_refused")


@pytest.mark.parametrize("codec", ["utf-16", "utf-16-le", "utf-16-be", "utf-8-sig", "utf-32", "utf-32-le",
                                   "utf-32-be"])
def test_non_utf8_text_encodings_are_scanned(card, codec):
    (card.path / "wide.txt").write_bytes(f"token = {SECRET}\n".encode(codec))
    git(card.path, "add", "wide.txt")
    git(card.path, "commit", "-q", "-m", "wide")
    _refused(card, "secret_scan_refused")


def test_key_inside_binary_blob_is_scanned(card):
    (card.path / "blob.bin").write_bytes(b"\x00\x01\xff\xfe" * 64 + _pem("RSA ").encode() + b"\x00\x9f" * 32)
    git(card.path, "add", "blob.bin")
    git(card.path, "commit", "-q", "-m", "bin")
    _refused(card, "secret_scan_refused")


def test_provider_token_inside_binary_blob_is_scanned(card):
    (card.path / "blob.bin").write_bytes(b"\x00\x01\xff\xfe" * 64 + f"token={SECRET}".encode() + b"\x00\x9f" * 32)
    git(card.path, "add", "blob.bin")
    git(card.path, "commit", "-q", "-m", "bin")
    _refused(card, "secret_scan_refused")


def _blob_sha_tail_plus_digit(prefix: bytes) -> bytes:
    """Content whose blob sha ends in ``+<digit>``: in the raw tree the next entry's ``100644`` mode follows those
    bytes, so ``+<digit>100644`` reads as an E.164 phone number (review 5892051198)."""
    import hashlib
    for i in range(1_000_000):
        body = prefix + f"# {i}\n".encode()
        tail = hashlib.sha1(b"blob %d\0" % len(body) + body).digest()[-2:]
        if tail[:1] == b"+" and tail[1:] in b"123456789":
            return body
    raise AssertionError("no candidate")


def test_tree_sha_bytes_never_false_match(card):
    """A benign new file whose blob sha forms a phone-shaped run with the next tree entry's mode is published:
    trees are matched by entry name only, never by their raw (binary sha) bytes."""
    (card.path / "zz_probe_a.py").write_bytes(_blob_sha_tail_plus_digit(b"print('benign')\n"))
    (card.path / "zz_probe_b.py").write_text("x = 1\n")
    git(card.path, "add", "zz_probe_a.py", "zz_probe_b.py")
    git(card.path, "commit", "-q", "-m", "benign files")
    tree = subprocess.run(["git", "-C", str(card.path), "cat-file", "tree", "HEAD"], capture_output=True,
                          check=True).stdout
    from agent.redact import _SIGNAL_PHONE_RE
    assert _SIGNAL_PHONE_RE.search(tree.decode("latin-1"))  # the fixture really has the shape
    head = git(card.path, "rev-parse", "HEAD")
    assert card.receipt()["state"] == "published" and card.remote() == head


def test_secret_in_tree_entry_name_refused(card):
    commit(card.path, f"conf/{SECRET}.txt", "plain\n")
    _refused(card, "secret_scan_refused")


def test_random_binary_blob_passes(card):
    import random
    data = random.Random(7).randbytes(256 * 1024)
    (card.path / "noise.bin").write_bytes(b"\x00" + data)
    git(card.path, "add", "noise.bin")
    git(card.path, "commit", "-q", "-m", "noise")
    head = git(card.path, "rev-parse", "HEAD")
    assert card.receipt()["state"] == "published" and card.remote() == head


def test_phone_shaped_bytes_in_binary_blob_pass_but_text_phone_refused(card):
    """PII-style patterns apply to text only; binary blobs get the high-signal patterns."""
    (card.path / "img.bin").write_bytes(b"\x00\x89PNG\xff" + b"+15551234567 " + b"\x00\xfe" * 16)
    git(card.path, "add", "img.bin")
    git(card.path, "commit", "-q", "-m", "image")
    head = git(card.path, "rev-parse", "HEAD")
    assert card.receipt()["state"] == "published" and card.remote() == head
    assert pub._secret_in("call +15551234567 now") and not pub._secret_in("call +15551234567 now", pii=False)
    assert pub._secret_in(f"a +15551234567 b {SECRET}", pii=False)


def test_global_config_cannot_reshape_the_scan_or_push(card, world, monkeypatch):
    """The scan and the push ignore global config (attributes file, hooks path); a global pre-push hook never runs."""
    hooks = world.tmp / "global-hooks"
    hooks.mkdir()
    marker = world.tmp / "hook-ran"
    (hooks / "pre-push").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (hooks / "pre-push").chmod(0o755)
    (world.tmp / "attrs").write_text("* -diff binary\n")
    gc = Path(os.environ["GIT_CONFIG_GLOBAL"])
    gc.write_text(gc.read_text() + f"[core]\n\thooksPath = {hooks}\n\tattributesFile = {world.tmp / 'attrs'}\n")
    head = commit(card.path, "feature.txt", "work")
    assert card.receipt()["state"] == "published" and card.remote() == head
    assert not marker.exists()


def test_benign_unchanged_denylisted_file_on_base_is_not_refused(card, world):
    """Only additions count: a base-tree ``.env.example`` carried unchanged in a new tree is not re-refused."""
    author = world.tmp / "author"
    commit(author, ".env.example", "PLACEHOLDER=1\n")
    git(author, "push", "-q", "origin", "main")
    git(card.path, "fetch", "-q", "origin")
    git(card.path, "merge", "-q", "--ff-only", "origin/main")
    head = commit(card.path, "feature.txt", "work")
    assert card.receipt()["state"] == "published" and card.remote() == head


def _plant_blob(card, rng, secret, seed, prefix, deep=False):
    name = f"{prefix}{'x/y/' if deep else ''}cfg{seed}.txt"
    commit(card.path, name, "a\n" * rng.randint(0, 5) + f"key: {secret}\n" + "b\n" * rng.randint(0, 5))
    commit(card.path, name, "redacted\n")


def _plant_message(card, rng, secret, seed, prefix):
    (card.path / f"m{seed}.txt").write_text("m")
    git(card.path, "add", f"m{seed}.txt")
    git(card.path, "commit", "-q", "-m", f"subject\n\nbody {secret} tail")


def _plant_path(card, rng, secret, seed, prefix):
    name = f"{prefix}{secret}.txt"
    (card.path / name).parent.mkdir(parents=True, exist_ok=True)
    (card.path / name).write_text("plain\n")
    git(card.path, "add", name)
    git(card.path, "commit", "-q", "-m", "add a file")  # the message must not carry the name
    git(card.path, "rm", "-q", name)
    git(card.path, "commit", "-q", "-m", "drop")


def _plant_mergetag(card, rng, secret, seed, prefix):
    tagged = git(card.path, "rev-parse", "HEAD")
    _raw_commit(card, f"mergetag object {tagged}\n type commit\n tag v{seed}\n"
                      f" tagger T <t@example.com> 1700000000 +0000\n \n notes {secret}\n {_ARMOR}", "merge\n")


_PLANTERS = {
    "blob": _plant_blob,
    "deep-blob": lambda *a: _plant_blob(*a, deep=True),
    "message": _plant_message,
    "path": _plant_path,
    "header": lambda card, rng, secret, *_: _raw_commit(card, f"x-note {secret}", "plain\n"),
    "signature": lambda card, rng, secret, *_: _raw_commit(
        card, f"gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQEz{rng.randint(0, 9)}\n {secret}\n =AbCd\n"
              " -----END PGP SIGNATURE-----", "signed\n"),
    "sig-shaped-message": lambda card, rng, secret, *_: _raw_commit(
        card, "", f"subject\n\n{rng.choice(['gpgsig', 'gpgsig-sha256', 'mergetag'])} x\n {secret}\n"),
    "mergetag": _plant_mergetag,
}


@pytest.mark.parametrize("seed", range(16))
def test_property_random_secret_in_any_new_object_is_refused(card, seed):
    """A random credential placed in a new blob, a path name, or anywhere in a commit object (message, an extra
    header, a gpgsig signature block, a signature-shaped message line, an embedded mergetag), behind 0-3 benign
    commits (and for blobs/paths removed again), is always refused and never pushed."""
    import random
    rng = random.Random(seed)
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz0123456789"
    secret = "sk-" + "".join(rng.choice(alphabet) for _ in range(rng.randint(24, 40)))
    for i in range(rng.randint(0, 3)):
        commit(card.path, f"pad{i}/f{rng.randint(0, 99)}.txt", f"benign {i}\n")
    places = list(_PLANTERS)
    folder = "/".join(f"d{rng.randint(0, 9)}" for _ in range(rng.randint(0, 3)))
    _PLANTERS[places[seed % len(places)]](card, rng, secret, seed, f"{folder}/" if folder else "")
    _refused(card, "secret_scan_refused")


def test_denylisted_non_ascii_path_refused(card):
    commit(card.path, "dé/server.pem", "not even a key\n")
    _refused(card, "secret_scan_refused")


def test_lfs_and_submodules_refused(card):
    commit(card.path, ".gitattributes", "*.bin filter=lfs diff=lfs merge=lfs -text\n")
    _refused(card, "source_lfs_unsupported")


def test_foreign_branch_refused(world, board):
    c = Card(world, "hub", board, branch="feature/shared")
    commit(c.path, "x.txt")
    _refused(c, "source_branch_foreign")


def test_protected_branch_refused(world, board):
    c = Card(world, "hub", board, branch="release/1.0")
    commit(c.path, "x.txt")
    _refused(c, "source_branch_protected")


def test_remote_diverged_refused(card, world):
    commit(card.path, "mine.txt")
    other = world.tmp / "other"
    subprocess.run(["git", "clone", "-q", str(world.origin), str(other)], check=True, capture_output=True)
    git(other, "checkout", "-q", "-b", card.branch)
    commit(other, "theirs.txt")
    git(other, "push", "-q", "origin", card.branch)
    receipt = card.receipt()
    assert receipt["reason"] == "publish_error=remote_diverged"
    assert "publish_source" not in card.board.actions()


def test_lease_lost_before_push_refused(card, board):
    commit(card.path, "x.txt")
    board.renew_mode = "deny"
    _refused(card, "publish_lease_lost")


def test_publish_source_refused_is_retained_after_push(card, board):
    """Lease lost between push and publish_source: the branch moved by a fenced ff push, the receipt is retained."""
    head = commit(card.path, "x.txt")
    board.refuse["publish_source"] = "lease_lost"
    receipt = card.receipt()
    assert receipt["state"] == "retained" and receipt["reason"] == "publish_error=publish_source:lease_lost"
    assert card.remote() == head


def test_preflight_own_timeout_is_retained(card, monkeypatch):
    commit(card.path, "x.txt")
    card.cfg.__class__  # frozen dataclass: build a copy with a zero budget
    cfg = kws.Settings(**{**card.cfg.__dict__, "preflight_timeout_s": 0.0})
    monkeypatch.setattr(kws, "settings", lambda config=None: cfg)
    receipt = card.receipt()
    assert receipt["state"] == "retained" and receipt["reason"] == "publish_error=preflight_timeout"
    assert card.remote() is None


def test_preflight_never_raises(card, monkeypatch):
    monkeypatch.setattr(pub, "publish", lambda *a, **kw: 1 / 0)
    commit(card.path, "x.txt")
    assert card.receipt()["reason"] == "preflight_error=ZeroDivisionError"


def test_rejected_push_classified(card, monkeypatch):
    commit(card.path, "x.txt")
    hook = card.world.origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'rejected by policy' >&2\nexit 1\n")
    hook.chmod(0o755)
    receipt = card.receipt()
    assert receipt["reason"].startswith("publish_error=push_failed:")
    assert card.remote() is None


def test_unpublished_park_refusal_names_the_publish_refusal(card, monkeypatch):
    """The worktree's clone is not registered (a second checkout of the same repo): the agent-facing
    ``unpublished_workspace`` refusal names ``source_clone_unregistered``, the clone and the config key, not only the
    ``workspace_source_publish_refused`` event."""
    commit(card.path, "x.txt")
    cfg = kws.Settings(**{**card.cfg.__dict__, "clones": {}})
    monkeypatch.setattr(kws, "settings", lambda config=None: cfg)
    with kbc.write_txn(card.conn):
        card.conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(card.path), card.id))
    with pytest.raises(ValueError) as refused:
        kb.schedule_task(card.conn, card.id, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z", clock_only=True, awaiting={"kind": "clock", "why": "fixture window"},
                         expected_run_id=card.run_id)
    text = str(refused.value)
    assert text.startswith("unpublished_workspace:")
    assert "workspace_source publish refused: source_clone_unregistered:" in text
    # The exact clone root (the value to register), closed by the detail's paren: not ``<clone>/.git``.
    assert f"(its clone is {card.world.homes['hub'].clone})" in text
    assert "kanban.workspace_source.clones" in text
    task = kb.get_task(card.conn, card.id)
    assert task is not None and task.status == "running"  # refused: run ownership intact
    assert card.remote() is None


def test_unregistered_refusal_names_a_bare_repo_itself(world, tmp_path):
    """A worktree of a bare repository: its git common dir is the repository itself (not named ``.git``), so the
    refusal names that directory, not its parent."""
    path = tmp_path / "bare-wt"
    git(world.origin, "worktree", "add", "-q", "--detach", str(path), "main")
    cfg = kws.Settings(**{**use_home(world, "hub")[1].__dict__, "clones": {}})
    with pytest.raises(pub.PublishRefused) as refused:
        pub.prove_origin(str(path), cfg)
    assert refused.value.code == "source_clone_unregistered"
    assert refused.value.detail.endswith(f"(its clone is {world.origin})")


def test_publish_refusal_hint_is_scoped_to_the_preflighted_run():
    """Only the run being preflighted gets its refusal named; another card or run (a nested ending) gets none."""
    assert pre.refusal_hint("t_a", 7) == ""
    token = pre._REFUSALS.set(("t_a", 7, []))
    try:
        assert pre.refusal_hint("t_a", 7) == ""
        pre._note_refusal("t_b", 7, "dirty_source", "x")
        pre._note_refusal("t_a", 8, "dirty_source", "x")
        assert pre.refusal_hint("t_a", 7) == ""
        pre._note_refusal("t_a", 7, "dirty_source", "untracked files")
        assert pre.refusal_hint("t_a", 7) == " (workspace_source publish refused: dirty_source: untracked files)"
        assert pre.refusal_hint("t_b", 7) == "" and pre.refusal_hint("t_a", 8) == ""
        pre._note_refusal("t_a", 7, "source_clone_unregistered", "")
        assert pre.refusal_hint("t_a", 7).startswith(
            " (workspace_source publish refused: source_clone_unregistered; register the worktree's clone under "
            "kanban.workspace_source.clones")
    finally:
        pre._REFUSALS.reset(token)
    assert pre.refusal_hint("t_a", 7) == ""


def test_unpublished_park_refusal_without_publish_refusal_is_unchanged(card, monkeypatch):
    """No publish was attempted (publish_on_park off): the refusal carries no publish hint."""
    commit(card.path, "x.txt")
    cfg = kws.Settings(**{**card.cfg.__dict__, "publish_on_park": False})
    monkeypatch.setattr(kws, "settings", lambda config=None: cfg)
    with kbc.write_txn(card.conn):
        card.conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(card.path), card.id))
    with pytest.raises(ValueError) as refused:
        kb.schedule_task(card.conn, card.id, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z", clock_only=True, awaiting={"kind": "clock", "why": "fixture window"},
                         expected_run_id=card.run_id)
    assert str(refused.value).endswith("to the checkpoint/reason")


# --- endings carry the receipt; the ending always proceeds (S6) ------------------------------------------------------

def _leaving_op(conn, tid, status):
    from hermes_cli import kanban_sync_client as ksc, kanban_sync_wire as wire_ops
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM kanban_sync_outbox WHERE table_name='tasks' AND row_uid=? AND state='pending' ORDER BY rowid",
        (tid,))]
    ops = [wire_ops.translate(ksc._full_row_if_unseen(conn, r), "hlc") for r in rows]
    return [o for o in ops if o["fields"].get("status") == status]


def test_complete_task_publishes_and_carries_published_receipt(card):
    head = commit(card.path, "x.txt")
    assert kb.complete_task(card.conn, card.id, result="ok", summary="done", expected_run_id=card.run_id)
    (op,) = _leaving_op(card.conn, card.id, "done")
    assert op["fields"]["source_receipt"]["state"] == "published"
    assert op["fields"]["source_receipt"]["commit"] == head == card.remote()


def test_publish_refusal_never_blocks_the_ending(card):
    commit(card.path, "x.txt")
    (card.path / "dirty.txt").write_text("x")
    assert kb.block_task(card.conn, card.id, reason="1) look at it", kind="needs_input", expected_run_id=card.run_id)
    (op,) = _leaving_op(card.conn, card.id, "blocked")
    assert op["fields"]["source_receipt"]["state"] == "retained"


def test_scheduled_continue_carries_receipt(card):
    commit(card.path, "x.txt")
    assert kb.schedule_task(card.conn, card.id, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z", clock_only=True, awaiting={"kind": "clock", "why": "fixture window"},
                            expected_run_id=card.run_id)
    (op,) = _leaving_op(card.conn, card.id, "scheduled")
    assert op["fields"]["source_receipt"]["state"] == "published"


def test_review_handoff_carries_receipt(card):
    commit(card.path, "x.txt")
    assert kb.request_review(card.conn, card.id, summary="ready")
    (op,) = _leaving_op(card.conn, card.id, "review")
    assert op["fields"]["source_receipt"]["state"] == "published"


def test_involuntary_timeout_sends_no_receipt(card):
    """A reclaim (timeout/crash) cannot publish: no receipt rides it, so custody stays retained with this holder."""
    commit(card.path, "x.txt")
    assert kb.reclaim_task(card.conn, card.id, reason="timed out")
    rows = [json.loads(r[0]) for r in card.conn.execute(
        "SELECT fields FROM kanban_sync_outbox WHERE table_name='tasks' AND row_uid=?", (card.id,))]
    assert not any("source_receipt" in r for r in rows)
    assert card.remote() is None


# --- two clones: resume the exact branch + SHA after the original worktree is gone ----------------------------------

def test_two_clone_resume_exact_branch_and_sha_after_worktree_deleted(world, board):
    from hermes_cli import kanban_source_clones as kcl
    from hermes_cli import kanban_source_resolver as ksr
    a = Card(world, "hub", board)
    head = commit(a.path, "feature.txt", "from hub")
    assert kb.complete_task(a.conn, a.id, result="ok", summary="done", expected_run_id=a.run_id) is not None
    published = board.manifest[a.id]
    git(world.homes["hub"].clone, "worktree", "remove", "--force", str(a.path))
    assert not a.path.exists()
    conn_b, cfg_b = use_home(world, "worker-2")
    snapshot = kws.SourceSnapshot(json.dumps(published), kws.Custody("hub", "published", published["manifest_revision"]))
    task = type("T", (), {"id": a.id, "workspace_path": None, "branch_name": None})()
    out = ksr.resolve_source_workspace(conn_b, task, snapshot, me="worker-2", cfg=cfg_b, lookup=kcl.github_repo)
    assert (out.branch, out.commit) == (a.branch, head)
    assert git(out.path, "rev-parse", "HEAD") == head
    assert git(out.path, "symbolic-ref", "--short", "HEAD") == a.branch
    assert Path(out.path).joinpath("feature.txt").read_text() == "from hub"
    shutil.rmtree(world.tmp / "other", ignore_errors=True)
