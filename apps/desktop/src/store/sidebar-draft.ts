import { atom } from 'nanostores'

// Main's fallback while a center tile borrows $currentCwd for Files. Captured
// only at draft creation and at that tile boundary, never mirrored by cwd
// writers. Placement and send resolve live main intent via $mainDraftSessionCwd.
export const $mainSidebarDraftCwd = atom<string | null>(null)
