import type { SessionInfo } from '@hermes/shared/gateway-types'

import { LONG_MSG } from '../config/limits.js'
import { t } from '../i18n/runtime.js'
import { buildToolTrailLine } from '../lib/text.js'
import type { Msg } from '../types.js'

export const introMsg = (info: SessionInfo): Msg => ({ info, kind: 'intro', role: 'system', text: '' })

export const userDisplay = (text: string) => {
  if (text.length <= LONG_MSG) {
    return text
  }

  const first = text.split('\n')[0]?.trim() ?? ''
  const words = first.split(/\s+/).filter(Boolean)
  const prefix = (words.length > 1 ? words.slice(0, 4).join(' ') : first).slice(0, 80)

  return t('libText.messages.longMessage', prefix || t('libText.messages.messageFallback'))
}

/** A resumed user row's durable message row (backtrack browsing finds its prompt by it). */
const storedRowId = (role: string, rowId: unknown): Pick<Msg, 'rowId'> =>
  role === 'user' && Number.isSafeInteger(rowId) && (rowId as number) > 0 ? { rowId: rowId as number } : {}

type RowMeta = TranscriptRow['display_metadata']

const metaText = (meta: RowMeta): string | undefined =>
  typeof meta?.display_text === 'string' && meta.display_text.trim() ? meta.display_text : undefined

const agentsFinished = (meta: RowMeta) => {
  const count = meta && typeof meta.task_count === 'number' ? meta.task_count : undefined

  return count === undefined
    ? t('libText.messages.backgroundAgentWorkFinished')
    : t(
        count === 1 ? 'libText.messages.backgroundAgentsFinishedOne' : 'libText.messages.backgroundAgentsFinishedOther',
        count
      )
}

// display_kind -> its event line. Rows of these kinds are backend notices, drawn as markers, never messages.
const TIMELINE_EVENTS: Record<string, (meta: RowMeta) => string> = {
  model_switch: () => t('libText.messages.modelChanged'),
  auto_continue: () => t('libText.messages.resumedInterruptedTurn'),
  personality_switch: () => t('libText.messages.personalityChanged'),
  // A slash-command /loop tick's expanded prompt: its `↻ /loop /cmd` line, never the prompt.
  loop_tick: meta => metaText(meta) ?? '↻ /loop',
  async_delegation_complete: meta =>
    typeof meta?.display_text === 'string' ? meta.display_text : agentsFinished(meta),
  process_complete: meta =>
    typeof meta?.display_text === 'string' ? meta.display_text : t('libText.messages.backgroundProcessFinished')
}

const timelineEventText = (displayKind: string, meta: RowMeta): string | undefined =>
  Object.hasOwn(TIMELINE_EVENTS, displayKind) ? TIMELINE_EVENTS[displayKind]!(meta) : undefined

export const toTranscriptMessages = (rows: unknown): Msg[] => {
  if (!Array.isArray(rows)) {
    return []
  }

  const out: Msg[] = []
  let pending: string[] = []

  for (const row of rows) {
    if (!row || typeof row !== 'object') {
      continue
    }

    const { context, display_kind, name, role, row_id, text, timestamp } = row as TranscriptRow

    const createdAt =
      typeof timestamp === 'number' && Number.isFinite(timestamp) && timestamp > 0 ? timestamp : undefined

    if (role === 'tool') {
      pending.push(buildToolTrailLine(name ?? 'tool', context ?? ''))

      continue
    }

    if (typeof text !== 'string' || !text.trim()) {
      continue
    }

    // Display-only timeline events: render as dim ◈ markers instead of
    // opaque user messages. Hidden compaction handoffs are skipped entirely.
    if (display_kind === 'hidden') {
      continue
    }

    const eventText = display_kind
      ? timelineEventText(display_kind, (row as TranscriptRow).display_metadata)
      : undefined

    if (eventText !== undefined) {
      out.push({ kind: 'event', role: 'system', text: eventText })
      pending = []

      continue
    }

    if (role === 'assistant') {
      out.push({ role, text, ...(createdAt !== undefined && { createdAt }), ...(pending.length && { tools: pending }) })
      pending = []
    } else if (role === 'user' || role === 'system') {
      out.push({ role, text, ...(createdAt !== undefined && { createdAt }), ...storedRowId(role, row_id) })
      pending = []
    }
  }

  return out
}

export const fmtDuration = (ms: number) => {
  const t = Math.max(0, Math.floor(ms / 1000))
  const h = Math.floor(t / 3600)
  const m = Math.floor((t % 3600) / 60)
  const s = t % 60

  return h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m ${s}s` : `${s}s`
}

interface TranscriptRow {
  context?: string
  display_kind?: string
  display_metadata?: { task_count?: number; [key: string]: unknown }
  name?: string
  role?: string
  row_id?: number
  text?: string
  timestamp?: number
}
