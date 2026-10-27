/**
 * [daveey] Per-open timing for session loads (instant session loads, C1).
 *
 * One line per open, written to the Desktop log (and the console) once the
 * open reconciles or is abandoned:
 *
 *   [open-timing] {"id":…,"connection":…,"paint":"cache","paintMs":12,"reconciledMs":840,"changed":false,…}
 *
 * - click:      sidebar click, or resumeSession entry when nothing clicked
 * - paint:      first transcript on screen (`cache`, `warm` or `rest`) and when
 * - reconciled: the authoritative REST page applied (`rest`), the open ended
 *               read-only on an asleep gateway (`asleep`), or another open
 *               superseded it first (`abandoned`, written with its paint)
 * - stages:     click -> owner resolved / gateway ready / REST page back
 *
 * The last 200 records stay on `window.__hermesOpenTimings` for inspection.
 */

export type OpenPaintSource = 'cache' | 'rest' | 'warm'
export type OpenOutcome = 'abandoned' | 'asleep' | 'failed' | 'rest' | 'warm'

export interface OpenTimingRecord {
  changed?: boolean
  clickAt: number
  connection?: string
  id: string
  outcome?: OpenOutcome
  paint?: OpenPaintSource
  paintMs?: number
  paintRows?: number
  reconciledMs?: number
  /** ms from click to each step of the open path (owner resolved, gateway ready, REST page back). */
  stages?: Record<string, number>
  startedBy: 'click' | 'resume'
}

const CLICK_WINDOW_MS = 2_000
const KEEP = 200
const open = new Map<string, OpenTimingRecord>()

const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now())

function history(): OpenTimingRecord[] {
  const host = globalThis as { __hermesOpenTimings?: OpenTimingRecord[] }
  host.__hermesOpenTimings ??= []

  return host.__hermesOpenTimings
}

/** Another open started: earlier unfinished opens are written as abandoned. */
function abandonOthers(id: string): void {
  for (const other of [...open.keys()]) {
    if (other !== id) {
      markOpenReconciled(other, 'abandoned')
    }
  }
}

/** A sidebar click on `id`: the clock every later mark measures against. */
export function markOpenClick(id: string, connection?: string): void {
  if (!id) {
    return
  }

  abandonOthers(id)
  open.set(id, { clickAt: now(), connection, id, startedBy: 'click' })
}

/** resumeSession entry. Keeps a click mark from the last moment; otherwise this is the start. */
export function markOpenStart(id: string, connection?: string): void {
  const current = open.get(id)

  if (current && !current.outcome && now() - current.clickAt < CLICK_WINDOW_MS) {
    current.connection ||= connection

    return
  }

  abandonOthers(id)
  open.set(id, { clickAt: now(), connection, id, startedBy: 'resume' })
}

/** A step of the open path finished (first mark per name wins). */
export function markOpenStage(id: string, stage: string): void {
  const record = open.get(id)

  if (record && !record.outcome && record.stages?.[stage] === undefined) {
    record.stages = { ...record.stages, [stage]: Math.round(now() - record.clickAt) }
  }
}

/** First transcript on screen for this open (later paints are ignored). */
export function markOpenPaint(id: string, source: OpenPaintSource, rows: number): void {
  const record = open.get(id)

  if (!record || record.paint || record.outcome) {
    return
  }

  record.paint = source
  record.paintMs = Math.round(now() - record.clickAt)
  record.paintRows = rows
}

/** The open settled: write its line. `changed` = the fresh page differed from the paint. */
export function markOpenReconciled(id: string, outcome: OpenOutcome, changed?: boolean): void {
  const record = open.get(id)

  if (!record || record.outcome) {
    return
  }

  record.outcome = outcome
  record.reconciledMs = Math.round(now() - record.clickAt)

  if (changed !== undefined) {
    record.changed = changed
  }

  open.delete(id)

  const list = history()
  list.push(record)
  list.splice(0, Math.max(0, list.length - KEEP))

  const line = `[open-timing] ${JSON.stringify(record)}`

  try {
    console.info(line)
    window.hermesDesktop?.logLine?.(line)
  } catch {
    // logging must never break an open
  }
}

/** Test seam. */
export function resetOpenTimings(): void {
  open.clear()
  history().splice(0)
}
