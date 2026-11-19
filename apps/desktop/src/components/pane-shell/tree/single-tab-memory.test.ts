import { describe, expect, it } from 'vitest'

import { $parkedTreePanes, setTreePaneParked } from './parked-panes'
import { resetSingleTabsForTests, singleTabMemory } from './single-tab-memory'

function seed() {
  singleTabMemory.parked = [{ groupId: 'main', id: 'session-tile:previous', index: 0 }]
  singleTabMemory.preferred = 'session-tile:preferred'
  setTreePaneParked('session-tile:previous', true)
}

describe('Single-tab test reset', () => {
  it('clears both bookkeeping fields and only unparks Single-owned panes', () => {
    seed()
    setTreePaneParked('browser:background', true)

    resetSingleTabsForTests()

    expect(singleTabMemory.parked).toEqual([])
    expect(singleTabMemory.preferred).toBeNull()
    expect($parkedTreePanes.get().has('session-tile:previous')).toBe(false)
    expect($parkedTreePanes.get().has('browser:background')).toBe(true)
    setTreePaneParked('browser:background', false)
  })

  it('is safe to call repeatedly without changing unrelated parking', () => {
    setTreePaneParked('browser:background', true)
    resetSingleTabsForTests()
    resetSingleTabsForTests()
    expect([...$parkedTreePanes.get()]).toEqual(['browser:background'])
    setTreePaneParked('browser:background', false)
  })

  // Deliberately leave state dirty: the global setup hook, not a local hook,
  // must clear it before the next case starts.
  it.each([1, 2])("starts case %i without the previous test's module state", () => {
    expect(singleTabMemory.parked).toEqual([])
    expect(singleTabMemory.preferred).toBeNull()
    expect($parkedTreePanes.get().has('session-tile:previous')).toBe(false)
    seed()
  })
})
