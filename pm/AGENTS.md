# pm/ — dependencies and Hermes environments

Applies on top of the root `AGENTS.md` (which carries the short form of the pinning rule).

## Dependency pinning policy

All dependencies carry upper bounds (litellm compromise #2796/#2810; Mini Shai-Hulud worm,
May 2026). PyPI: `>=floor,<next_major` (`"httpx>=0.28.1,<1"`); pre-1.0: `<0.(minor+2)`
(`>=0.29,<0.32`). Git URLs: 40-char commit SHA. GitHub Actions: SHA + `# vN` comment. CI-only
Python requirements: `==exact`. A bare `>=X.Y.Z` is rejected by CI and reviewers.
After changing `pyproject.toml`, run `hermes pm lock`, re-source `./activate`, and commit
`pyproject.toml` with `uv.lock`. Reference: #2810 (bounds), #9801 (SHA pinning + audit CI).

PM owns Hermes Python dependency changes. Use `pm.sync_venv(['extra'], explicit=True)`
for declared runtime extras, `hermes pm install` for setup/sync, and `hermes pm repair`
for damaged dependencies. Do not mutate Hermes environments with raw pip or uv.
Developer-only tools (`just`) are optional packages in `pm.testenv.DEVELOPER_TOOLS`: the
`--test-environment` install provisions those the lockfile pins; a user install or desktop bundle
never does.
Use `pm.build_environment` for fresh build outputs and `pm.ensure_environment` for
isolated tool environments. Callers receive an interpreter or tool path, not uv.
Nix's declarative uv2nix builds and unrelated user projects remain independently owned.

The `[tool.uv] exclude-newer = "3 days"` release-age check covers **Hermes's own dependencies
only**, exact pins included, with one per-package exemption class: CA trust stores (`certifi`),
whose revocation releases must reach the lock the day they ship (`exclude-newer-package`; nothing
else goes there). It runs when Hermes LOCKS
(`hermes pm lock`, CI's lock check), never on users' machines: every generated workspace drops the
cutoff (`pm/workspace.py::_without_release_age`), so a mirror without upload times cannot break an
install (#135589). Plugin resolution is unchanged by this: core's exact pins stay put, and a plugin
may move a transitive core package within core's declared ranges
(`tests/pm/test_workspace_build_inputs.py::test_plugin_can_move_compatible_transitive_but_not_exact_requirement`).
Plugin `python_dependencies` follow the plugin's own policy. Teknium's rulings: "plugins dont have to abide by our 14 day rule … Only hermes'
dependencies themselves have to"; dates are checked by us, not users; pinned deps need >3 days.
JS: `minimumReleaseAge` (14 days) in `pnpm-workspace.yaml`; an exemption is one exact
`- name@1.2.3  # why` line in `minimumReleaseAgeExclude`, which `hermes pm update --pnpm` names
and later drops (`website/docs/reference/package-management.md` § Release age and package mirrors).

## Homes that borrow the account's state

A home other than the account's `~/.hermes` on the same filesystem (a test, review or mutation
`HERMES_HOME`) hard-links the account's verified tool entries and unchanged source-snapshot files,
and shares its uv cache (`pm/environments.py::account_home_root`). A linked file is ONE inode in
both homes, so **published tool entries and source snapshots are never written in place**: replace
a file by writing a sibling and renaming it over (or remove the tree and stage it again); never
open it for writing, `write_text` or append to a path inside a published entry or a snapshot
package directory. The one sanctioned in-place change is `pm/install.py::_heal_exec_bit`, which
only ADDS execute bits (the digest excludes modes, and the account's copy needs +x as well). Files
a snapshot rewrites (the root `pyproject.toml`, generated members) are real copies, never links.
`tests/pm/test_account_seed.py` fails when a borrower's install, re-pin or snapshot changes the
bytes of an account inode.

`HERMES_PM_ACCOUNT_SEED=0` turns borrowing off. CI sets it (`setup-pm`, whose `actions/cache`
save/restore and `--exact-lock` prune act on the uv cache path), and so should a primary home kept
outside `~/.hermes` beside a stale `~/.hermes`: it would otherwise share that home's uv cache and
compile bytecode on first import instead of eagerly.
