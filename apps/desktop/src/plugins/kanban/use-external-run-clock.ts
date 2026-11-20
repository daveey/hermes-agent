import { useEffect, useMemo, useState } from 'react'

/** Keep expiry reactive even while the board's cached task objects stay unchanged. */
export function useExternalRunClock(row: unknown): number {
  const [tick, setTick] = useState(0)

  useEffect(() => {
    const timer = setInterval(() => setTick(value => value + 1), 15_000)

    return () => clearInterval(timer)
  }, [])

  const sample = useMemo(() => ({ row, tick, nowSeconds: Date.now() / 1000 }), [row, tick])

  return sample.nowSeconds
}
