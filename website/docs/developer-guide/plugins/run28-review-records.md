# Ask Eve run28 plugin review records

## Source and approval

Each record below cites frozen run28 `SHA256SUMS`, SHA256
`7d30ad1e318a714a3d20e506a79e9ac28ea9de464f237fb644a2847a422464d0`,
and independent RV3 approval `deleg_8e533dd0`, recorded on
`t_01a1fd2c65f894e3`, comment `rv3approve 237105`.

| Registry key | Frozen source prefix | Approval |
| --- | --- | --- |
| `askeve-github-read` | `app-port/vendor/plugins/askeve-github-read/` | `deleg_8e533dd0`, manifest above |
| `project-work` | `app-port/vendor/plugins/project-work/` | `deleg_8e533dd0`, manifest above |

The archive `app-port-run28-partial-repin-UNTESTED.tar.gz` predates RV3;
its filename is historical, not the later review verdict. Archive SHA256:
`1edf10574d0cee2d09e913f471586d5932ba60daa9cb26f73b82f7353842b773`.
Its canonical evidence location is the task's hub evidence directory.
All 30,314 manifest entries were verified before deriving these maps.
The reviewed candidate identity is `app-93c0d4f54469`.

The strict version-1 registry permits only plugin keys and path/digest pairs,
not annotations. These citations and machine-readable
`tests/fixtures/plugin_reviews/run28/provenance.json` therefore accompany the
maps without changing the verifier or its schema. Every enrolled digest is
computed from frozen bytes and cross-checked against the manifest, never from
an installed plugin.

## Import scope and exclusions

Diagnosis `t_01a2022dc156bb94` (DIAGNOSIS.md SHA256
`d31873a1339a0e1bc752847d11a4b9b375564c646549233c4ca4eb4a771a67b1`)
observed project-work paths under the candidate vendor directory and the
alpha-askeve profile link. Its saved quiet-check listing is truncated: it is
not an exhaustive runtime import census. Frozen `app-port/stage_app.py`
`VENDOR_PLUGINS` and `ingress_pin.py` establish the two directory roots.
The approved source closure is:

- `project-work`: `__init__.py`, `client.py`, `config.py`, `errors.py`,
  `ingress.py`, `native.py`, `runtime.py`, `service.py`, `store.py`, `plugin.yaml`.
  The entry point imports client/config/ingress; config and ingress import
  service, which imports store/native/errors. Runtime is included as executable
  source within the reviewed directory, not claimed as an observed owner import.
- `askeve-github-read`: `__init__.py`, `github_read.py`, `credentials.py`,
  `plugin.yaml`. The entry point imports its helpers; its sandbox gate loads
  sibling project-work. All executable files and both manifests are pinned,
  including files not yet imported, as the enrollment contract requires.

No app-wide root, proof driver, observer relay, or production application
`app-3ce88be428f7` is enrolled. The registry attests plugin **bytes**, not an
application identity or installation path: an identical approved plugin copy
has the same digest authorization, but this grants no approval to the enclosing
application. Core-owned modules remain under core Git custody.

The diagnosis also names h11/socksio/slack_bolt in environment
`de383b6a70774a83bda9b30164a16351`. These are not plugin review records;
core's pre-import managed dependency registration and installer RECORD
verification remain the only applicable path. This enrollment neither labels
those dependencies RV3-audited nor changes their trust class.

## Verification and limits

Inert `.txt` fixture copies preserve the 14 original files byte-for-byte.
`SHA256SUMS.plugins` contains the 14 unchanged plugin rows from the full original
manifest. Tests verify each fixture's membership and digest,
each record's provenance and complete source set, positive `validate_files`,
and rejection after flipping one byte in each of the 14 files. The full manifest
is intentionally not duplicated in Git (even compressed it exceeds the blob
limit); independent review must retrieve the archive, verify the full manifest
hash above, and compare every fixture and record against those original bytes.
No plugin is imported or invoked by those tests, and no service API is called.

This is not proof that a live owner advertises contract-v2. All component
registration must precede imports (including the GitHub-read sibling gate),
managed dependency custody must hold, the boot/disk identity must match, and
verification must meet the unchanged deadline with no unknown entries.
RV3's source review did not load plugins at runtime; the real container/gVisor
and remaining activation gates are not waived. No verification code,
`_verified_sha`, unknown filtering, owner restart or app deployment is changed.
