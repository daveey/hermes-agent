import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { $displayTimestamps } from '@/store/display-timestamps'

import { stubThreadEnvironment } from '../test-utils'

import { Thread } from '.'

// Timeline timestamps render only when `display.timestamps` is enabled.
$displayTimestamps.set(true)

const timestamp = new Date('2026-05-01T00:00:00.000Z')
stubThreadEnvironment()

function Harness({ text, asyncResult, staleNotice }: { text: string; asyncResult?: string; staleNotice?: boolean }) {
  const message = {
    id: 'system-1',
    role: 'system',
    content: [{ type: 'text', text }],
    createdAt: timestamp,
    metadata: { custom: { timelineTimestamp: timestamp.getTime() / 1000, asyncResult, staleNotice } }
  } as unknown as ThreadMessage

  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messages: [message],
    isRunning: false,
    onNew: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread />
    </AssistantRuntimeProvider>
  )
}

function expectTimestampSeparated(container: HTMLElement, precedingText: string) {
  const row = container.querySelector('[data-role="system"]')
  const stamp = row?.querySelector('[data-slot="timeline-timestamp"]')?.textContent

  expect(stamp).toBeTruthy()
  expect(row?.textContent).toContain(`${precedingText} ${stamp}`)
}

afterEach(cleanup)

describe('background report inline code', () => {
  // The report body is the same `aui-md prose` markdown renderer the
  // assistant turn uses, rendered with no assistant/room slot ancestor. The
  // real stylesheet's cascade must still reach its `<code>`, or Tailwind
  // Typography's fixed near-black ink wins on every dark theme (#107486).
  const stylesheet = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '../../../styles.css'), 'utf8')

  it('themes inline code in an opened report with the chat inline-code tokens', () => {
    const { container, getByRole } = render(
      <>
        <style>{stylesheet}</style>
        <Harness asyncResult="run `discover_models` first" text="1 background agent finished" />
      </>
    )

    fireEvent.click(getByRole('button', { name: '1 background agent finished' }))
    const code = container.querySelector('[data-role="system"] :not(pre) > code')
    expect(code).toBeTruthy()
    const style = getComputedStyle(code as Element)

    expect(style.color).toBe('var(--ui-inline-code-foreground)')
    expect(style.background).toBe('var(--ui-inline-code-background)')
  })
})

describe('background report disclosure', () => {
  it('keeps result bodies out of the transcript until opened and removes them when collapsed', () => {
    const report = '{"blockers":[{"title":"Local-model readiness uses the wrong endpoint"}]}'
    const { container, getByRole } = render(<Harness asyncResult={report} text="2 background agents finished" />)

    expect(container.textContent).not.toContain('blockers')
    expectTimestampSeparated(container, '2 background agents finished')
    const toggle = getByRole('button', { name: '2 background agents finished' })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')

    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(container.textContent).toContain(report)

    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(container.textContent).not.toContain('blockers')
  })
})

describe('system message timestamp text separation', () => {
  it('separates an ordinary system row timestamp in accessible and copied text', () => {
    const { container } = render(<Harness text="Review saved." />)

    expectTimestampSeparated(container, 'Review saved.')
  })

  it('separates a slash-status timestamp in accessible and copied text', () => {
    const { container } = render(<Harness text={'slash:/model\nmodel changed'} />)

    expectTimestampSeparated(container, 'model changed')
  })

  it('separates a steer timestamp in accessible and copied text', () => {
    const { container } = render(<Harness text="steer:rerun tests" />)

    expectTimestampSeparated(container, 'rerun tests')
  })
})

describe('slash output face', () => {
  const body = (container: HTMLElement) => container.querySelector('[data-role="system"] .whitespace-pre-wrap')

  it('keeps column-padded output without a plan gauge in the default face', () => {
    const { container } = render(
      <Harness text={'slash:/usage\nSession Token Usage\nInput tokens:                 13,527'} />
    )

    expect(body(container)?.className).not.toContain('font-mono')
  })

  it('renders a usage-window /usage block (plan gauges) monospace', () => {
    const output =
      'Session Token Usage\nInput tokens:                 13,527\n\nNous Plan\nToday  [||        ]  20% used'
    const { container } = render(<Harness text={`slash:/usage\n${output}`} />)

    expect(body(container)?.className).toContain('font-mono')
  })

  it('centers a single-line status inline after the command', () => {
    const { container } = render(<Harness text={'slash:/model\n  model → x  '} />)
    const row = container.querySelector('[data-role="system"]')

    expect(row?.className).toContain('text-center')
    expect(row?.querySelector('.font-mono')?.textContent).toBe('/model')
    expect(row?.textContent).toContain('·model → x')
  })
})

describe('prefixed system notes', () => {
  it('renders a review note as memory chrome with trimmed label and detail', () => {
    const { container } = render(<Harness text="review: Memory updated :  saved 2 facts " />)
    const row = container.querySelector('[data-role="system"]')

    expect(row?.querySelector('.tool-memory-legendary-title')?.textContent).toBe('Memory updated')
    expect(row?.querySelector('.tool-memory-legendary-meta')?.textContent).toBe('saved 2 facts')
  })

  it('omits the review detail span when there is no detail', () => {
    const { container } = render(<Harness text="review:Skill saved" />)
    const row = container.querySelector('[data-role="system"]')

    expect(row?.querySelector('.tool-memory-legendary-title')?.textContent).toBe('Skill saved')
    expect(row?.querySelector('.tool-memory-legendary-meta')).toBeNull()
  })

  it('renders a steer note with its trimmed text', () => {
    const { container } = render(<Harness text="steer:  rerun tests  " />)
    const row = container.querySelector('[data-role="system"]')

    expect(row?.textContent).toContain('steered·rerun tests')
  })

  it('left-aligns a multiline plain note', () => {
    const { container } = render(<Harness text={'line one\nline two'} />)

    expect(container.querySelector('[data-role="system"]')?.className).toContain('text-left')
  })

  it('renders nothing for an empty system message', () => {
    const { container } = render(<Harness text="" />)

    expect(container.querySelector('[data-role="system"]')).toBeNull()
  })
})

// [daveey] session-outcome: a blocked-card notice whose card has since left `blocked` stays in the transcript,
// dimmed and labelled, so it no longer reads as a live ask.
describe('stale kanban notice', () => {
  it('renders dimmed and labelled "outdated: card moved on"', () => {
    const { container } = render(<Harness staleNotice text="⏸ Kanban t_1 blocked — needs your OK" />)
    const row = container.querySelector('[data-role="system"]')

    expect(row?.hasAttribute('data-stale-notice')).toBe(true)
    expect(row?.className).toContain('opacity-60')
    expect(row?.querySelector('[data-slot="stale-notice-label"]')?.textContent).toBe('outdated: card moved on')
    expect(row?.textContent).toContain('⏸ Kanban t_1 blocked — needs your OK')
  })

  it('a live notice renders as before, without the label', () => {
    const { container } = render(<Harness text="⏸ Kanban t_1 blocked — needs your OK" />)
    const row = container.querySelector('[data-role="system"]')

    expect(row?.hasAttribute('data-stale-notice')).toBe(false)
    expect(row?.querySelector('[data-slot="stale-notice-label"]')).toBeNull()
    expect(row?.className).not.toContain('opacity-60')
  })
})
