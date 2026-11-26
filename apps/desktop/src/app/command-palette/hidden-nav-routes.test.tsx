// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useEffect } from 'react'
import { MemoryRouter, useLocation, useNavigate } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'

import { PALETTE_AREA } from '@/app/command-palette/contrib'
import { contributedRoutes, navigateToWorkspacePage, ROUTES_AREA, SIDEBAR_NAV_AREA } from '@/app/routes'
import { ContribRender } from '@/contrib/react/boundary'
import { registry } from '@/contrib/registry'
import type * as HermesApi from '@/hermes'
import { $commandPaletteOpen } from '@/store/command-palette'

import { CommandPalette } from './index'

vi.mock('@/hermes', async importActual => ({
  ...(await importActual<typeof HermesApi>()),
  getHermesConfigRecord: async () => ({}),
  listAllProfileSessions: async () => ({ sessions: [], total: 0 })
}))

const pluginPages = [
  ['transfer', 'Session Transfer', '/session-transfer'],
  ['kanban', 'Kanban', '/kanban'],
  ['tailscale', 'Tailscale', '/tailscale'],
  ['lens', 'Session Lens', '/session-lens'],
  ['resetwatch', 'Resetwatch', '/resetwatch']
] as const

function PluginFixture() {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  useEffect(
    () =>
      registry.registerMany(
        pluginPages.flatMap(([id, label, path]) => [
          { area: ROUTES_AREA, id: `page-${id}`, data: { path }, render: () => <div>{label} page</div> },
          { area: SIDEBAR_NAV_AREA, id: `nav-${id}`, data: { label, path } },
          {
            area: PALETTE_AREA,
            id: `palette-${id}`,
            data: {
              id: `fixture-${id}`,
              label,
              run: () => navigateToWorkspacePage(navigate, path)
            }
          }
        ])
      ),
    [navigate]
  )

  const route = contributedRoutes().find(route => route.path === pathname)

  return (
    <>
      <output data-testid="path">{pathname}</output>
      {route && <ContribRender render={route.render} />}
    </>
  )
}

afterEach(() => {
  act(() => $commandPaletteOpen.set(false))
  cleanup()
})

it('retains the four built-in and five contributed palette routes independently of sidebar presentation', async () => {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <PluginFixture />
        <CommandPalette />
      </MemoryRouter>
    </QueryClientProvider>
  )

  for (const [id, path] of [
    ['nav-skills', '/capabilities'],
    ['nav-messaging', '/messaging'],
    ['nav-artifacts', '/artifacts'],
    ['nav-cron', '/cron'],
    ...pluginPages.map(([id, , path]) => [`palette-${id}`, path])
  ]) {
    act(() => $commandPaletteOpen.set(true))

    const row = await waitFor(() => {
      const found = document.querySelector(`[data-value$="${id}"]`)
      expect(found).not.toBeNull()

      return found!
    })

    fireEvent.click(row)
    await waitFor(() => expect(screen.getByTestId('path').textContent).toBe(path))
    const plugin = pluginPages.find(([, , pluginPath]) => pluginPath === path)

    if (plugin) {
      expect(screen.getByText(`${plugin[1]} page`)).toBeTruthy()
    }
  }
})
