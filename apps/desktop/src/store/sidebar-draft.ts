import { atom } from 'nanostores'

// Display-only main workspace snapshot; never consumed by the send path.
export const $mainSidebarDraftCwd = atom<string | null>(null)
export const $sidebarBorrowedTileCwd = atom<string | null>(null)
export const $sidebarBorrowedTileId = atom<string | null>(null)
