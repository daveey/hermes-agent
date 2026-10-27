/**
 * [daveey] Read-only open of a chat whose gateway is asleep (instant session loads, C1).
 *
 * A sidebar row whose owning gateway is unreachable (`offline_cached`, from the
 * hermes-hub replica) opens from the local transcript cache instead of dialing
 * the dead gateway. The open is display-only: no runtime is bound, and a send
 * explains that the machine is asleep instead of trying to reach it. Sending
 * through the cloud is out of scope.
 */
import { atom } from 'nanostores'

import type { SessionInfo } from '@/hermes'
import { syncedAgoLabel } from '@/lib/synced-ago'

import { readCachedTranscript } from './transcript-tail-cache'

export interface AsleepOpen {
  connectionId: string
  gateway: string
  profile: string
  storedSessionId: string
  syncedAt: null | number
}

export const $asleepOpen = atom<AsleepOpen | null>(null)

const scopeOf = (row: Pick<SessionInfo, 'connection_id' | 'profile'>) => ({
  connectionId: row.connection_id?.trim() || '',
  profile: row.profile?.trim() || 'default'
})

/** Whether a cached row has a transcript on this Mac to open read-only. */
export function hasCachedTranscriptFor(row: Pick<SessionInfo, 'connection_id' | 'id' | 'profile'>): boolean {
  return Boolean(row.connection_id && readCachedTranscript(row.id, scopeOf(row)))
}

/** The user opened an asleep gateway's row: the next resume of it stays read-only. */
export function markAsleepOpen(row: SessionInfo): void {
  const scope = scopeOf(row)

  $asleepOpen.set({
    connectionId: scope.connectionId,
    gateway: row.offline_gateway || scope.connectionId,
    profile: scope.profile,
    storedSessionId: row.id,
    syncedAt: row.offline_synced_at ?? null
  })
}

export function clearAsleepOpen(): void {
  if ($asleepOpen.get()) {
    $asleepOpen.set(null)
  }
}

/** The asleep open for `storedSessionId`, if that is what is on screen. */
export function asleepOpenFor(storedSessionId: null | string | undefined): AsleepOpen | null {
  const open = $asleepOpen.get()

  return open && storedSessionId && open.storedSessionId === storedSessionId ? open : null
}

/** What a send says while the chat's machine is asleep. */
export function asleepSendNotice(open: Pick<AsleepOpen, 'gateway' | 'syncedAt'>): string {
  return `${open.gateway} is asleep, so this chat is read-only. You're reading the copy saved on this Mac (${syncedAgoLabel(open.syncedAt)}). Send again once ${open.gateway} is awake.`
}
