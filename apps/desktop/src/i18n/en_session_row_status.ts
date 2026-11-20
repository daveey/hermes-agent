import type { SessionRowStatusCopy } from './types_session_row_status'

// [daveey] Why a chat's stored outcome is `unresolved`, in words (session-outcome contract reasons).
const UNRESOLVED_REASONS: Readonly<Record<string, string>> = {
  empty: 'Unresolved: no messages yet',
  interrupted: 'Unresolved: the last turn was interrupted',
  no_summary: 'Unresolved: the last reply did not wrap the work up',
  notice_tail: 'Unresolved: a card update arrived and nothing answered it',
  subagent_killed: 'Unresolved: a subagent was stopped before it finished',
  subagent_lost: 'Unresolved: a subagent went silent without finishing',
  turn_failed: 'Unresolved: the last turn failed'
}

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'] as const
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'] as const
const pad2 = (n: number) => String(n).padStart(2, '0')
// A weekday names one day only within the coming week; past that the label carries the date.
const WEEKDAY_ONLY_S = 6 * 24 * 3600

/** Local `Wed 09:00` for an epoch-seconds wake time (a watch is often days out, so the weekday matters);
 *  `Wed Nov 25 09:00` when it is more than 6 days out, where a weekday alone is ambiguous. */
const wakeLabel = (eta: number): string => {
  const date = new Date(eta * 1000)
  const day = eta - Date.now() / 1000 > WEEKDAY_ONLY_S ? ` ${MONTHS[date.getMonth()]} ${date.getDate()}` : ''

  return `${WEEKDAYS[date.getDay()]}${day} ${pad2(date.getHours())}:${pad2(date.getMinutes())}`
}

/** `title (t_…, wakes …)`: the card's title, when it has one, with the id kept for lookup. */
const watchLabel = (card: { id: string; label?: string; eta?: number }): string => {
  const wake = card.eta === undefined ? '' : `wakes ${wakeLabel(card.eta)}`

  if (card.label && card.label !== card.id) {
    return `${card.label} (${wake ? `${card.id}, ${wake}` : card.id})`
  }

  return wake ? `${card.id} (${wake})` : card.id
}

// [daveey] Sidebar row status copy (kanban + process badges, outcome stripe tooltips), spread into en.ts.
export const enSessionRowStatus = {
  kanbanUnknown: count =>
    `${count} work ${count === 1 ? 'item has' : 'items have'} unknown current status: evidence is missing, expired or unverified. This does not mean done; no new check was requested.`,
  kanbanTasks: (open, blocked) =>
    [open > 0 ? `Waiting on ${open} ${open === 1 ? 'card' : 'cards'}` : '', blocked > 0 ? `${blocked} blocked` : '']
      .filter(Boolean)
      .join(', '),
  kanbanAttention: count =>
    `${count} done ${count === 1 ? 'card says' : 'cards say'} part of the work was dropped, with no follow-up card`,
  kanbanStalled: count =>
    `${count} ${count === 1 ? 'card has' : 'cards have'} stalled: waiting over 2 h on something only an agent can do, with nothing producing it`,
  outcomeDone: 'Done',
  outcomeCrons: names => `Keeps running: ${names.join(', ')}`,
  outcomeWatches: cards => `Still watching: ${cards.map(watchLabel).join(', ')}`,
  outcomeDecision: 'Waiting on your decision',
  outcomeMuted: 'Muted',
  outcomeRunning: 'Running',
  outcomeWaiting: 'Waiting',
  pausedQueue: 'A message queued before a restart may not have run: Send again or Discard',
  outcomeUnresolved: reason =>
    reason && Object.hasOwn(UNRESOLVED_REASONS, reason) ? UNRESOLVED_REASONS[reason] : 'Unresolved',
  unreadNotDone: outcome => `Unread — ${outcome}`,
  muteFailed: 'Could not update the parked state',
  backgroundProcesses: count => `${count} background ${count === 1 ? 'process' : 'processes'} running`,
  backgroundUnknown: 'Open work, current status unknown',
  backgroundWaiting: 'Open work, waiting',
  backgroundExternal: 'Running on an external service',
  backgroundExternalDown: 'An external service is down',
  kanbanExternalRunning: 'running externally',
  kanbanExternalDown: 'external down',
  backgroundProcessUnnamed: 'background process',
  stopProcess: 'Stop',
  stoppingProcess: 'Stopping…'
} satisfies SessionRowStatusCopy
