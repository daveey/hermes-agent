# Quiet-start approved-source cohort

This is a partial enrollment for `t_01a217a1e20d7b63`, not proof of Mac quiet-start
admission. It enrolls only the 31 independently approved keys out of the original
67 refusals. The other 36 keys remain unapproved here; no refusal is filtered,
limit raised, plugin-review check skipped, or verification deadline changed.

## Source and independent authority

`tests/fixtures/plugin_reviews/quiet31/provenance.json` records each key's source
repository, exact commit, prefix, independent review task, report SHA256, complete
approved file map. Thirty keys come from
`daveey/hermes-mods` commit `eeaa0dc77b6b7d92de316340868b1705521e2d86`.
`error-ledger` comes from `olric3015/hermes-plugins` commit
`732cf2268b43b8d99c385912f9a50af149cad0ee`, prefix `error-ledger/`.

The five original independent source reviews are:

| Batch | Review task | Approved keys |
| --- | --- | --- |
| 01 | `t_01a21e41eb011c61` | 15 |
| 02 | `t_01a21e47a84affec` | 2 |
| 03 | `t_01a21e47c2078cab` | 5 |
| 04 | `t_01a21e48441adb31` | 8 |
| 05 | `t_01a21e4af56a4ebc` | 1 |

The source reports and maps are preserved in the owner's immutable
`run22-review-results-evidence.tar.gz`, SHA256
`687f8dacc6f2f5a59fe92314b2b598b821586974450baa6ad50ae7ce0e9ce142`.
The external source packet is `external-source-candidates-run21.tar.gz`, SHA256
`b264aee40a46e9f0912807905ebaf1945a1f7502c599956c96bc0a47f7ac79f0`.
Both live under the owner's canonical hub evidence directory. Reviewers must
retrieve the original reports/maps and compare all enrolled rows against their
per-key verdicts; fixture hashes alone do not create approval.

All 309 fixture files were derived from those immutable source bytes and
cross-checked against the approved maps, never from an installed plugin. The
fixtures are individually hash-checked base64 JSON files, not importable modules
or profile archives. Encoding keeps source snippets inert to script-text linters;
no source byte is changed and no linter or size gate is exempted. Each decoded
source file is below the ordinary blob-size gate. The
existing two run28 records are unchanged. Registry size is 31,904 bytes and
323 total file records, within the unchanged 1 MiB / 4096-file bounds. The new
source bytes total 2,686,232, with largest file 163,013 bytes; per-plugin
validation remains bounded by the existing 8 MiB/file and 32 MiB/plugin limits.

## Closure qualifications

- `prompt-coach` requires `system-one/client.py`; both keys are enrolled at the
  exact paired reviewed bytes. Their roots still must register before execution.
- `fast-rate-limit-fallback` requires the paired `account-usage/selection.py`
  digest `84c8692078951ea86ac28044a230b3507fcf9761a46025452a882e80a44f95b2`.
- `lanes` also executes `session-panel/model.py`, read by the batch04 review.
  This enrollment does **not** approve the entire session-panel closure: its
  cross-plugin dependencies remain unresolved on the owner. A lanes map does
  not substitute for session-panel root registration or closure approval.
- Managed core imports and installer-receipted dependencies retain their own
  existing custody paths. Desktop halves and external executables are not
  silently granted backend review by this data change.
- Advisory limitations and nonblocking findings in each source review remain
  disclosed, not repaired by enrollment. Future source changes need new review.

## Why the refusals occurred

The registry requirement landed in `e6eeedac34a85ea615198a80a51d1d4a59f47600`;
initial enrollment `7ae39153aed` covered only two different run28 plugins.
These 31 keys had no boot-pinned records. Source hashes or installation metadata
were deliberately not enough. Existing parser/tamper tests correctly refused
missing records; they could not supply independent source approvals for the
user's installed cohort. This change supplies a reviewed subset, not a generic
trust exemption. Missing reviews are not evidence of sync drift or a late latch.

`test_quiet31_review_enrollment.py` checks exact map equality against frozen
fixtures, validates all 31 maps with the production verifier, and changes every
one of the 309 files in turn to prove rejection. No plugin code or database
engine runs. The existing run28, registration, boot-pin and source/code-object
regressions remain applicable. Installed parity, normal updater activation,
remaining closure reviews, residual handler latency and five age-qualified
standard admitted replies are separate outstanding acceptance gates.
