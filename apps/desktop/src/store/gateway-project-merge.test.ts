import { describe, expect, it } from 'vitest'

import type { SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import type { SessionInfo } from '@/hermes'

import { isRemoteProjectId, mergeGatewayProjectTrees, remoteProjectId } from './gateway-project-merge'

const row = (id: string, last_active: number, extra: Partial<SessionInfo> = {}): SessionInfo =>
  ({ id, last_active, started_at: last_active, title: id, ...extra }) as SessionInfo

const project = (over: Partial<SidebarProjectTree> & Pick<SidebarProjectTree, 'id' | 'label'>): SidebarProjectTree => ({
  path: `/Users/x/code/${over.label}`,
  repos: [],
  sessionCount: over.previewSessions?.length ?? 0,
  ...over
})

describe('mergeGatewayProjectTrees', () => {
  it('returns the base tree untouched when no remote answered', () => {
    const base = [project({ id: 'a', label: 'hermes' })]

    expect(mergeGatewayProjectTrees(base, [], 3)).toBe(base)
  })

  it('folds a same-named remote project into the local lane, newest rows first, stamped with their gateway', () => {
    const base = [
      project({
        id: 'local-hermes',
        label: 'hermes',
        previewSessions: [row('l1', 100), row('l2', 50)],
        lastActive: 100
      })
    ]

    const merged = mergeGatewayProjectTrees(
      base,
      [
        {
          connectionId: 'mac-mini',
          projects: [
            project({
              id: 'mini-hermes',
              label: 'Hermes',
              previewSessions: [row('m1', 200), row('m2', 10)],
              lastActive: 200
            })
          ]
        }
      ],
      3
    )

    expect(merged).toHaveLength(1)
    expect(merged[0].id).toBe('local-hermes')
    expect(merged[0].sessionCount).toBe(4)
    expect(merged[0].lastActive).toBe(200)
    expect(merged[0].previewSessions?.map(s => [s.id, s.connection_id ?? null])).toEqual([
      ['m1', 'mac-mini'],
      ['l1', null],
      ['l2', null]
    ])
  })

  it('merges Home with Home regardless of label', () => {
    const merged = mergeGatewayProjectTrees(
      [project({ id: '__home__', label: 'Home', isNoProject: true, path: null, previewSessions: [row('l', 1)] })],
      [
        {
          connectionId: 'nous-1',
          projects: [project({ id: '__home__', label: 'whatever', isNoProject: true, previewSessions: [row('n', 2)] })]
        }
      ],
      3
    )

    expect(merged).toHaveLength(1)
    expect(merged[0].previewSessions?.map(s => s.id)).toEqual(['n', 'l'])
  })

  it('gives a remote-only project its own namespaced lane with no local path', () => {
    const merged = mergeGatewayProjectTrees(
      [project({ id: 'p', label: 'babybear' })],
      [
        {
          connectionId: 'mac-mini',
          projects: [
            project({
              id: 'p',
              label: 'echo-dot',
              previewSessions: [row('m1', 5)],
              repos: [
                {
                  id: 'r',
                  label: 'echo-dot',
                  path: '/x',
                  groups: [{ id: 'g', label: 'main', sessions: [row('m1', 5)] }]
                } as never
              ]
            })
          ]
        }
      ],
      3
    )

    expect(merged.map(p => p.id)).toEqual(['p', remoteProjectId('mac-mini', 'p')])
    expect(isRemoteProjectId(merged[1].id)).toBe(true)
    expect(merged[1].path).toBeNull()
    expect(merged[1].repos[0].id).toBe(remoteProjectId('mac-mini', 'r'))
    expect(merged[1].repos[0].groups[0].sessions[0].connection_id).toBe('mac-mini')
    expect(merged[1].previewSessions?.[0].connection_id).toBe('mac-mini')
  })

  it('skips archived remote projects', () => {
    const merged = mergeGatewayProjectTrees(
      [],
      [{ connectionId: 'mac-mini', projects: [project({ id: 'x', label: 'old', archived: true })] }],
      3
    )

    expect(merged).toEqual([])
  })
})
