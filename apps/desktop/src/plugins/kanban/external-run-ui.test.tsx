import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ExternalRunBadge } from './external-run-ui'

afterEach(cleanup)

const gh = (over: Record<string, unknown> = {}) => ({
  adapter: 'github_run',
  health: 'ok',
  job: 'running',
  next_due_at: 1300,
  observed_at: 1000,
  reason: "job 'tests', 12 min",
  service: 'GitHub Actions',
  url: 'https://github.com/org/repo/actions/runs/1',
  ...over
})

describe('ExternalRunBadge', () => {
  it('scheduled + running is sky “running externally” with Running-on hover', () => {
    const { container } = render(
      <ExternalRunBadge now={1000} task={{ external: gh(), id: 't_ext1', status: 'scheduled' }} />
    )

    const badge = container.querySelector('[data-slot="kanban-external-badge"]')

    expect(badge?.getAttribute('data-visual')).toBe('running')
    expect(badge?.textContent).toBe('running externally')
    expect(badge?.getAttribute('style')).toContain('rgb(56, 189, 248)')
    expect(badge?.getAttribute('title')).toContain('Running on GitHub Actions')
  })

  it('queued is Waiting on', () => {
    const { container } = render(
      <ExternalRunBadge now={1000} task={{ external: gh({ job: 'queued' }), id: 't_q', status: 'scheduled' }} />
    )

    expect(container.querySelector('[data-slot="kanban-external-badge"]')?.getAttribute('title')).toContain(
      'Waiting on GitHub Actions'
    )
  })

  it('unknown visual stays sky with Status unknown hover', () => {
    const { container } = render(
      <ExternalRunBadge now={1301} task={{ external: gh(), id: 't_u', status: 'scheduled' }} />
    )

    const badge = container.querySelector('[data-slot="kanban-external-badge"]')

    expect(badge?.getAttribute('data-visual')).toBe('unknown')
    expect(badge?.getAttribute('title')).toContain('Status unknown on GitHub Actions')
  })

  it('down is red “external down”', () => {
    const { container } = render(
      <ExternalRunBadge
        now={1000}
        task={{ external: gh({ health: 'unreachable', job: 'unknown' }), id: 't_d', status: 'scheduled' }}
      />
    )

    const badge = container.querySelector('[data-slot="kanban-external-badge"]')

    expect(badge?.getAttribute('data-visual')).toBe('down')
    expect(badge?.textContent).toBe('external down')
    expect(badge?.getAttribute('style')).toContain('239, 68, 68')
    expect(badge?.getAttribute('title')).toMatch(/unreachable/)
  })

  it('missing external on a scheduled card renders nothing (today)', () => {
    const { container } = render(<ExternalRunBadge now={1000} task={{ id: 't_plain', status: 'scheduled' }} />)

    expect(container.querySelector('[data-slot="kanban-external-badge"]')).toBeNull()
  })

  it('a running (not scheduled) card never shows the badge', () => {
    const { container } = render(
      <ExternalRunBadge now={1000} task={{ external: gh(), id: 't_run', status: 'running' }} />
    )

    expect(container.querySelector('[data-slot="kanban-external-badge"]')).toBeNull()
  })
})

describe('ExternalRunBadge fake-timer staleness', () => {
  it('flips running → unknown after next_due_at', () => {
    vi.useFakeTimers()
    vi.setSystemTime(1_000_000)

    try {
      const task = { external: gh(), id: 't_timer', status: 'scheduled' as const }
      const { container, rerender } = render(<ExternalRunBadge now={1000} task={task} />)

      expect(container.querySelector('[data-visual="running"]')).not.toBeNull()
      rerender(<ExternalRunBadge now={1301} task={task} />)
      expect(container.querySelector('[data-visual="unknown"]')).not.toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })
})
