# Isolated Desktop probes

A separate Electron `--user-data-dir` is necessary, but not sufficient. Never send
synthetic input to the owner's window or use a real session as a test fixture.

## What leaked

`hermes_constants.get_default_hermes_root()` previously collapsed every descendant
of the native Hermes home back to that home. Thus `HERMES_HOME=~/.hermes/cache/scratch/probe/home`
made `hermes_cli.profiles` enumerate the real default and named profile directories.
The sidebar's `profile=all` route uses `_profile_targets()` → `profiles_to_serve()`;
it consequently read the real databases. Startup multi-profile detection uses the
same root. The root resolver now recognizes only the native home and its immediate
`profiles/<name>` homes (including native symlink aliases), not arbitrary descendants.

Host discovery is a separate boundary. `gateway.host_rendezvous.host_state_dir()`
uses `gateway.status._get_lock_dir()`: `HERMES_GATEWAY_LOCK_DIR`, otherwise
`$XDG_STATE_HOME/hermes/gateway-locks` or `~/.local/state/hermes/gateway-locks`.
Changing HERMES_HOME does NOT change this OS-user-wide namespace. Desktop's
`HERMES_DESKTOP_ISOLATED_BACKEND=1` skips its attach-first ladder; it does not alone
isolate Python profile enumeration or the backend's `desktop-serve` record.
An observe-only warning is not proof of isolation.

## Launch contract

Use a worktree, never rebuild the live checkout. Build the worktree Electron main
process first using the repository's normal build commands. Allocate one fresh,
absolute probe directory and reserve unused renderer/CDP ports. Keep the following
environment for both Electron and its backend; sanitize inherited worker, profile,
gateway, token and connection settings rather than copying the owner's full env:

    HOME=<probe>/oshome
    HERMES_HOME=<probe>/oshome/.hermes
    HERMES_GATEWAY_LOCK_DIR=<probe>/gateway-locks
    XDG_STATE_HOME=<probe>/xdg-state
    HERMES_DESKTOP_ISOLATED_BACKEND=1
    HERMES_DESKTOP_HERMES_ROOT=<absolute-worktree>
    HERMES_DESKTOP_DEV_SERVER=http://127.0.0.1:5175
    HERMES_DESKTOP_CDP_PORT=9333

Create HOME and HERMES_HOME first. Using a separate HOME is defense in depth and
works around older cores' root-resolution bug; use the explicit lock override even
then (an inherited XDG_STATE_HOME can otherwise point back to the real records).
Start Electron from the worktree's `apps/desktop` with explicit
`--remote-debugging-port=9333 --user-data-dir=<probe>/userdata`.
Use a fresh userdata directory, not a copy of real connection settings.
Do not copy real configs, plugins or credentials wholesale.

Worker-launched unpackaged probes are hidden by `agent-test-instance.ts`. Preserve
the worker-launch identity needed by that guard when sanitizing (HERMES_KANBAN_TASK),
and require the `worker-launched test instance` log before proceeding. Never set
HERMES_DESKTOP_AGENT_VISIBLE=1 without an explicit request. Do not use OS-level
keyboard injection; own the CDP target and verify its identity before each action.

## Vite must have its own cache

Worktree `node_modules` may be a symlink into the live checkout. Vite's default
cacheDir therefore writes a shared cache. Override it with an absolute path such as
`<probe>/node_modules/.vite`; the literal `node_modules` path segment is essential:
without it the React/Babel plugin can transform prebundled dependencies and Vite
can stall for minutes. Do not use `<probe>/.vite`.

For example, from `apps/desktop`, use Vite's API with the existing config:

    PROBE_CACHE_DIR=<probe>/node_modules/.vite node --input-type=module -e '
      import { createServer } from "vite";
      const server = await createServer({
        configFile: "./vite.config.ts",
        cacheDir: process.env.PROBE_CACHE_DIR,
        server: { host: "127.0.0.1", port: 5175, strictPort: true }
      });
      await server.listen();
      server.printUrls();
    '

Use the repository's bundled Node. Do not run `dev:renderer` if it deletes dist.

## Fail-closed checks and cleanup

Before any send/openSession, verify the backend process has the exact probe home
and lock directory, profile enumeration contains only homes inside the probe,
and `GET /api/profiles/sessions?profile=all` contains only synthetic probe sessions
(or is empty). Verify no real gateway connections exist in the fresh userdata.
Abort on any foreign profile/session/host-owner record; do not test by sending to it.
These environment settings are application data isolation, not an OS security sandbox.

Afterward stop only the exact Electron, Vite and backend processes this probe
started. Never `hermes serve --stop`, never kill the owner's app to free a port.
Keep logs/screenshots in the probe directory and remove synthetic state when done.

## Regression coverage

`tests/test_scratch_home_isolation.py` covers nested root resolution, native and
profile symlink compatibility, served profiles, sidebar all-profile targets, and
separate rendezvous reads for serve, desktop-serve and gateway roles. It creates
synthetic native/scratch trees and a valid shared record, proves that record is
readable before switching namespaces, then proves the probe cannot see it.
This is backend/helper coverage, not a live Electron UI acceptance test.
