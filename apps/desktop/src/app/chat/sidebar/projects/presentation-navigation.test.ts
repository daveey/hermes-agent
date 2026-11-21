import { describe, expect, it } from 'vitest'

import { makeSessionInfo } from '@/test/session-info'

import { groupProjects } from './presentation-groups'
import { presentationNavigation as nav } from './presentation-navigation'
import type { SidebarProjectTree } from './workspace-groups'

const node = (id: string, label: string, extra: Partial<SidebarProjectTree> = {}): SidebarProjectTree => ({
  id,
  label,
  path: `/code/${label}`,
  repos: [],
  sessionCount: 2,
  ...extra
})

const local = node('local-app', 'app')
const foreign = node('foreign-app', ' APP ', { connectionId: 'studio', isAuto: true })
const other = node('other', 'other')
const groups = groupProjects([local, foreign, other])

describe('presentation navigation (never a placement authority)', () => {
  it('a raw member or primary filter selects every member, retaining unrelated selections', () => {
    expect(nav.expandFilter(['foreign-app', 'other'], groups)).toEqual(['local-app', 'foreign-app', 'other'])
    expect(nav.expandFilter(['local-app'], groups)).toEqual(['local-app', 'foreign-app'])
    expect(nav.expandFilter(['dead'], groups)).toEqual([])
    expect(nav.expandFilter([], groups)).toEqual([])
  })
  it('one checkbox toggles every id without altering an unrelated filter', () => {
    expect(nav.toggleFilter(['other'], groups[0])).toEqual(['other', 'local-app', 'foreign-app'])
    expect(nav.toggleFilter(['other', 'foreign-app'], groups[0])).toEqual(['other'])
  })
  it('a synthetic live member participates in the same navigation group', () => {
    const catalog = nav.catalog(
      [local],
      [makeSessionInfo({ id: 'R', cwd: '/code/app', connection_id: 'studio' })],
      s => s.connection_id || null
    )

    expect(catalog[0].memberIds).toHaveLength(2)
    expect(nav.expandFilter(['local-app'], catalog)).toEqual(catalog[0].memberIds)
  })
  it('retains all entered members even empty, but resolves the visible primary after dismissal', () => {
    expect(nav.shown([local, { ...foreign, sessionCount: 0 }], 'foreign-app', groups)).toHaveLength(2)
    expect(groupProjects([foreign])[0].primary).toBe(foreign)
  })
})
