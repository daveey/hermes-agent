import { setTreePaneParked, TREE_PARK_SINGLE } from './parked-panes'

export interface ParkedTab {
  groupId: string
  id: string
  index: number
}

// Keep this module free of the registry/settings graph: global test setup imports it
// before individual test files install their mocks.
export const singleTabMemory: { parked: ParkedTab[]; preferred: string | null } = {
  parked: [],
  preferred: null
}

export function resetSingleTabsForTests(): void {
  const entries = singleTabMemory.parked
  singleTabMemory.parked = []
  singleTabMemory.preferred = null
  entries.forEach(entry => setTreePaneParked(entry.id, false, TREE_PARK_SINGLE))
}
