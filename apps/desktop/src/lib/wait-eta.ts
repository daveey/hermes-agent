/** [daveey] The ETA readout of a light-blue wait (CI, deploy, timer): waited / expected, and what is left.
 *  Backend fields: `kanban_wait_info.eta_fields` (`waited_since`, `expected_s`, `estimate_source`); the remaining
 *  time is computed here from the client clock, so it counts down live. */

export type WaitKind = 'ci' | 'clock' | 'deploy'

export interface WaitEta {
  estimate_source?: null | string
  expected_s?: null | number
  wait_kind?: null | string
  waited_since?: null | number
}

export const ORANGE_OVER = '#f0883e'

const KIND_LABEL: Record<WaitKind, string> = { ci: 'CI', clock: 'timer', deploy: 'deploy' }

export const isWaitKind = (value: unknown): value is WaitKind => value === 'ci' || value === 'clock' || value === 'deploy'

/** Largest unit: `30s`, `12m`, `3.4h`, `14h`, `2d`, `1.5d`. Minutes are whole numbers; hours and days take one
 *  decimal below 10 (a trailing .0 dropped) and whole numbers from 10 up. */
export function formatWaitDuration(seconds: number): string {
  const s = Math.max(0, Number.isFinite(seconds) ? seconds : 0)

  for (const [size, unit] of [
    [86400, 'd'],
    [3600, 'h'],
    [60, 'm']
  ] as const) {
    if (s >= size) {
      const value = s / size

      if (unit === 'm' || value >= 10) {
        return `${Math.round(value)}${unit}`
      }

      return `${value.toFixed(1).replace(/\.0$/, '')}${unit}`
    }
  }

  return `${Math.round(s)}s`
}

export interface WaitReadout {
  expected: null | string
  /** Seconds left (negative once over); null without an estimate. */
  leftS: null | number
  /** Time left, `+<over>` once past the estimate, `—` without one. */
  left: string
  over: boolean
  title: string
  unknown: boolean
  waited: string
  waitedS: number
}

/** The row readout for one wait at client time `now` (epoch seconds); null when the card carries no park time. */
export function waitReadout(eta: undefined | WaitEta | null, now: number): null | WaitReadout {
  if (!eta || !isWaitKind(eta.wait_kind) || typeof eta.waited_since !== 'number') {
    return null
  }

  const waitedS = Math.max(0, now - eta.waited_since)
  const waited = formatWaitDuration(waitedS)
  const what = `Waiting on ${KIND_LABEL[eta.wait_kind]}`
  const source = eta.estimate_source?.trim()

  if (typeof eta.expected_s !== 'number') {
    return {
      expected: null,
      left: '—',
      leftS: null,
      over: false,
      title: `${what} · ${waited} so far · no estimate${source ? ` (${source})` : ''}`,
      unknown: true,
      waited,
      waitedS
    }
  }

  const leftS = eta.expected_s - waitedS
  const expected = formatWaitDuration(eta.expected_s)
  const over = leftS < 0
  const left = over ? `+${formatWaitDuration(-leftS)}` : formatWaitDuration(leftS)
  const tail = over ? `${left.slice(1)} over` : `${left} left`

  return {
    expected,
    left,
    leftS,
    over,
    title: `${what} · waited ${waited} · expected ${expected}${source ? ` (${source})` : ''} · ${tail}`,
    unknown: false,
    waited,
    waitedS
  }
}
