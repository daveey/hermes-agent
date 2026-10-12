import { atom } from 'nanostores'

/** Session the global rename dialog (⌘⇧N) edits; null = closed. */
export interface SessionRenameTarget {
  profile?: string
  sessionId: string
}

export const $sessionRenameTarget = atom<SessionRenameTarget | null>(null)

export function openSessionRename(target: SessionRenameTarget): void {
  $sessionRenameTarget.set(target)
}

export function closeSessionRename(): void {
  $sessionRenameTarget.set(null)
}
