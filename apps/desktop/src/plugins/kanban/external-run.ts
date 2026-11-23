/** [daveey] External-run cards (CI / deploy / external parks): visual state, hover copy, session counts.
 *  Field names match `interface.md` + `interface-hover.md`. Absent fields → no claim (today's output). */

export type ExternalJob = 'cancelled' | 'failed' | 'queued' | 'running' | 'succeeded' | 'unknown'

export type ExternalHealth =
  | 'auth_failed'
  | 'degraded'
  | 'down'
  | 'host_silent'
  | 'ok'
  | 'probe_broken'
  | 'rate_limited'
  | 'unreachable'
  | 'watch_missing'

export type ExternalAdapter = 'github_pr_checks' | 'github_run' | 'shell'

export interface ExternalInfo {
  adapter?: ExternalAdapter | string
  health: ExternalHealth | string
  job: ExternalJob | string
  next_due_at?: null | number
  observed_at?: null | number
  reason?: string
  service: string
  url?: null | string
}

export interface ExternalCard {
  /** ETA readout of a CI / deploy / timer wait (`kanban_wait_info.eta_fields`). */
  eta?: {
    estimate_source?: null | string
    expected_s?: null | number
    wait_kind?: null | string
    waited_since?: null | number
  } | null
  external?: ExternalInfo | null
  task_id: string
  title?: string
}

export type ExternalVisual = 'down' | 'running' | 'unknown'

const DOWN_HEALTH = new Set<string>([
  'auth_failed',
  'down',
  'host_silent',
  'probe_broken',
  'unreachable',
  'watch_missing'
])

const pollInterval = (external: ExternalInfo): number => {
  const observed = external.observed_at
  const due = external.next_due_at

  if (observed == null || due == null) {
    return 300
  }

  return Math.max(120, due - observed)
}

/** Hub/desktop clock: a missed poll (now > next_due_at) is unknown; two intervals late is down. */
export function deriveExternalVisual(external: null | undefined | ExternalInfo, now: number): ExternalVisual | null {
  if (!external) {
    return null
  }

  if (DOWN_HEALTH.has(external.health)) {
    return 'down'
  }

  if (external.health === 'rate_limited') {
    return 'unknown'
  }

  const due = external.next_due_at

  if (due != null) {
    const every = pollInterval(external)

    if (now > due + every) {
      return 'down'
    }

    if (now > due) {
      return 'unknown'
    }
  }

  if (external.job === 'queued' || external.job === 'running') {
    return 'running'
  }

  if (external.job === 'succeeded' || external.job === 'failed' || external.job === 'cancelled') {
    return null
  }

  return 'unknown'
}

export interface ExternalStripeCounts {
  down: number
  external: number
  unknown: number
}

const ZERO: ExternalStripeCounts = { down: 0, external: 0, unknown: 0 }

type ExternalCountRow = {
  kanban_external?: number
  kanban_external_cards?: ExternalCard[]
  kanban_external_down?: number
  kanban_external_unknown?: number
}

const stampedCounts = (row: ExternalCountRow): ExternalStripeCounts | null => {
  if (row.kanban_external == null && row.kanban_external_down == null && row.kanban_external_unknown == null) {
    return null
  }

  const unknown = row.kanban_external_unknown ?? 0
  const down = row.kanban_external_down ?? 0
  const external = row.kanban_external ?? unknown

  return { down, external, unknown }
}

const derivedCounts = (cards: ExternalCard[], now: number): ExternalStripeCounts => {
  let down = 0
  let running = 0
  let unknown = 0

  for (const card of cards) {
    const visual = deriveExternalVisual(card.external, now)

    if (visual === 'down') {
      down += 1
    } else if (visual === 'unknown') {
      unknown += 1
    } else if (visual === 'running') {
      running += 1
    }
  }

  return { down, external: running + unknown, unknown }
}

/** Re-derive from `kanban_external_cards` on the client clock, then take max with count stamps so a capped list cannot drop red. Absent → zeros. */
export function kanbanExternalStripeCounts(row: ExternalCountRow, now: number): ExternalStripeCounts {
  const cards = row.kanban_external_cards
  const stamped = stampedCounts(row)
  const derived = cards?.length ? derivedCounts(cards, now) : null

  if (!derived && !stamped) {
    return ZERO
  }

  if (!derived) {
    return stamped ?? ZERO
  }

  if (!stamped) {
    return derived
  }

  return {
    down: Math.max(derived.down, stamped.down),
    external: Math.max(derived.external, stamped.external),
    unknown: Math.max(derived.unknown, stamped.unknown)
  }
}

const minuteLabel = (seconds: number): string => {
  const mins = Math.max(1, Math.round(Math.abs(seconds) / 60))

  return mins === 1 ? '1 min' : `${mins} min`
}

