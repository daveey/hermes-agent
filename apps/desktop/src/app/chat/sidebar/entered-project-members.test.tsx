import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { SidebarProvider } from '@/components/ui/sidebar'
import { fetchProjectSessions } from '@/store/projects'
import type * as ProjectsStore from '@/store/projects'
import { makeSessionInfo } from '@/test/session-info'

import { type EnteredMemberOptions, EnteredProjectMembers, useEnteredMember } from './entered-project-members'
import type { enteredProjectLiveRows } from './projects/gateway-project-rows'
import { withGatewayProjectRows } from './projects/gateway-project-rows'
import type * as Model from './projects/model'
import { groupProjects, presentationGroupOf } from './projects/presentation-groups'
import { projectOwnerBySessionId, type SidebarProjectTree } from './projects/workspace-groups'

vi.mock('@/store/projects', async importOriginal => ({
  ...(await importOriginal<typeof ProjectsStore>()),
  fetchProjectSessions: vi.fn()
}))
vi.mock('./projects/model', async importOriginal => ({
  ...(await importOriginal<typeof Model>()),
  useRepoWorktreeMap: vi.fn(() => [{}]),
  useWorkspaceNodeOpen: () => [true, vi.fn()]
}))
vi.mock('./projects/presentation-group-row', () => ({
  MachineHeading: ({ owner }: { owner: string | null }) => <span>{owner || 'This device'}</span>
}))

const local = makeSessionInfo({
  id: 'local',
  title: 'LOCAL',
  cwd: '/code/app',
  git_repo_root: '/code/app'
})

const studio = makeSessionInfo({
  id: 'studio',
  title: 'STUDIO',
  connection_id: 'studio',
  profile: 'research',
  cwd: '/code/app',
  git_repo_root: '/code/app'
})

const base: SidebarProjectTree = {
  id: '/code/app',
  label: 'app',
  path: '/code/app',
  repos: [
    {
      id: '/code/app',
      path: '/code/app',
      label: 'app',
      groups: [
        {
          id: 'main',
          label: 'main',
          path: '/code/app',
          isMain: true,
          sessions: [local]
        }
      ],
      sessionCount: 1
    }
  ],
  sessionCount: 1,
  sessionIds: [local.id],
  previewSessions: [local]
}

const owners: Parameters<typeof enteredProjectLiveRows>[2] = {
  foreignOwner: row => (row.connection_id === 'studio' ? 'studio' : null),
  ownerKey: row => JSON.stringify([row.connection_id || 'local', row.profile || 'default'])
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})
it('entering either member resolves primary and renders independent same-path base pipelines once', async () => {
  const tree = withGatewayProjectRows([base], [local, studio], owners.foreignOwner, id => id)
  const groups = groupProjects(tree)
  expect(presentationGroupOf(groups, tree[1].id)?.primary).toBe(base)

  const options: EnteredMemberOptions = {
    gatewayReady: true,
    projectTree: tree,
    scope: 'local:default',
    grouped: true,
    orderRepos: repos => repos,
    isHiddenFromProjects: () => false,
    agentSessions: [local, studio],
    owners,
    removedSessionIds: new Set(),
    projectOwners: projectOwnerBySessionId(tree),
    projects: [],
    knownProjects: { ids: new Set(), roots: [] },
    ownerKey: owners.ownerKey,
    showAllProfiles: false
  }

  vi.mocked(fetchProjectSessions).mockImplementation(async id => tree.find(node => node.id === id) ?? null)

  function Body() {
    const primary = useEnteredMember(tree[0], options)

    return (
      <EnteredProjectMembers
        members={tree}
        options={options}
        primary={primary}
        renderRows={rows => rows.map(row => <span key={row.id}>{row.title}</span>)}
      />
    )
  }

  render(
    <SidebarProvider>
      <Body />
    </SidebarProvider>
  )
  await waitFor(() => expect(fetchProjectSessions).toHaveBeenCalledTimes(2))
  expect(screen.getAllByText('LOCAL')).toHaveLength(1)
  expect(screen.getAllByText('STUDIO')).toHaveLength(1)
  expect(screen.getByText('This device')).toBeTruthy()
  expect(screen.getByText('studio')).toBeTruthy()
  expect(document.querySelectorAll('[data-entered-project-member]')).toHaveLength(2)
  expect(fetchProjectSessions).toHaveBeenCalledWith(tree[0].id, {
    supersedable: false
  })
  expect(fetchProjectSessions).toHaveBeenCalledWith(tree[1].id, {
    supersedable: false
  })
})
