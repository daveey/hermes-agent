import { describe, expect, it } from 'vitest'

import { $parkedTreePanes, setTreePaneParked, TREE_PARK_SINGLE } from './parked-panes'
import { resetSingleTabsForTests, singleTabMemory } from './single-tab-memory'

function seed() {
  singleTabMemory.parked = [{ groupId: 'main', id: 'session-tile:previous', index: 0 }]
  singleTabMemory.preferred = 'session-tile:preferred'
  setTreePaneParked('session-tile:previous', true, TREE_PARK_SINGLE)
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

  it('dropping Single’s claim leaves a pane parked when another owner still holds it', () => {
    setTreePaneParked('preview-tile:live', true, TREE_PARK_SINGLE)
    setTreePaneParked('preview-tile:live', true, 'mirror:preview-tile')
    setTreePaneParked('preview-tile:live', false, TREE_PARK_SINGLE)
    expect($parkedTreePanes.get().has('preview-tile:live')).toBe(true)
    setTreePaneParked('preview-tile:live', false, 'mirror:preview-tile')
    expect($parkedTreePanes.get().has('preview-tile:live')).toBe(false)
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
