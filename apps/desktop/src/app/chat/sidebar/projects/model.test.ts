import { describe, expect, it } from 'vitest'

import { orderProjectsByIds, sortProjectsForOverview } from './model'
import { NO_PROJECT_ID, type SidebarProjectTree } from './workspace-groups'

function makeProject(id: string, sessionCount: number): SidebarProjectTree {
  return {
    id,
    isAuto: true,
    label: id,
    lastActive: 0,
    path: `/repos/${id}`,
    previewSessions: [],
    repos: [],
    sessionCount
  }
}

const home = (): SidebarProjectTree => ({
  ...makeProject(NO_PROJECT_ID, 2),
  isAuto: false,
  isNoProject: true,
  path: null
})

const ids = (projects: SidebarProjectTree[]) => projects.map(project => project.id)

describe('orderProjectsByIds', () => {
  it('leaves the deterministic sort alone when nothing has been dragged', () => {
    const projects = [makeProject('a', 0), makeProject('b', 2)]

    expect(orderProjectsByIds(projects, [])).toBe(projects)
  })

  it('applies the saved manual order', () => {
    const projects = [makeProject('a', 1), makeProject('b', 1), makeProject('c', 1)]

    expect(ids(orderProjectsByIds(projects, ['c', 'a', 'b']))).toEqual(['c', 'a', 'b'])
  })

  it('keeps freshly-scanned zero-session repos below the hand-ordered list', () => {
    // The regression: a disk scan keeps finding git checkouts the user has
    // never opened in Hermes. Surfacing every unsaved id at the top buried the
    // projects they deliberately dragged into place.
    const projects = [makeProject('scanned-1', 0), makeProject('mine', 4), makeProject('scanned-2', 0)]

    expect(ids(orderProjectsByIds(projects, ['mine']))).toEqual(['mine', 'scanned-1', 'scanned-2'])
  })

  it('still surfaces a new project that has real activity', () => {
    // A project you just started working in should not sink beneath the saved
    // order — only the zero-session discoveries do.
    const projects = [makeProject('ordered', 1), makeProject('just-started', 3)]

    expect(ids(orderProjectsByIds(projects, ['ordered']))).toEqual(['just-started', 'ordered'])
  })

  it('drops ids that are no longer present', () => {
    const projects = [makeProject('a', 1)]

    expect(ids(orderProjectsByIds(projects, ['gone', 'a']))).toEqual(['a'])
  })

  it('keeps Home on top of a hand-picked order', () => {
    const projects = [makeProject('a', 1), home(), makeProject('b', 1)]

    expect(ids(orderProjectsByIds(projects, ['b', 'a']))).toEqual([NO_PROJECT_ID, 'b', 'a'])
  })
})

describe('sortProjectsForOverview', () => {
  it('puts Home above the active project', () => {
    const active = { ...makeProject('active', 5), isAuto: false }
    const projects = [makeProject('scanned', 0), active, home()]

    expect(ids(sortProjectsForOverview(projects, 'active'))).toEqual([NO_PROJECT_ID, 'active', 'scanned'])
  })
})

describe('stable project order (created sort)', () => {
  const named = (id: string, lastActive: number, isAuto = true): SidebarProjectTree => ({
    ...makeProject(id, 1),
    isAuto,
    lastActive
  })

  it('does not hoist the active project or follow activity', () => {
    const projects = [named('zeta', 900), named('alpha', 100), named('mid', 500)]

    expect(ids(sortProjectsForOverview(projects, 'zeta', { stable: true }))).toEqual(['alpha', 'mid', 'zeta'])
  })

  it('keeps the same order when activity changes', () => {
    const before = [named('b', 100), named('a', 200), named('c', 300)]
    const after = [named('b', 999), named('a', 200), named('c', 1)]

    expect(ids(sortProjectsForOverview(after, 'b', { stable: true }))).toEqual(
      ids(sortProjectsForOverview(before, 'a', { stable: true }))
    )
  })

  it('keeps explicit projects above auto repos and Home on top', () => {
    const projects = [named('auto-a', 5), named('mine', 1, false), home()]

    expect(ids(sortProjectsForOverview(projects, null, { stable: true }))).toEqual([NO_PROJECT_ID, 'mine', 'auto-a'])
  })

  it('keeps the default sort activity-led', () => {
    const projects = [named('old', 100), named('new', 900)]

    expect(ids(sortProjectsForOverview(projects, null))).toEqual(['new', 'old'])
  })

  it('appends a new active project below the hand-picked order instead of above it', () => {
    const projects = [makeProject('ordered', 1), makeProject('just-started', 3)]

    expect(ids(orderProjectsByIds(projects, ['ordered'], { stable: true }))).toEqual(['ordered', 'just-started'])
  })

  it('sorts by name alone under `alphabetical`, Home first, ignoring activity, hoist and kind', () => {
    const projects = [
      { ...named('zeta', 900), label: 'Zeta' },
      { ...named('beta10', 1), label: 'beta10' },
      named('mine', 5, false),
      { ...named('beta2', 50), label: 'Beta2' },
      home()
    ]

    expect(ids(sortProjectsForOverview(projects, 'zeta', { alphabetical: true }))).toEqual([
      NO_PROJECT_ID,
      'beta2',
      'beta10',
      'mine',
      'zeta'
    ])
  })

  it('ignores a saved drag order under `alphabetical`', () => {
    const projects = [makeProject('a', 1), makeProject('b', 1)]

    expect(orderProjectsByIds(projects, ['b', 'a'], { alphabetical: true })).toBe(projects)
  })
})
