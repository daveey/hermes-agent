"""Trusted owner-target observations with real local Git and source files."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from hermes_cli import kanban_owner_target as target

REF = "owner-target:mini/core"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()


def write(path, value):
    path.write_text(json.dumps(value))


@pytest.fixture
def source(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@invalid.local")
    git(repo, "config", "user.name", "Test")
    git(repo, "commit", "--allow-empty", "-m", "old")
    old = git(repo, "rev-parse", "HEAD")
    git(repo, "commit", "--allow-empty", "-m", "new")
    new = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/pull/7/head", new)
    git(repo, "update-ref", "refs/remotes/origin/main", new)
    queue = tmp_path / "queue.json"
    entry = {"pr": 7, "id": "generation1", "state": "queued", "judged_head": new,
             "vet": {"state": "ready", "head": new}}
    write(queue, {"entries": [entry]})
    fresh = tmp_path / "fetch.stamp"
    fresh.touch()
    stamp = tmp_path / "installed.json"
    write(stamp, {"core_sha": old})
    train = tmp_path/'train.json'
    write(train, {'skip':{}})
    observation = tmp_path / 'pr-observation.json'
    fact = dict(version=1, repository='example/project', pr=7, enqueue_generation='generation1',
                head=new, observed_at=time.time(), state='current')
    write(observation, fact)
    config = {"kind": "queue", "queue_path": str(queue), "repository": 'example/project', "pr": 7,
              "observation_path": str(observation),
              "pause_path": str(tmp_path / "PAUSE"), 'train_path':str(train)}
    registry = tmp_path / "registry.json"
    write(registry, {"version": 1, "authority": "mini", "sources": {"core": config}})
    return dict(repo=repo, queue=queue, fresh=fresh, stamp=stamp, config=config,
                registry=registry, entry=entry, old=old, new=new, observation=observation, fact=fact)


def observe(s, selector=REF):
    return target.observe(selector, registry_path=s["registry"])


def save_config(s):
    write(s["registry"], {"version": 1, "authority": "mini", "sources": {"core": s["config"]}})


def update_config(s):
    git(s['repo'], 'remote', 'add', 'origin', 'https://example.com/project.git')
    s["config"] = {"kind": "update", "repo_path": str(s["repo"]),
                   "repository": 'https://example.com/project.git', "channel": "main",
                   "remote_ref": "refs/heads/main", "target_ref": "refs/remotes/origin/main",
                   "observation_path": str(s['fresh']),
                   "stamp_path": str(s["stamp"]), "stamp_sha_key": "core_sha"}
    s['update_fact'] = dict(version=1, host=target.socket.gethostname(), repo_path=str(s['repo'].resolve()),
                            repository=s['config']['repository'], channel='main', remote_ref='refs/heads/main',
                            target_ref='refs/remotes/origin/main', head=s['new'], observed_at=time.time(), state='current')
    write(s['fresh'], s['update_fact'])
    save_config(s)


def test_pending_revision_binds_enqueue_and_full_reviewed_head(source):
    s = source
    before = observe(s)
    assert before["disposition"] == "pending"
    assert before['target']['identity'].startswith(REF+'@')
    assert len(before['target']['identity'].rsplit('@',1)[1]) == 64
    assert before["target"] == {"kind": "queue", "identity": before['target']['identity'],
                                "revision": "generation1:" + s["new"]}
    assert abs(before["observed_at"] - time.time()) < 2
    assert set(before) == {"version", "observed_at", "selector", "target", "disposition"}
    s["entry"]["id"] = "generation2"
    write(s["queue"], {"entries": [s["entry"]]})
    assert observe(s)['disposition'] == 'unavailable'
    s['fact']['enqueue_generation'] = 'generation2'
    write(s['observation'], s['fact'])
    assert observe(s)['disposition'] == 'pending'
    assert observe(s)["target"]["revision"] != before["target"]["revision"]


@pytest.mark.parametrize("selector", ["owner-target:other/core", "owner-target:mini/unknown",
    "owner-target:mini/../core", "owner-target:mini/core\n", "owner-target:mini/" + "x" * 65,
    "owner-target:mini/core --command=x", "owner-target:mini//etc/passwd"])
def test_selector_authority_and_literal_validation(source, selector):
    assert observe(source, selector)["disposition"] == "unavailable"


@pytest.mark.parametrize("change", ["head", "judged", "duplicate", "missing", "id", "vet", "state"])
def test_missing_ambiguous_or_changed_queue_fails_closed(source, change):
    s = source
    entries = [s["entry"]]
    def change_head():
        s['fact']['head'] = s['old']
        write(s['observation'], s['fact'])

    changes = {
        "head": change_head,
        "judged": lambda: entries[0].update(judged_head=s["old"]),
        "duplicate": lambda: entries.append(dict(entries[0])),
        "missing": entries.clear,
        "id": lambda: entries[0].pop("id"),
        "vet": lambda: entries[0].pop("vet"),
        "state": lambda: entries[0].update(state="merged"),
    }
    changes[change]()
    write(s["queue"], {"entries": entries})
    assert observe(s)["disposition"] == "unavailable"


@pytest.mark.parametrize("adverse", ["hold_reason", "vet_reason", "paused", "marker", "held", "rejected"])
def test_adverse_queue_never_pending(source, adverse):
    s = source
    if adverse == "marker":
        Path(s["config"]["pause_path"]).touch()
    elif adverse in {"held", "rejected"}:
        s["entry"]["vet"]["state"] = adverse
    else:
        s["entry"][adverse] = True if adverse == "paused" else "adverse"
    write(s["queue"], {"entries": [s["entry"]]})
    assert observe(s)["disposition"] == "failed"


@pytest.mark.parametrize("file", ["queue", "observation"])
@pytest.mark.parametrize("age", [1201, -60])
def test_stale_and_future_source_unavailable(source, file, age):
    when = time.time() - age
    os.utime(source[file], (when, when))
    assert observe(source)["disposition"] == "unavailable"


def test_terminal_history_does_not_hide_live_generation(source):
    s = source
    history = dict(s["entry"], id="oldgeneration", state="merged")
    write(s["queue"], {"entries": [history, s["entry"]]})
    assert observe(s)["disposition"] == "pending"


def test_update_pending_then_installed_then_descendant(source):
    s = source
    update_config(s)
    assert observe(s)["disposition"] == "pending"
    assert observe(s)["target"]["revision"] == s["new"]
    write(s["stamp"], {"core_sha": s["new"]})
    old = time.time() - 86400
    os.utime(s["stamp"], (old, old))  # an installed build need not be rewritten every tick
    assert observe(s)["disposition"] == "satisfied"
    git(s["repo"], "commit", "--allow-empty", "-m", "newer installed")
    write(s["stamp"], {"core_sha": git(s["repo"], "rev-parse", "HEAD")})
    assert observe(s)["disposition"] == "satisfied"


@pytest.mark.parametrize('field,value', [
    ('version', True), ('version', 2), ('host', 'another-host'), ('repo_path', '/wrong/repo'),
    ('repository', 'https://example.com/other.git'), ('channel', 'canary'),
    ('remote_ref', 'refs/heads/canary'), ('target_ref', 'refs/remotes/origin/canary'),
    ('head', 'short'), ('head', 'a' * 40), ('state', 'unavailable'),
    ('observed_at', True), ('observed_at', 0), ('observed_at', float('nan')),
    ('extra', 'unknown'),
])
def test_update_producer_fact_exact_schema(source, field, value):
    update_config(source)
    source['update_fact'][field] = value
    write(source['fresh'], source['update_fact'])
    assert observe(source)['disposition'] == 'unavailable'


@pytest.mark.parametrize('failure', ['retouch', 'future', 'refchange', 'originchange', 'missing', 'fifo', 'oversize'])
def test_update_fact_cannot_be_renewed_by_scanner(source, failure):
    s = source
    update_config(s)
    def retimestamp(offset):
        s['update_fact']['observed_at'] = time.time() + offset
        write(s['fresh'], s['update_fact'])
        s['fresh'].touch()

    def remove_fact(*, fifo=False):
        s['fresh'].unlink()
        if fifo:
            os.mkfifo(s['fresh'])

    changes = {
        'retouch': lambda: retimestamp(-1201),
        'future': lambda: retimestamp(60),
        'refchange': lambda: git(s['repo'], 'update-ref', s['config']['target_ref'], s['old']),
        'originchange': lambda: git(s['repo'], 'remote', 'set-url', 'origin', 'https://example.com/other.git'),
        'oversize': lambda: s['fresh'].write_bytes(b' ' * 16385),
        'missing': remove_fact,
        'fifo': lambda: remove_fact(fifo=True),
    }
    changes[failure]()
    assert observe(s)['disposition'] == 'unavailable'


def test_update_ref_change_during_ancestry_read_fails_closed(source, monkeypatch):
    update_config(source)
    ancestry = target._ancestry
    def changed(repo, older, newer):
        result = ancestry(repo, older, newer)
        git(repo, 'update-ref', source['config']['target_ref'], source['old'])
        return result
    monkeypatch.setattr(target, '_ancestry', changed)
    assert observe(source)['disposition'] == 'unavailable'


def test_update_diverged_is_not_pending(source):
    s = source
    update_config(s)
    git(s["repo"], "checkout", "--detach", s["old"])
    git(s["repo"], "commit", "--allow-empty", "-m", "divergent")
    write(s["stamp"], {"core_sha": git(s["repo"], "rev-parse", "HEAD")})
    assert observe(s)["disposition"] == "failed"


@pytest.mark.parametrize("failure", ["shortsha", "unknownsha", "stampmissing", "refmissing", "stale", "wrongkey"])
def test_update_missing_and_stale_fail_closed(source, failure):
    s = source
    update_config(s)
    def stale_fact():
        when = time.time() - 1201
        os.utime(s["fresh"], (when, when))

    def wrong_key():
        s["config"]["stamp_sha_key"] = "arbitrary.path"
        save_config(s)

    changes = {
        "shortsha": lambda: write(s["stamp"], {"core_sha": s["old"][:12]}),
        "unknownsha": lambda: write(s["stamp"], {"core_sha": "a" * 40}),
        "stampmissing": s["stamp"].unlink,
        "refmissing": lambda: git(s["repo"], "update-ref", "-d", s["config"]["target_ref"]),
        "stale": stale_fact,
        "wrongkey": wrong_key,
    }
    changes[failure]()
    assert observe(s)["disposition"] == "unavailable"


@pytest.mark.parametrize("failure", ["oversize", "duplicates", "malformed", "missing", "command", "identity", "boolversion"])
def test_registry_explicit_bounded_and_no_commands(source, failure):
    s = source
    def extra_config(field):
        s["config"][field] = "do-not-execute-or-trust"
        save_config(s)

    changes = {
        "oversize": lambda: s["registry"].write_bytes(b" " * (target.MAX_BYTES + 1)),
        "duplicates": lambda: s["registry"].write_text('{"version":1,"version":1,"authority":"mini","sources":{}}'),
        "malformed": lambda: s["registry"].write_text("not json"),
        "missing": s["registry"].unlink,
        "boolversion": lambda: write(s["registry"], {"version": True, "authority": "mini", "sources": {"core": s["config"]}}),
        "command": lambda: extra_config("command"),
        "identity": lambda: extra_config("identity"),
    }
    changes[failure]()
    assert observe(s)["disposition"] == "unavailable"


def test_actual_cli_default_registry_in_disposable_home(source, tmp_path):
    home = tmp_path / "hermes-home"
    repair = home / "session-repair"
    repair.mkdir(parents=True)
    (repair / "target-sources.json").write_bytes(source["registry"].read_bytes())
    env = dict(os.environ, HERMES_HOME=str(home))
    run = subprocess.run([sys.executable, "-m", "hermes_cli.kanban_owner_target", "observe", "--selector", REF],
                         env=env, capture_output=True, check=False, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["selector"] == REF and result["disposition"] == "pending"
    run = subprocess.run([sys.executable, "-m", "hermes_cli.kanban_owner_target", "observe", "--selector", REF,
                          "--registry", str(source["registry"])], env=env, capture_output=True, check=False, timeout=10)
    assert run.returncode != 0


@pytest.mark.parametrize("key", ["commit", "core_sha", "full_sha", "serves_sha", "sha"])
def test_explicit_installed_stamp_projection(source, key):
    s = source
    update_config(s)
    s["config"]["stamp_sha_key"] = key
    save_config(s)
    write(s["stamp"], {key: s["old"], "unrelated_sha": s["new"]})
    assert observe(s)["disposition"] == "pending"


@pytest.mark.parametrize("failure", ["oversize", "duplicate", "malformed", "missing", "nonregular"])
def test_queue_source_bounds(source, failure):
    s = source
    def nonregular():
        s["queue"].unlink()
        s["queue"].mkdir()

    changes = {
        "oversize": lambda: s["queue"].write_bytes(b" " * (target.MAX_BYTES + 1)),
        "duplicate": lambda: s["queue"].write_text('{"entries":[],"entries":[]}'),
        "malformed": lambda: s["queue"].write_text("["),
        "nonregular": nonregular,
        "missing": s["queue"].unlink,
    }
    changes[failure]()
    assert observe(s)["disposition"] == "unavailable"


@pytest.mark.parametrize('field,value', [
    ('version', True), ('version', 2), ('repository', 'other/project'), ('pr', True), ('pr', 8),
    ('enqueue_generation', 'old'), ('head', 'a' * 40), ('head', 'short'),
    ('observed_at', True), ('observed_at', 'now'), ('observed_at', 0),
    ('observed_at', float('inf')), ('observed_at', float('nan')),
    ('state', 'unavailable'), ('state', 'merged'), ('extra', 'unknown'),
])
def test_exact_observation_schema_and_identity(source, field, value):
    source['fact'][field] = value
    write(source['observation'], source['fact'])
    assert observe(source)['disposition'] == 'unavailable'


@pytest.mark.parametrize('failure', ['oversize', 'duplicate', 'missing', 'fifo', 'future', 'retouched_stale'])
def test_observation_bounded_and_producer_timestamp_not_file_touch(source, failure):
    s = source
    if failure == 'oversize':
        s['observation'].write_bytes(b' ' * 4097)
    elif failure == 'duplicate':
        s['observation'].write_text('{"version":1,"version":1}')
    elif failure in {'missing', 'fifo'}:
        s['observation'].unlink()
        if failure == 'fifo':
            os.mkfifo(s['observation'])
    else:
        s['fact']['observed_at'] = time.time() + 60 if failure == 'future' else time.time() - 1201
        write(s['observation'], s['fact'])
        s['fresh'].touch()  # A generic fetch marker cannot renew this fact.
    assert observe(s)['disposition'] == 'unavailable'


def test_queue_needs_no_local_per_pr_ref(source):
    git(source['repo'], 'update-ref', '-d', 'refs/pull/7/head')
    source['fresh'].unlink()
    assert observe(source)['disposition'] == 'pending'


def test_legacy_ref_and_freshness_config_not_accepted(source):
    source['config'].pop('repository')
    source['config'].pop('observation_path')
    source['config'].update(repo_path=str(source['repo']), revision_ref='refs/pull/7/head',
                            freshness_path=str(source['fresh']))
    save_config(source)
    assert observe(source)['disposition'] == 'unavailable'


def test_git_output_bound(source):
    with pytest.raises(target.Unavailable, match="output exceeded"):
        target._git(source["repo"], "log", "--format=tformat:" + "x" * 1100)


def test_git_timeout(source, monkeypatch):
    monkeypatch.setattr(target, "GIT_TIMEOUT", 0)
    with pytest.raises(target.Unavailable, match="timeout"):
        target._git(source["repo"], "status", "--porcelain")


def test_registry_scope_and_actual_host_change_target_identity(source, monkeypatch):
    original = observe(source)['target']
    alternative = source['observation'].with_name('another-observation.json')
    write(alternative, source['fact'])
    source['config']['observation_path'] = str(alternative)
    save_config(source)
    new_scope = observe(source)['target']
    assert new_scope['revision'] == original['revision']
    assert new_scope['identity'] != original['identity']
    monkeypatch.setattr(target.socket, 'gethostname', lambda: 'another-source')
    assert observe(source)['target']['identity'] != new_scope['identity']


@pytest.mark.parametrize('adverse', ['candidate','alone','skip','skip_nan'])
def test_real_train_live_hold_semantics(source, adverse):
    s = source
    if adverse == 'candidate':
        s['entry']['hold_candidate'] = {'head':s['new']}
    elif adverse == 'alone':
        s['entry']['hold_noted'] = 'validate failed on train candidate abcdefa with this PR alone on main'
    else:
        write(Path(s['config']['train_path']), {'skip':{s['entry']['id']:time.time()+600 if adverse == 'skip' else float('nan')}})
    write(s['queue'],{'entries':[s['entry']]})
    assert observe(s)['disposition'] in ('failed','unavailable')


def test_expired_skip_and_old_dedupe_note_are_not_live_holds(source):
    s = source
    s['entry']['hold_noted'] = 'old resolved vet hold'
    write(s['queue'],{'entries':[s['entry']]})
    write(Path(s['config']['train_path']), {'skip':{s['entry']['id']:time.time()-1}})
    assert observe(s)['disposition'] == 'pending'
