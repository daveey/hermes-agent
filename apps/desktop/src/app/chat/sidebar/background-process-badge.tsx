import { useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover'
import type { SessionInfo } from '@/hermes'
import { useI18n } from '@/i18n'
import { coarseElapsed } from '@/lib/time'
import { stopRowBackgroundProcess } from '@/store/session-background-stop'
import { rowBackgroundProcesses } from '@/store/session-dot-state'
import type { SessionBackgroundProcess } from '@/types/hermes'

// Hover → open; leaving the badge for the card (to reach Stop) must not close
// it, so closing waits a beat and either surface's pointerenter cancels it.
const CLOSE_DELAY_MS = 180

const AGE_KEY = { day: 'ageDay', hour: 'ageHour', minute: 'ageMin' } as const

/**
 * [daveey] The live background processes that keep this row off green: a
 * small process glyph (count when more than one) next to the kanban badge,
 * same size/tone as the periodic loop glyph. Hover lists each command and its
 * age with a Stop button; a click opens the session like the row does.
 */
export function BackgroundProcessBadge({ onOpen, session }: { onOpen: () => void; session: SessionInfo }) {
  const { t } = useI18n()
  const r = t.sidebar.row
  const procs = rowBackgroundProcesses(session)
  const [open, setOpen] = useState(false)
  const closeTimer = useRef<null | ReturnType<typeof setTimeout>>(null)

  const cancelClose = () => {
    if (closeTimer.current !== null) {
      clearTimeout(closeTimer.current)
      closeTimer.current = null
    }
  }

  const show = () => {
    cancelClose()
    setOpen(true)
  }

  const hideSoon = () => {
    cancelClose()
    closeTimer.current = setTimeout(() => setOpen(false), CLOSE_DELAY_MS)
  }

  useEffect(() => cancelClose, [])

  if (procs.length === 0) {
    return null
  }

  const label = r.backgroundProcesses(procs.length)

  const age = (proc: SessionBackgroundProcess): string => {
    if (!proc.started_at) {
      return ''
    }

    const { unit, value } = coarseElapsed(Date.now() - proc.started_at * 1000)

    return unit === 'second' ? r.ageNow : `${value}${r[AGE_KEY[unit]]}`
  }

  return (
    <Popover onOpenChange={setOpen} open={open}>
      <PopoverAnchor asChild>
        <button
          aria-label={label}
          className="pointer-events-auto ml-1 inline-flex items-center gap-px align-[-1px] tabular-nums text-(--ui-text-tertiary) hover:text-foreground focus-visible:text-foreground focus-visible:outline-none"
          data-process-count={procs.length}
          data-testid="session-row-background-processes"
          onBlur={hideSoon}
          onClick={event => {
            event.stopPropagation()
            setOpen(false)
            onOpen()
          }}
          onFocus={show}
          onPointerEnter={show}
          onPointerLeave={hideSoon}
          type="button"
        >
          <Codicon name="server-process" size="0.625rem" />
          {procs.length > 1 && procs.length}
        </button>
      </PopoverAnchor>
      <PopoverContent
        align="end"
        className="w-80 p-1.5"
        data-testid="session-row-background-processes-card"
        onCloseAutoFocus={event => event.preventDefault()}
        onOpenAutoFocus={event => event.preventDefault()}
        onPointerEnter={show}
        onPointerLeave={hideSoon}
        side="top"
      >
        <div className="px-1 pb-1 text-[0.6875rem] font-medium text-(--ui-text-secondary)">{label}</div>
        <ul className="flex flex-col gap-0.5">
          {procs.map(proc => (
            <ProcessLine age={age(proc)} key={proc.id} proc={proc} session={session} />
          ))}
        </ul>
      </PopoverContent>
    </Popover>
  )
}

function ProcessLine({ age, proc, session }: { age: string; proc: SessionBackgroundProcess; session: SessionInfo }) {
  const { t } = useI18n()
  const r = t.sidebar.row
  const [stopping, setStopping] = useState(false)
  // Placeholder rows (an older backend stamped only the count) name nothing
  // the backend could stop.
  const stoppable = !proc.id.startsWith('#')

  return (
    <li className="flex items-center gap-2 rounded-md px-1 py-0.5 hover:bg-(--ui-control-hover-background)">
      <span
        className="min-w-0 flex-1 truncate font-mono text-[0.6875rem] text-foreground"
        data-process-id={proc.id}
        title={proc.command || undefined}
      >
        {proc.command || r.backgroundProcessUnnamed}
      </span>
      {age && <span className="shrink-0 text-[0.625rem] tabular-nums text-(--ui-text-tertiary)">{age}</span>}
      {stoppable && (
        <Button
          className="h-5 shrink-0 px-1.5 text-[0.625rem]"
          disabled={stopping}
          onClick={async event => {
            event.stopPropagation()
            setStopping(true)

            if (!(await stopRowBackgroundProcess(session, proc.id))) {
              setStopping(false)
            }
          }}
          size="sm"
          variant="outline"
        >
          {stopping ? r.stoppingProcess : r.stopProcess}
        </Button>
      )}
    </li>
  )
}
