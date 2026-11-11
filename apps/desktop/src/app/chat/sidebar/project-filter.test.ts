import { describe, expect, it } from 'vitest'

import {
  filterToNonEmptyProjects,
  filterToSessionBearingProjects,
  projectReorderHandler,
  resolveLiveProjectFilter,
  sidebarProjectOrder,
  sidebarProjectsShown
} from './project-filter'

describe('resolveLiveProjectFilter', () => {
  const treeA = [{ id: 'p_aaa' }, { id: '/Users/me/repos/alpha' }] as const
  const treeB = [{ id: 'p_bbb' }, { id: '/Users/me/repos/beta' }] as const

  it('returns the filter unchanged when it is empty', () => {
    expect(resolveLiveProjectFilter([], treeA)).toEqual([])
  })

  it('keeps ids the active tree resolves', () => {
    expect(resolveLiveProjectFilter(['p_aaa', '/Users/me/repos/alpha'], treeA)).toEqual([
      'p_aaa',
      '/Users/me/repos/alpha'
    ])
  })

  it('drops ids that only resolve in another profile (#96246)', () => {
    // Persisted in profile B, active profile is A: nothing resolves, the
    // filter must become inert instead of filtering everything out.
    expect(resolveLiveProjectFilter(['p_bbb', '/Users/me/repos/beta'], treeA)).toEqual([])
  })

  it('keeps the live half of a mixed filter', () => {
    expect(resolveLiveProjectFilter(['p_bbb', 'p_aaa'], treeA)).toEqual(['p_aaa'])
  })

  it('fails open while the tree is loading', () => {
    // Empty/pending tree → no narrowing yet; re-hydrates when the tree lands.
    expect(resolveLiveProjectFilter(['p_aaa'], [])).toEqual([])
    expect(resolveLiveProjectFilter(['p_aaa'], null)).toEqual([])
    expect(resolveLiveProjectFilter(['p_aaa'], undefined)).toEqual([])
  })
})

describe('filterToSessionBearingProjects (#53004)', () => {
  const base = { label: 'repo', path: '/tmp/repo', repos: [] } as const

  const tree = [
    { ...base, id: 'auto_empty', isAuto: true, sessionCount: 0 },
    { ...base, id: 'auto_active', isAuto: true, sessionCount: 2 },
    { ...base, id: 'explicit_empty', sessionCount: 0 },
    { ...base, id: 'home', isNoProject: true, sessionCount: 0 }
  ]

  it('drops auto projects with no sessions but keeps active autos', () => {
    const ids = filterToSessionBearingProjects(tree).map(project => project.id)

    expect(ids).not.toContain('auto_empty')
    expect(ids).toContain('auto_active')
  })

  it('never drops explicit projects or the Home bucket', () => {
    const ids = filterToSessionBearingProjects(tree).map(project => project.id)

    expect(ids).toContain('explicit_empty')
    expect(ids).toContain('home')
  })
})

describe('filterToNonEmptyProjects', () => {
  const projects = [
    { id: 'home', isNoProject: true, sessionCount: 0 },
    { id: 'explicit_empty', isAuto: false, sessionCount: 0 },
    { id: 'auto_empty', isAuto: true, sessionCount: 0 },
    { id: 'unknown_count' },
    { id: 'busy', sessionCount: 3 }
  ]

  it('drops every empty project, explicit ones included, but keeps Home', () => {
    expect(filterToNonEmptyProjects(projects).map(p => p.id)).toEqual(['home', 'busy'])
  })

  it('keeps the entered project even when it is empty', () => {
    expect(filterToNonEmptyProjects(projects, 'explicit_empty').map(p => p.id)).toEqual([
      'home',
      'explicit_empty',
      'busy'
    ])
  })
})

describe('sidebar project policy (alphabetical, no empties)', () => {
  it('lists only non-empty projects plus Home and the entered one', () => {
    const projects = [
      { id: 'home', isNoProject: true, sessionCount: 0 },
      { id: 'explicit_empty', isAuto: false, sessionCount: 0 },
      { id: 'busy', isAuto: true, sessionCount: 2 }
    ]

    expect(sidebarProjectsShown(projects).map(p => p.id)).toEqual(['home', 'busy'])
    expect(sidebarProjectsShown(projects, 'explicit_empty').map(p => p.id)).toEqual(['home', 'explicit_empty', 'busy'])
  })

  it('keeps an entered empty AUTO project too', () => {
    const projects = [
      { id: 'auto_empty', isAuto: true, sessionCount: 0 },
      { id: 'busy', isAuto: true, sessionCount: 2 }
    ]

    expect(sidebarProjectsShown(projects).map(p => p.id)).toEqual(['busy'])
    expect(sidebarProjectsShown(projects, 'auto_empty').map(p => p.id)).toEqual(['auto_empty', 'busy'])
  })

  it('sorts alphabetically and drops the drag handle', () => {
    const handler = () => undefined

    expect(sidebarProjectOrder('updated')).toEqual({ alphabetical: true, stable: false })
    expect(sidebarProjectOrder('created')).toEqual({ alphabetical: true, stable: true })
    expect(projectReorderHandler(false, handler)).toBeUndefined()
  })
})
