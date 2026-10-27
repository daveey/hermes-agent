// What a Desktop SSH teardown does to the remote `serve --isolated` backend it owns.
//
// A normal disconnect (quit, a broken tunnel being rebuilt, revalidation, resume from sleep, the idle pool stop)
// RELEASES the backend: the tunnel and SSH transport close, the backend and its lockfile stay, and the next connect
// reuses it with the token Desktop persisted when it first adopted it. The backend's own idle watchdog
// (hermes_cli/web_server_idle_exit.py: exit 900 s after the last client, never mid-turn) bounds how long a released
// backend lives, so releasing does not reopen the #91668 leak.
//
// A deliberate change (different connection settings, a removed or switched connection, an explicit recycle)
// TERMINATES it, as before. Any reason not listed here terminates, so a new caller errs toward the old behavior.
// Windows remotes use their own lifecycle and are skipped, as before.

export type SshTeardownReason =
  | 'quit'
  | 'bootstrap-retry'
  | 'revalidate'
  | 'resume'
  | 'idle-stop'
  | 'fingerprint-change'
  | 'connection-removed'
  | 'recycle'
  | 'config-apply'
  | 'profile-teardown'

export type RemoteTeardownAction = 'release' | 'terminate' | 'skip-windows'

const RELEASE_REASONS: ReadonlySet<SshTeardownReason> = new Set([
  'quit',
  'bootstrap-retry',
  'revalidate',
  'resume',
  'idle-stop'
])

export function remoteTeardownAction(reason: SshTeardownReason, remoteOs: string | undefined): RemoteTeardownAction {
  if (remoteOs === 'Windows') {
    return 'skip-windows'
  }

  return RELEASE_REASONS.has(reason) ? 'release' : 'terminate'
}

// The cleanup a teardown runs against the remote, for teardownSshState's `cleanupRemote`. Windows remotes use
// connectWindowsRemote, which shares no POSIX lock/kill; the log keeps quit from being a mysterious no-op there.
export function remoteCleanupFor(
  action: RemoteTeardownAction,
  lifecycle: {
    release: (ssh, ownershipId, log: (line: string) => void) => Promise<void>
    disconnect: (ssh, ownershipId) => Promise<void>
  },
  log: (line: string) => void
): (ssh, ownershipId) => Promise<void> {
  return {
    'skip-windows': async () =>
      log('[ssh] skip remote serve teardown on Windows remotes; POSIX disconnect does not apply'),
    release: (ssh, ownershipId) => lifecycle.release(ssh, ownershipId, log),
    terminate: lifecycle.disconnect
  }[action]
}

// Why each pool key is being stopped, carried from the caller to the pool stopper's afterStop hook. Quit, the
// update handoff, idle retirement, resume and revalidation record a reason; uninstall and other stops record none,
// so take() falls back to 'connection-removed' and the remote backend is terminated as before W0.
export function createTeardownReasons() {
  const pending = new Map<string, SshTeardownReason>()

  return {
    note(key: string, reason?: SshTeardownReason) {
      if (reason) {
        pending.set(key, reason)
      }
    },
    noteAll(keys: Iterable<string>, reason?: SshTeardownReason) {
      for (const key of keys) {
        this.note(key, reason)
      }
    },
    take(key: string): SshTeardownReason {
      const reason = pending.get(key) ?? 'connection-removed'
      pending.delete(key)

      return reason
    }
  }
}