export function formatExternalRelative(
  epoch: null | number | undefined,
  now: number,
  kind: 'future' | 'past'
): string | null {
  if (epoch == null) {
    return null
  }

  const delta = epoch - now

  if (kind === 'past') {
    return `${minuteLabel(delta)} ago`
  }

  return delta < 0 ? `overdue by ${minuteLabel(delta)}` : `in ${minuteLabel(delta)}`
}

const humanHealth = (health: string): string => health.replaceAll('_', ' ')

const runningStatus = (external: ExternalInfo): string => {
  if (external.job === 'queued') {
    return external.reason?.trim() || 'queued, no runner yet'
  }

  return external.reason?.includes('in progress') ? external.reason : 'in progress'
}

export interface ExternalHover {
  line1: string
  line2: string
  text: string
}

const hoverOf = (line1: string, line2: string): ExternalHover => ({
  line1,
  line2,
  text: [line1, line2].filter(Boolean).join('\n')
})

const downHover = (
  external: ExternalInfo,
  service: string,
  last: string | null,
  next: string | null,
  url: string,
  taskId?: string
): ExternalHover => {
  if (!DOWN_HEALTH.has(String(external.health))) {
    return hoverOf(
      `${service} overdue — no recent check`,
      [last ? `last checked ${last}` : null, next, url].filter(Boolean).join(' · ')
    )
  }

  return hoverOf(
    `${service} ${humanHealth(String(external.health))} — ${external.reason?.trim() || 'unreachable'}`,
    [taskId ? `Agent triaging (${taskId})` : 'Agent triaging', url].filter(Boolean).join(' · ')
  )
}

/** Worded exactly per interface-hover.md. Missing `external` → null (today: no tooltip claim). */
export function formatExternalHover(
  external: null | undefined | ExternalInfo,
  now: number,
  opts: { taskId?: string; visual?: ExternalVisual | null } = {}
): ExternalHover | null {
  if (!external) {
    return null
  }

  const visual = opts.visual ?? deriveExternalVisual(external, now)

  if (!visual) {
    return null
  }

  const service = external.service || 'external service'

  if (external.adapter === 'clock') {
    // A timer: nothing polls it, so there is no check to report, only what it waits for.
    return hoverOf(`Waiting on a timer${external.reason?.trim() ? ` — ${external.reason.trim()}` : ''}`, '')
  }

  const last = formatExternalRelative(external.observed_at, now, 'past')
  const nextRelative = formatExternalRelative(external.next_due_at, now, 'future')
  const next = nextRelative?.startsWith('overdue') ? `check ${nextRelative}` : `next check ${nextRelative ?? 'unknown'}`
  const url = external.url?.trim() || ''

  if (visual === 'down') {
    return downHover(external, service, last, next, url, opts.taskId)
  }

  if (visual === 'unknown') {
    return hoverOf(
      `Status unknown on ${service} — last check ${last ?? 'unknown'}`,
      [next, url].filter(Boolean).join(' · ')
    )
  }

  const verb = external.job === 'running' ? 'Running on' : 'Waiting on'

  const checks = [
    last ? `last checked ${last}` : null,
    nextRelative ? (nextRelative.startsWith('overdue') ? next : `next ${nextRelative}`) : null
  ]
    .filter(Boolean)
    .join(' · ')

  const detail =
    external.job === 'running' && external.reason && !external.reason.includes('in progress')
      ? external.reason.trim()
      : ''

  return hoverOf(`${verb} ${service} — ${runningStatus(external)}`, [detail, checks, url].filter(Boolean).join(' · '))
}

const SIDEBAR_HOVER_CAP = 5

/** Sidebar stripe: each card (max 5) then "+N more". `stampedTotal` beats a capped cards list. Missing cards/fields → null. */
export function formatSessionExternalHover(
  cards: ExternalCard[] | null | undefined,
  now: number,
  stampedTotal?: number
): string | null {
  if (!cards?.length) {
    return null
  }

  const lines: string[] = []

  for (const card of cards.slice(0, SIDEBAR_HOVER_CAP)) {
    const hover = formatExternalHover(card.external, now, { taskId: card.task_id })

    if (!hover) {
      continue
    }

    const title = card.title?.trim()
    lines.push(title ? `${title}\n${hover.text}` : hover.text)
  }

  if (!lines.length) {
    return null
  }

  const extra = Math.max(cards.length, stampedTotal ?? 0) - SIDEBAR_HOVER_CAP

  if (extra > 0) {
    lines.push(`+${extra} more`)
  }

  return lines.join('\n\n')
}

export const SKY_400 = '#38bdf8'
export const EXTERNAL_DOWN_RED = '#ef4444'
