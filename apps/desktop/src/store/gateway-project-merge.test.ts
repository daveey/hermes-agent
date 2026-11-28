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
  // [daveey] With a remote gateway active, This device's tree arrives as a
  // remote tree. A bare row there would resume on the ACTIVE remote (ghost row
  // on nous-1), so `local` is stamped explicitly in a remote tree.
  it("stamps This device's rows `local` when they arrive as another gateway's tree", () => {
    const merged = mergeGatewayProjectTrees(
      [project({ id: 'nous-home', label: 'Home', previewSessions: [row('n1', 10)] })],
      [
        {
          connectionId: 'local',
          projects: [project({ id: 'mbp-taxes', label: 'taxes', previewSessions: [row('t1', 5)] })]
        }
      ],
      3
    )

    const taxes = merged.find(node => node.label === 'taxes')

    expect(taxes?.previewSessions?.map(s => [s.id, s.connection_id])).toEqual([['t1', 'local']])
  })

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

  it('drops a remote copy of a session the active gateway already places (no duplicate row)', () => {
    // nous-1 also reports a MacBook session; with no /Users/... folder there it
    // lands in nous-1's Home, while the MacBook files it under its project.
    const local = row('s', 100, { cwd: '/Users/x/code/watch', kanban_active: 1 })

    const merged = mergeGatewayProjectTrees(
      [
        project({ id: '__home__', label: 'Home', isNoProject: true, path: null, previewSessions: [row('l', 1)] }),
        project({ id: 'p_watch', label: 'watch', previewSessions: [local], sessionIds: ['s'] })
      ],
      [
        {
          connectionId: 'nous-1',
          projects: [
            project({
              id: '__home__',
              label: 'Home',
              isNoProject: true,
              previewSessions: [row('s', 100, { cwd: '/Users/x/code/watch' }), row('n', 2)],
              sessionIds: ['s', 'n'],
              sessionCount: 2,
              repos: [
                {
                  id: '__home__',
                  label: 'Home',
                  path: null,
                  groups: [{ id: '__home__', label: 'Home', sessions: [row('s', 100), row('n', 2)] }]
                } as never
              ]
            })
          ]
        }
      ],
      3
    )

    const home = merged.find(p => p.isNoProject)!

    expect(home.previewSessions?.map(s => s.id)).toEqual(['n', 'l'])
    expect(home.sessionIds).toEqual(['n'])
    // Local 'l' + remote 'n'; the remote's copy of 's' no longer counts.
    expect(home.sessionCount).toBe(2)
    expect(merged.find(p => p.id === 'p_watch')?.previewSessions).toEqual([local])
  })

  it('drops a remote-only project whose every row the active gateway already has', () => {
    const merged = mergeGatewayProjectTrees(
      [project({ id: 'p', label: 'babybear', previewSessions: [row('s', 5)], sessionIds: ['s'] })],
      [
        {
          connectionId: 'nous-1',
          projects: [project({ id: 'q', label: 'mirror', previewSessions: [row('s', 5)], sessionIds: ['s'] })]
        }
      ],
      3
    )

    expect(merged.map(p => p.id)).toEqual(['p'])
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
