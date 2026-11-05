// The remote warm set and the keep-alive bound to it, wired in one place so the two can never disagree:
// remote-prewarm.ts holds default + the profiles dispatched to recently warm, and ssh-keepalive-hold.ts keeps
// Desktop main's idle-exit-blocking socket only on those scopes (plus the primary and anything used in the last
// ACTIVE_USE_HOLD_MS).
import fs from 'node:fs'

import { backendScopeKey } from './connection-registry'
import { DESKTOP_PROFILE_NAME_RE } from './desktop-profile'
import { prewarmTargetKey, prewarmTargets, RecentRemoteProfiles } from './remote-prewarm'
import { type SshKeepaliveHoldDeps, SshKeepaliveHolds } from './ssh-keepalive-hold'

export type RemoteWarmSetDeps = {
  statePath: () => string
  readRegistry: () => { connections: Array<{ id: string; kind?: string }> }
  keepalives: SshKeepaliveHoldDeps['keepalives']
  isPrimaryScope: (scope: string) => boolean
  log: (line: string) => void
  env?: Record<string, string | undefined>
}

export function createRemoteWarmSet(deps: RemoteWarmSetDeps) {
  const sshConnectionIds = () =>
    deps
      .readRegistry()
      .connections.filter(connection => connection.kind === 'ssh')
      .map(connection => connection.id)

  const recent = new RecentRemoteProfiles({
    load: () => JSON.parse(fs.readFileSync(deps.statePath(), 'utf8')),
    save: data => fs.writeFileSync(deps.statePath(), JSON.stringify(data), { mode: 0o600 })
  })

  const listTargets = () =>
    (deps.env ?? process.env).HERMES_DESKTOP_REMOTE_PREWARM === '0' ? [] : prewarmTargets(sshConnectionIds(), recent)

  const holds = new SshKeepaliveHolds({
    keepalives: deps.keepalives,
    prewarmKeys: () => listTargets().map(prewarmTargetKey),
    isPrimaryScope: deps.isPrimaryScope,
    isSshConnection: id => sshConnectionIds().includes(id),
    log: deps.log
  })

  // The one door that counts as use: it stamps the recent set (prewarm) and the 30-min keep-alive hold together.
  // Only an SSH connection in the registry and a valid profile name count; anything else is a no-op.
  const noteUse = (connectionId: unknown, profile: unknown): boolean => {
    const id = String(connectionId ?? '').trim()
    const name = String(profile ?? '').trim() || 'default'

    if (!id || !DESKTOP_PROFILE_NAME_RE.test(name) || !sshConnectionIds().includes(id)) {
      return false
    }

    recent.touch(id, name)
    holds.noteUse(backendScopeKey(id, name))

    return true
  }

  // ensureRegistryBackend's call for every dial: stamps only an SSH dial made for a user action.
  const noteDial = (connectionId: unknown, profile: unknown, opts: RegistryDialUseOpts = {}): boolean =>
    registryDialCountsAsUse(opts) && noteUse(connectionId, profile)

  return { holds, listTargets, noteDial, noteUse, recent }
}

export type RegistryDialUseOpts = { forceProbe?: boolean; passive?: boolean; use?: boolean }

// Whether an ensureRegistryBackend dial counts as the user using that profile. Only callers acting on a user's
// action set `use` (opening a terminal on it). Every other dial is plumbing: a renderer socket (re)connect, a
// sidebar/roster/session-list read, a media load, a post-resume redial, the prewarm probe. Those once counted, so
// one reconnect sweep over a host's profiles stamped them all into the cap-4 prewarm set and pinned each for
// 30 min. Renderer user actions (open, new chat, send) report use through `hermes:remote:note-use` instead.
export function registryDialCountsAsUse(opts: RegistryDialUseOpts = {}): boolean {
  return opts.use === true && !opts.passive && !opts.forceProbe
}
