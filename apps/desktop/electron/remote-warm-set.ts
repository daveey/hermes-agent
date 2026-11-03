// The remote warm set and the keep-alive bound to it, wired in one place so the two can never disagree:
// remote-prewarm.ts holds default + the profiles dispatched to recently warm, and ssh-keepalive-hold.ts keeps
// Desktop main's idle-exit-blocking socket only on those scopes (plus the primary and anything used in the last
// ACTIVE_USE_HOLD_MS).
import fs from 'node:fs'

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

  return { holds, listTargets, recent }
}
