import { Codecs, persistentAtom } from '@/lib/persisted'

import { $sessions, lineageAliases } from './session'

/**
 * [daveey] Sessions the user marked "blocked" with ⌘B — parked on something
 * outside the chat (a review, a person, a long job). The row's status stripe
 * turns grey and ⌘] / ⌘[ skip it. A manual flag: only ⌘B (again) clears it.
 *
 * Local to this window's storage. Compression rotates a conversation's stored
 * id, so membership is checked across the whole lineage.
 */
export const $blockedSessionIds = persistentAtom<string[]>(
  'hermes.desktop.blockedSessionIds',
  [],
  Codecs.stringArray
)

type LineageRows = Parameters<typeof lineageAliases>[1]

export function isSessionBlocked(
  id: string,
  blocked: readonly string[] = $blockedSessionIds.get(),
  sessions: LineageRows = $sessions.get()
): boolean {
  if (blocked.length === 0) {
    return false
  }

  return lineageAliases(id, sessions).some(alias => blocked.includes(alias))
}

/** Flip the flag; returns the new state. Unblocking clears every alias. */
export function toggleSessionBlocked(id: string): boolean {
  const sessions = $sessions.get()
  const blocked = $blockedSessionIds.get()

  if (isSessionBlocked(id, blocked, sessions)) {
    const aliases = new Set(lineageAliases(id, sessions))
    $blockedSessionIds.set(blocked.filter(entry => !aliases.has(entry)))

    return false
  }

  $blockedSessionIds.set([...blocked, id])

  return true
}
