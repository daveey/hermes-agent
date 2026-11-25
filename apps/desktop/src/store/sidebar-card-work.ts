import { executionIdentityKey, selectExecutionObservation } from '@/lib/execution-observations'
import type { SessionInfo } from '@/types/hermes'

/** Display-only card evidence. Never changes execution truth, asks, or persisted outcomes. */
export function sidebarCardWork(session: Pick<SessionInfo, 'execution_observations'>, now = Date.now() / 1000) {
  const values = Array.isArray(session.execution_observations) ? session.execution_observations : []
  const identities = new Map<string, unknown>()

  for (const value of values) {
    if (!value || typeof value !== 'object' || !('identity' in value)) {
      continue
    }

    const identity = value.identity
    const key = executionIdentityKey(identity)

    if (key && identity && typeof identity === 'object' && 'kind' in identity && identity.kind === 'card') {
      identities.set(key, identity)
    }
  }

  let queued = 0
  let quiet = 0

  for (const identity of identities.values()) {
    const evidence = selectExecutionObservation(values, identity, now)

    if (evidence.state === 'queued') {
      queued++
    }

    // Same 2h activity boundary as PanelModel. A stale/missing source is unknown, not stuck.
    const at = evidence.last_activity

    if (evidence.state === 'running' && typeof at === 'number' && Number.isFinite(at) && at > 0 && now - at >= 7200) {
      quiet++
    }
  }

  return { queued, quiet }
}
