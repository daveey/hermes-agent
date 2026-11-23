import { describe, expect, it } from 'vitest'

import { mergeGatewayProjectTrees } from '@/store/gateway-project-merge'
import { makeSessionInfo } from '@/test/session-info'

import { gatewayProjectId, withGatewayProjectRows } from './gateway-project-rows'
import { groupProjects } from './presentation-groups'
import { NO_PROJECT_ID, overlayLivePreviews, type SidebarProjectTree } from './workspace-groups'

const home: SidebarProjectTree = {
  id: NO_PROJECT_ID,
  isNoProject: true,
  label: 'Home',
  path: null,
  repos: [],
  sessionCount: 1,
  sessionIds: ['local-1'],
  previewSessions: [makeSessionInfo({ id: 'local-1' })]
}

const localApp: SidebarProjectTree = { id: '/code/app', label: 'app', path: '/code/app', repos: [], sessionCount: 0 }

const rows = [
  makeSessionInfo({ id: 'local-1' }),
  makeSessionInfo({ id: 'lab-loose', connection_id: 'homelab' }),
  makeSessionInfo({ id: 'lab-app', connection_id: 'homelab', cwd: '/srv/app', git_repo_root: '/srv/app' }),
  makeSessionInfo({ id: 'studio-app', connection_id: 'studio', cwd: '/work/app/src', git_repo_root: '/work/app' }),
  makeSessionInfo({ id: 'studio-infra', connection_id: 'studio', cwd: '/work/infra' })
]

const foreign = (session: { connection_id?: null | string }) =>
  session.connection_id && session.connection_id !== 'local' ? session.connection_id : null

const label = (id: string) => ({ homelab: 'Homelab', studio: 'Studio' })[id] ?? id

describe('withGatewayProjectRows', () => {
  it.each(['local', 'studio'])(
    'merges live rows into the declared %s owner folder without counting them twice',
    owner => {
      const existing = makeSessionInfo({ id: 'existing', cwd: '/work/app', connection_id: owner })
      const fresh = makeSessionInfo({ id: 'fresh', cwd: '/work/app', connection_id: owner })

      const declared: SidebarProjectTree = {
        id: 'p_app',
        label: 'app',
        path: '/work/app',
        sessionCount: 1,
        sessionIds: [existing.id],
        previewSessions: [existing],
        repos: [
          {
            id: 'repo',
            label: 'app',
            path: '/work/app',
            sessionCount: 1,
            groups: [{ id: 'lane', label: 'app', path: '/work/app', sessions: [existing] }]
          }
        ]
      }

      const merged = mergeGatewayProjectTrees([], [{ connectionId: owner, projects: [declared] }], 10)
      expect(merged[0].path).toBeNull()
      const foreignOwner = (s: { connection_id?: null | string }) => s.connection_id ?? null
      const result = withGatewayProjectRows(merged, [existing, fresh, fresh], foreignOwner)
      expect(result).toHaveLength(1)
      expect(result[0]).toMatchObject({
        id: merged[0].id,
        remoteProject: { connectionId: owner, id: 'p_app' },
        sessionCount: 2,
        sessionIds: ['existing', 'fresh']
      })
      expect(result[0].previewSessions?.map(s => s.id)).toEqual(['existing', 'fresh'])
      expect(withGatewayProjectRows(result, [existing, fresh], foreignOwner)).toEqual(result)
      expect(merged[0].sessionCount).toBe(1)
    }
  )

  it('keeps equal paths and session ids on different owners and profiles distinct', () => {
    const own = makeSessionInfo({ id: 'same', cwd: '/work/app', connection_id: 'studio' })

    const project: SidebarProjectTree = {
      ...localApp,
      path: '/work/app',
      connectionId: 'studio',
      sessionCount: 1,
      sessionIds: ['same'],
      previewSessions: [own]
    }

    const other = makeSessionInfo({ ...own, connection_id: 'homelab' })
    const profile = makeSessionInfo({ ...own, profile: 'other' })
    const tree = withGatewayProjectRows([project], [own, other, profile, other], foreign)
    expect(tree).toHaveLength(2)
    expect(tree.map(n => n.sessionCount)).toEqual([2, 1])
    expect(tree[0].previewSessions?.map(s => s.profile || 'default')).toEqual(['default', 'other'])
  })

  it('uses retained owner row folders when the remote node has no project or repo path', () => {
    const existing = makeSessionInfo({ id: 'existing', connection_id: 'studio', cwd: '/work/app' })
    const fresh = makeSessionInfo({ id: 'fresh', connection_id: 'studio', cwd: '/work/app' })

    const node: SidebarProjectTree = {
      ...localApp,
      path: null,
      repos: [],
      remoteProject: { connectionId: 'studio', id: 'p_app' },
      sessionCount: 1,
      sessionIds: ['existing'],
      previewSessions: [existing]
    }

    const result = withGatewayProjectRows([node], [existing, fresh], foreign)

    expect(result).toHaveLength(1)
    expect(result[0].sessionCount).toBe(2)
    expect(result[0].previewSessions).toEqual([existing, fresh])
  })

  it('does not synthesize rows already claimed in a mixed-owner merged node', () => {
    const remote = makeSessionInfo({ id: 'remote', connection_id: 'studio', cwd: '/work/app' })

    const node = {
      ...localApp,
      sessionCount: 2,
      sessionIds: ['own', 'remote'],
      previewSessions: [makeSessionInfo({ id: 'own' }), remote]
    }

    expect(withGatewayProjectRows([node], [remote], foreign)).toEqual([node])
  })

  it('files every row another gateway owns: folder-less to Home, the rest under their folder on that gateway', () => {
    const tree = withGatewayProjectRows([home, localApp], rows, foreign, label)

    // Same-named projects keep the bare name (the overview groups them); placement is untouched.
    expect(tree.map(node => [node.label, [...(node.sessionIds ?? [])].sort()])).toEqual([
      ['Home', ['lab-loose', 'local-1']],
      ['app', []],
      ['app', ['lab-app']],
      ['app', ['studio-app']],
      ['infra', ['studio-infra']]
    ])
    expect(tree.find(node => node.isNoProject)?.sessionCount).toBe(2)
    const presentation = groupProjects(tree)
    const apps = presentation.filter(group => group.key === 'name:app')
    expect(apps).toHaveLength(1)
    expect(apps[0].members.flatMap(member => member.sessionIds ?? []).sort()).toEqual(['lab-app', 'studio-app'])
    expect(tree.some(node => node.label.includes(' · '))).toBe(false)
    expect(tree.find(node => node.label === 'infra')?.id).toBe(gatewayProjectId('studio', '/work/infra'))
    // Every row the tree was given lands in exactly one project.
    expect(tree.flatMap(node => node.sessionIds ?? []).sort()).toEqual(rows.map(row => row.id).sort())
  })

  it('reaches the overview previews, and leaves the tree untouched with no foreign rows', () => {
    const tree = withGatewayProjectRows([home], rows, foreign, label)
    const previews = overlayLivePreviews(tree, rows, [], 10)

    expect(Object.values(previews).flat().length).toBe(rows.length)
    expect(withGatewayProjectRows([home], [rows[0]], foreign, label)[0]).toBe(home)
  })

  it('adds a Home bucket when the serving gateway has none', () => {
    const tree = withGatewayProjectRows([], [rows[1]], foreign, label)

    expect(tree).toHaveLength(1)
    expect(tree[0]).toMatchObject({ id: NO_PROJECT_ID, isNoProject: true, sessionCount: 1, sessionIds: ['lab-loose'] })
  })
})
