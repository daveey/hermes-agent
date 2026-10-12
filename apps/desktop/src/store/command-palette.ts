import { atom } from 'nanostores'

import { releaseTypingFocus } from '@/components/ui/keyboard-first'

/** Whether the global command palette (Cmd/Ctrl+K) is currently open. */
export const $commandPaletteOpen = atom(false)

/** Optional nested page to open when the palette next opens (e.g. `pets`). */
export const $commandPalettePage = atom<string | null>(null)

/** Optional search text to prefill when the palette next opens (type-to-search
 *  from a surface: the keystroke that opened the palette must not be lost). */
export const $commandPaletteSeed = atom<string | null>(null)

/** Session the `move-to-project` palette page re-homes (set by ⌘⇧P). */
export interface MoveToProjectTarget {
  profile?: string
  sessionId: string
}

export const $moveToProjectTarget = atom<MoveToProjectTarget | null>(null)

/** Open the palette on the project typeahead for one stored session. */
export function openMoveToProjectPalette(target: MoveToProjectTarget): void {
  $moveToProjectTarget.set(target)
  openCommandPalettePage('move-to-project')
}

export function openCommandPalette(): void {
  $commandPaletteOpen.set(true)
}

/** Open the palette directly on a nested page (`theme`, `pets`, …). */
export function openCommandPalettePage(page: string, seed?: string): void {
  $commandPalettePage.set(page)

  if (seed) {
    $commandPaletteSeed.set(seed)
  }

  $commandPaletteOpen.set(true)
}

// Closing hands the keyboard back to the composer — a hotkey-opened overlay
// owes typing focus to whatever the user was writing, not to its trigger.
// Skipped when the palette action itself moved focus (navigating to a route,
// opening a dialog): those surfaces claim focus after this runs.
function setOpen(open: boolean): void {
  const wasOpen = $commandPaletteOpen.get()

  $commandPaletteOpen.set(open)

  if (!open) {
    $commandPalettePage.set(null)
    $commandPaletteSeed.set(null)
    $moveToProjectTarget.set(null)

    if (wasOpen) {
      releaseTypingFocus()
    }
  }
}

export function closeCommandPalette(): void {
  setOpen(false)
}

export function setCommandPaletteOpen(open: boolean): void {
  setOpen(open)
}

export function toggleCommandPalette(): void {
  setOpen(!$commandPaletteOpen.get())
}
