/**
 * [daveey] Per-open timing for session loads (instant session loads, C1).
 *
 * One line per open, written to the Desktop log (and the console) once the
 * open reconciles or is abandoned:
 *
 *   [open-timing] {"id":…,"connection":…,"paint":"cache","paintMs":12,"reconciledMs":840,"changed":false,…}
 *
 * - click:      sidebar click, or resumeSession entry when nothing clicked. A click that never reaches
 *               resumeSession (it fronted a chat already open as a tile) is dropped, not logged.
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
  /** [daveey] `tile`: a session tile/tab open (`@session` ref clicks), timed apart from main-pane opens. */
  surface?: 'tile'
}

const CLICK_WINDOW_MS = 2_000
const KEEP = 200
const open = new Map<string, OpenTimingRecord>()
/** [daveey] Clicks whose resumeSession has started. A click that never got there (it only fronted a chat already
 *  on screen as a tile, or an offline row was refused) is not a main-pane open and is dropped, not logged. */
const entered = new Set<string>()

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
  entered.delete(id)
  open.set(id, { clickAt: now(), connection, id, startedBy: 'click' })
}

/** resumeSession entry. Keeps a click mark from the last moment; otherwise this is the start. */
export function markOpenStart(id: string, connection?: string): void {
  const current = open.get(id)

  entered.add(id)

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

  const opened = entered.has(id) || record.paint !== undefined || record.stages !== undefined
  open.delete(id)
  entered.delete(id)

  // A superseded click that never opened anything (see `entered`) is not an abandoned open.
  if (outcome === 'abandoned' && record.startedBy === 'click' && !opened) {
    return
  }

  record.outcome = outcome
  record.reconciledMs = Math.round(now() - record.clickAt)

  if (changed !== undefined) {
    record.changed = changed
  }

  writeRecord(record)
}

function writeRecord(record: OpenTimingRecord): void {
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

/**
 * [daveey] Timing for one tile open (`@session` ref click, tab restore). Kept out
 * of the main-pane `open` map: a tile resuming beside the main chat must not
 * mark the main open abandoned. Writes the same `[open-timing]` line, tagged
 * `"surface":"tile"`, once reconciled; an open never reconciled writes nothing.
 */
export function startTileOpenTiming(id: string, connection?: string) {
  const record: OpenTimingRecord = { clickAt: now(), connection, id, startedBy: 'resume', surface: 'tile' }

  return {
    record,
    stage(name: string) {
      if (!record.outcome && record.stages?.[name] === undefined) {
        record.stages = { ...record.stages, [name]: Math.round(now() - record.clickAt) }
      }
    },
    paint(source: OpenPaintSource, rows: number) {
      if (!record.paint && !record.outcome) {
        record.paint = source
        record.paintMs = Math.round(now() - record.clickAt)
        record.paintRows = rows
      }
    },
    reconciled(outcome: OpenOutcome, changed?: boolean) {
      if (record.outcome) {
        return
      }

      record.outcome = outcome
      record.reconciledMs = Math.round(now() - record.clickAt)

      if (changed !== undefined) {
        record.changed = changed
      }

      writeRecord(record)
    }
  }
}

/** Test seam. */
export function resetOpenTimings(): void {
  open.clear()
  entered.clear()
  history().splice(0)
}

/** [daveey] One `[open-timing]` log line's record, or null for any other / malformed line. */
export function parseOpenTimingLine(line: string): null | OpenTimingRecord {
  const at = line.indexOf('[open-timing] ')

  if (at < 0) {
    return null
  }

  try {
    const record = JSON.parse(line.slice(at + '[open-timing] '.length)) as OpenTimingRecord

    return record && typeof record === 'object' && typeof record.id === 'string' ? record : null
  } catch {
    return null
  }
}

/**
 * [daveey] The acceptance gate for "a `@session` ref click painted its tile from the transcript cache": a TILE
 * open (`surface:"tile"`) whose first paint was the cache and whose REST page then reconciled (`outcome:"rest"`).
 * A main-pane record (sidebar click, no surface) never qualifies, whatever it painted; neither does an open that
 * was abandoned, failed or never painted, nor a tile that waited on REST (`paint:"rest"`).
 */
export function isTileCachePaint(record: null | OpenTimingRecord | undefined): boolean {
  return record?.surface === 'tile' && record.paint === 'cache' && record.outcome === 'rest'
}

/** The first log line proving a tile cache paint for `id`, or null. */
export function tileCachePaintProof(lines: Iterable<string>, id: string): null | OpenTimingRecord {
  for (const line of lines) {
    const record = parseOpenTimingLine(line)

    if (record?.id === id && isTileCachePaint(record)) {
      return record
    }
  }

  return null
}
