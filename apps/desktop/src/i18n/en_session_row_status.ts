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
const pad2 = (n: number) => String(n).padStart(2, '0')

/** Local `Wed 09:00` for an epoch-seconds wake time (a watch is often days out, so the weekday matters). */
const wakeLabel = (eta: number): string => {
  const date = new Date(eta * 1000)

  return `${WEEKDAYS[date.getDay()]} ${pad2(date.getHours())}:${pad2(date.getMinutes())}`
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
  outcomeWatches: cards =>
    `Still watching: ${cards.map(card => (card.eta === undefined ? card.id : `${card.id} (wakes ${wakeLabel(card.eta)})`)).join(', ')}`,
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
  backgroundProcessUnnamed: 'background process',
  stopProcess: 'Stop',
  stoppingProcess: 'Stopping…'
} satisfies SessionRowStatusCopy
