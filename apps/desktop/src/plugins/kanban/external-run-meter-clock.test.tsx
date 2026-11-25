import type * as SDK from '@hermes/plugin-sdk'
import { act, cleanup, render } from '@testing-library/react'
import type { ComponentType } from 'react'
import { afterEach, expect, it, vi } from 'vitest'

import type * as API from './api'
import type * as OpenTask from './open-task'
import plugin from './plugin'

const { board } = vi.hoisted(() => ({
  board: {
    columns: [
      {
        name: 'scheduled',
        tasks: [
          {
            id: 't_clock',
            status: 'scheduled',
            title: 'CI',
            external: { health: 'ok', job: 'running', service: 'GitHub Actions', observed_at: 1000, next_due_at: 1300 }
          }
        ]
      }
    ]
  }
}))

vi.mock('@hermes/plugin-sdk', async importOriginal => ({
  ...(await importOriginal<typeof SDK>()),
  useQuery: () => ({ data: board })
}))
vi.mock('./api', async importOriginal => ({
  ...(await importOriginal<typeof API>()),
  bindApi: () => () => {},
  useKanbanScope: () => 'clock-fixture'
}))
vi.mock('./open-task', async original => ({ ...await original<typeof OpenTask>(), installOpenTaskHook: () => () => {} }))

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

it('the mounted statusbar meter expires external evidence without a board update', () => {
  vi.useFakeTimers()
  vi.setSystemTime(1_000_000)
  let Count: ComponentType | undefined
  const disposers: (() => void)[] = []

  plugin.register({
    i18n: { register() {}, t: (key: string) => key, onLocaleChange() {} },
    onDispose: (dispose: () => void) => disposers.push(dispose),
    storage: { get: (_key: string, fallback: unknown) => fallback, set() {} },
    registerMany: (entries: { id: string; render?: ComponentType }[]) => {
      Count ??= entries.find(entry => entry.id === 'count')?.render

      return () => {}
    }
  } as unknown as Parameters<typeof plugin.register>[0])

  try {
    expect(Count).toBeDefined()
    const { container } = render(Count ? <Count /> : null)

    expect(container.querySelector('[data-segment="external"]')).not.toBeNull()
    act(() => {
      vi.advanceTimersByTime(901_000)
    })
    expect(container.querySelector('[data-segment="externalDown"]')).not.toBeNull()
    expect(container.querySelector('[data-segment="external"]')).toBeNull()
  } finally {
    disposers.forEach(dispose => dispose())
  }
})
