# K01 actual-companion integration input

`session-tasks.plugin.js.txt` is the **complete, unmodified** source file, not a
synthetic companion or an installed plugin. It is read only by tests as text.

- Repository: https://github.com/daveey/hermes-mods
- Reviewed PR: https://github.com/daveey/hermes-mods/pull/402
- Commit: `ff969f65b26ee67d700f3dc604adee78a34b1060`
- Source path: `desktop-plugins/session-tasks/plugin.js`
- SHA-256: `6ad5b99582d61a48154818361c4471c820e05c8c0f8d9b916ef012cc8c927f61`
- Size: 88346 bytes
- License: MIT (see `LICENSE` in this directory)

The test-only resolver verifies the default bytes before collection. This makes
ordinary `pnpm run check:test:ui` hermetic: no GitHub access, neighboring checkout,
or CI secret is needed. Refresh this input only from an independently reviewed
actual companion commit; retain exact bytes and update this receipt and resolver
identity together. A passing default test qualifies this pinned pair, **not** the
latest companion, a deployed plugin, or installed Desktop.

`HERMES_K01_COMPANION=/absolute/path/to/plugin.js` still selects exactly the supplied
file for explicit replay. Explicit inputs are intentionally not restricted to the
pin (mutation/alternate-source replay remains possible); missing, empty or unreadable
explicit paths fail, never silently fall back. Existing real-loader, SDK, lifecycle,
cryptography and authority assertions are unchanged.

Reacquire exact source bytes with the GitHub contents API at the commit above,
requesting `Accept: application/vnd.github.raw+json`, then verify size and SHA-256.
Do not regenerate, pretty-print, transpile or reduce this input.
