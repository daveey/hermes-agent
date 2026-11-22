import { atom } from 'nanostores'

// Client-only placement of the sessionless main composer. $currentCwd also
// follows focused tiles, so it cannot identify a background main draft.
export const $mainSidebarDraftCwd = atom<string | null>(null)
