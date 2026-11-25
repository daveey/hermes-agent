import { useEffect, useState } from 'react'

/** Local display ticks only; no backend reads. Slow down after the seconds figure disappears. */
export function useWaitClock(fast: boolean, enabled = true): number {
  const [now, setNow] = useState(() => Date.now() / 1000)

  useEffect(() => {
    if (!enabled) {
      return
    }

    setNow(Date.now() / 1000)
    const id = window.setInterval(() => setNow(Date.now() / 1000), fast ? 1000 : 10_000)

    return () => window.clearInterval(id)
  }, [fast, enabled])

  return now
}
