/**
 * [daveey] A core update the host runs on its own (hermes-sync), as the version pill shows it.
 *
 * Desktop's pill only knew about updates it started itself, so a hub update that drained for 12 minutes
 * and held a user's message showed nothing (hermes-hub 2026-11-19). `GET /api/hermes/update/progress`
 * reports the host's published window; this file maps that payload and derives the pill's suffix,
 * tooltip and bar. Pure: the store polls, the statusbar renders.
 */

import type { BackgroundUpdateCopy } from '@/i18n/types_background_update'

export type BackgroundUpdateState = 'applying' | 'batching' | 'blocked' | 'draining'

/** Wire shape of `GET /api/hermes/update/progress` (absent on older backends: the route 404s). */
export interface UpdateProgressResponse {
  progress: null | {
    state: string
    due_at: number
    state_since: number
    elapsed_s: number
    /** Seconds in the current state (the drain's own clock, which `cap_s` is measured on). Absent on older hosts. */
    state_elapsed_s?: number
    cap_s: null | number
    waiting_on: string[]
  }
}

export interface BackgroundUpdateProgress {
  state: BackgroundUpdateState
  /** Seconds since the update was due, on the host's clock when fetched (negative while batching). */
  elapsedS: number
  /** Seconds in the current state when fetched; the bar measures the drainer's cap on this clock. */
  stateElapsedS: number
  capS: null | number
  waitingOn: string[]
  /** Local `Date.now()` when fetched: elapsed advances from here, so a skewed client clock never matters. */
  fetchedAt: number
}

export interface BackgroundUpdateView {
  /** Pausing work or installing: the pill spins. */
  busy: boolean
  suffix: string
  tooltip: string
  /** `fraction` null = no measurable end (indeterminate sweep); no bar while batching or blocked. */
  bar: null | { fraction: null | number }
}

const STATES: readonly BackgroundUpdateState[] = ['applying', 'batching', 'blocked', 'draining']
const MAX_WAITING_LISTED = 4

export function mapUpdateProgress(
  response: null | undefined | UpdateProgressResponse,
  fetchedAt: number
): BackgroundUpdateProgress | null {
  const raw = response?.progress

  if (!raw || !STATES.includes(raw.state as BackgroundUpdateState) || !Number.isFinite(raw.elapsed_s)) {
    return null
  }

  return {
    state: raw.state as BackgroundUpdateState,
    elapsedS: raw.elapsed_s,
    stateElapsedS: Number.isFinite(raw.state_elapsed_s)
      ? Math.max(0, raw.state_elapsed_s as number)
      : Math.max(0, raw.elapsed_s),
    capS: typeof raw.cap_s === 'number' && raw.cap_s > 0 ? raw.cap_s : null,
    waitingOn: Array.isArray(raw.waiting_on) ? raw.waiting_on.filter(item => typeof item === 'string') : [],
    fetchedAt
  }
}

/** `45s`, `12m`, `1h 5m`. */
export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))

  if (s < 60) {
    return `${s}s`
  }

  const minutes = Math.round(s / 60)

  return minutes < 60 ? `${minutes}m` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

function clock(epochMs: number): string {
  const date = new Date(epochMs)

  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
}

function waitingLine(items: string[], copy: BackgroundUpdateCopy): string {
  if (!items.length) {
    return ''
  }

  const shown = items.slice(0, MAX_WAITING_LISTED).join(', ')
  const more = items.length > MAX_WAITING_LISTED ? ` +${items.length - MAX_WAITING_LISTED}` : ''

  return `\n${copy.waitingOn(shown + more)}`
}

export function describeBackgroundUpdate(
  progress: BackgroundUpdateProgress | null,
  now: number,
  copy: BackgroundUpdateCopy
): BackgroundUpdateView | null {
  if (!progress) {
    return null
  }

  const since = Math.max(0, now - progress.fetchedAt) / 1000
  const elapsed = progress.elapsedS + since

  switch (progress.state) {
    case 'batching': {
      const dueIn = -elapsed

      return dueIn > 0
        ? {
            busy: false,
            suffix: copy.dueIn(formatDuration(dueIn)),
            tooltip: copy.tooltipBatching(clock(now + dueIn * 1000)),
            bar: null
          }
        : null // due: the drainer takes over within a pass and publishes `draining`
    }

    case 'draining': {
      const cap = progress.capS
      const drained = progress.stateElapsedS + since
      const pastCap = cap !== null && drained >= cap

      return {
        busy: true,
        suffix: copy.updating,
        tooltip:
          copy.tooltipDraining(formatDuration(elapsed)) +
          (pastCap ? `\n${copy.tooltipPastCap(formatDuration(cap))}` : '') +
          waitingLine(progress.waitingOn, copy),
        bar: { fraction: cap !== null && !pastCap ? Math.min(1, Math.max(0, drained / cap)) : null }
      }
    }

    case 'applying':
      return {
        busy: true,
        suffix: copy.updating,
        tooltip: copy.tooltipApplying(formatDuration(elapsed)),
        bar: { fraction: null }
      }

    case 'blocked':
      return {
        busy: false,
        suffix: copy.blocked,
        tooltip: copy.tooltipBlocked + waitingLine(progress.waitingOn, copy),
        bar: null
      }
  }
}
