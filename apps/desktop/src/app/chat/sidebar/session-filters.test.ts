import { describe, expect, it } from 'vitest'

import { type SidebarFilterNarrowInput, sidebarFiltersNarrow } from './session-filters'

const none: SidebarFilterNarrowInput = {
  hiddenGatewayIds: [],
  pluginFilters: [],
  prFilter: [],
  profileFilter: [],
  projectFilter: [],
  showAllProfiles: false,
  showAutomated: true,
  statusFilter: []
}

describe('sidebarFiltersNarrow', () => {
  it('does not narrow with nothing set', () => {
    expect(sidebarFiltersNarrow(none)).toBe(false)
  })

  it('narrows when a plugin filter is contributed', () => {
    expect(sidebarFiltersNarrow({ ...none, pluginFilters: [{ id: 'lanes' }] })).toBe(true)
  })

  it('keeps the existing narrowing rules', () => {
    expect(sidebarFiltersNarrow({ ...none, showAutomated: false })).toBe(true)
    expect(sidebarFiltersNarrow({ ...none, statusFilter: ['a'] })).toBe(true)
    expect(sidebarFiltersNarrow({ ...none, profileFilter: ['p'] })).toBe(false)
    expect(sidebarFiltersNarrow({ ...none, profileFilter: ['p'], showAllProfiles: true })).toBe(true)
  })
})
