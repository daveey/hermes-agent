# macOS bundle updates

Tagged macOS bundles use `electron-updater`. The bundled and Light stamps
name that owner. Stable and canary are separate application identities; each
updates within its baked channel. Development and bootstrap installs keep
checkout updates.

One-off builds carry `source: commit-build` and have no updater. Their package
identity includes the short commit SHA, so different commit builds can coexist.
Checks and apply requests explain that the developer must provide a new build.
This applies to the desktop and its bundled CLI, not to an unrelated remote
backend the desktop connects to.

## Feed contract

Channel registration lives in R2 at `releases/channels/NAME.json`, not in a
checked-in channel map. Channel bundles resolve that record and its digest-bound
build manifest before selecting an immutable native feed. The admitted request
keeps the application identity stable across builds and records the source commit
separately from the increasing native package version.

Existing tagged clients retain their native feed URLs. The generic
`apps/desktop/update-feed.cjs` adapter computes these legacy paths; it does not
register channels. The builder writes the legacy URL into `app-update.yml`;
`updates.desktop_feed_base_url` can supply an explicit bucket-base URL.

- Stable: `releases/darwin/stable/stable-mac.yml`
- Canary: `releases/darwin/canary/canary-mac.yml`
- Light: the same paths with `light/` between `darwin/` and the channel.
- Artifacts: `releases/tag/TAG/FILENAME`, shared by download links and feeds.
- Channel-build artifacts and native metadata: `releases/channel-builds/BUILD_ID/`.

Retirement is a different operation from a native update: the old channel points
to a qualified official destination, not to a differently named package in its
own native feed. The explicit migration action must preserve user state and
verify destination readiness before removing the preview application. Keeping
the qualified receiver manifest immutable lets an offline preview migrate after
the destination channel has advanced; the destination subsequently owns updates.

The current workflow builds the bundled variant on ARM64 runners only; there is
no Intel (x64) leg. Light has separate client/feed routing but no release matrix
leg in this change.

`python -m scripts.releases.r2 finalize` requires exactly one metadata file,
named `arm64-CHANNEL-mac.yml`. It rejects wrong
versions, variants, architectures, hashes and inconsistent legacy path fields.
Each referenced ZIP/DMG is streamed back and checked against its SHA-512 and
size. Publication checks the live version, conditionally replaces its ETag,
and reads back the resulting feed. Same-tag macOS artifacts cannot be overwritten
with different bytes. Mutable feeds use `Cache-Control: no-store`.
Canary retention protects the artifacts and blockmaps referenced by live feeds.
An unreadable feed prevents pruning.

## Client lifecycle

Checks never download automatically. Apply rechecks the release, downloads it,
and waits for Squirrel.Mac to accept the signed app. Only then does Hermes stop
its app-owned backends and request installation/relaunch. Unrelated quits do
not trigger installation. Downloads and native-verification failures leave
backends running. Concurrent checks cannot replace an apply operation's target.
The existing checkout updater never mutates the sealed app bundle.

## Release environment

The existing `release-signing` environment supplies:

- `CSC_LINK` and `CSC_KEY_PASSWORD`: Developer ID Application signing identity.
- `APPLE_API_KEY_P8`, `APPLE_API_KEY_ID`, `APPLE_API_ISSUER`: notarization.
- `CLOUDFLARE_R2_ACCOUNT_ID`, `CLOUDFLARE_R2_ACCESS_KEY_ID`,
  `CLOUDFLARE_R2_SECRET_ACCESS_KEY`: bucket access secrets.
- `CLOUDFLARE_R2_BUCKET`, `CLOUDFLARE_R2_PUBLIC_URL`: repository/environment vars.

Publishing requires the Apple credentials. The existing after-sign hook owns
notarization, so electron-builder's second notarization path is disabled.
The publish gate verifies the signature, stapled ticket and Gatekeeper assessment.
Recording the build refuses an update ZIP of 1 GiB (1,073,741,824 bytes) or
more: Squirrel.Mac before Electron 44.7, which v0.22.0 ships, reads the whole ZIP
into memory and crashes downloading one that size
([electron/electron#55089](https://github.com/electron/electron/issues/55089)).
The Darwin publish job waits for the arm64 build and its smoke, and serializes channel writes.

## Checkout updater: backend/client skew

Checkout-managed updates are not atomic backend-and-client updates. The backend
checkout can advance while Desktop remains open and its bundle swap is deferred.
The POSIX handoff's `mac_swap` stages and rolls back **the bundle**, not the
already committed backend code. Copy/install failure keeps or restores the old
app and reports an `app-swap` follow-up warning; it does not undo the code update.
Matching current install stamps therefore do not establish update atomicity.

A legacy client without session mutation identity receives HTTP 409
`client_upgrade_required`. The backend must not accept an unfenced edit to hide
the mismatch. Desktop retains a client-install-wide update reminder for that
refusal, including across ordinary notification clearing and renderer/app
relaunch (when localStorage is available). It stores only an acknowledgement bit,
not the failed edit, session identity or error text. The action opens **client**
Updates, even while viewing a remote backend; a Cloud-served webapp offers reload.
Nothing applies or restarts automatically. Explicit dismissal acknowledges the
note; a later refusal restores it. Opening Updates alone does not establish
compatibility or clear the reminder. After updating, retry the refused edit;
this recovery does not replay it automatically.

External `hermes-sync` safeguards are complementary: `desktop_contract_skew`
recognizes changed session-contract files or a recent upgrade refusal, but still
waits for the user to be away and for Desktop turns to finish before swapping.
`desktop_bundle_lag` records lag in `deploy-check.json` and escalates overdue swaps.
Those safeguards do not guarantee compatibility during the deferred interval.
Do not bypass their drain/idle gates or force a busy app restart.

Regression coverage:

- `tests/hermes_cli/test_session_mutation_legacy_client.py` exercises real HTTP
  refusal and verifies legacy archive/rename/delete do not change session state.
- `tests/scripts/desktop_update/test_post_commit_bundle_failure.py` executes the
  production shell swap functions on disposable old/new bundles after a real Git
  contract commit. Copy failure and install-rename failure retain the advanced
  code and complete old bundle, with an actionable warning.
- `apps/desktop/src/store/client-upgrade-recovery.test.ts` exercises persistence,
  reload, explicit acknowledgement, coalescing and client update targeting.

These are controlled mechanism proofs, not a signed macOS installation test and
not evidence for the cause of an earlier incident whose logs are unavailable.

## Verification limits

Helper tests exercise the strategy, native-event ordering, feed validation,
conditional publication, and retention. They are not proof of a signed install
or actual app replacement.

Native macOS packaged-update drivers are part of the existing
[install/update family](https://github.com/NousResearch/hermes-agent/blob/main/tests/install/BUNDLED_UPDATES.md). The stable gate
requires signed-package transitions on both architectures. Each acceptance
claim needs a successful native run for the exact old/new package pair.
Workflow definitions and historical helper results do not establish acceptance
of the current head. See [PM audit status](pm-audit-status.md) for scoped receipts.
