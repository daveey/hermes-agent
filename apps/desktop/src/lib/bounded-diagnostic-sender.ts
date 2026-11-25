/**
 * [daveey] Diagnostic POSTs (stripe reports) are fire-and-forget, but a sidebar of N rows commits N paints at once
 * (after a renderer stall or wake: 74 in one tick). Unbounded, they all ride one link together, all wait the same
 * 30 s request timeout and all log "Timed out connecting to Hermes backend" when that link is briefly half-open.
 * This keeps at most `limit` in flight; the rest wait in a FIFO (their timeout clock starts when they are sent).
 * A queue beyond `maxQueued` rejects the oldest waiter so memory stays bounded; callers already retry/drop.
 */
export function boundDiagnosticSender<A extends unknown[], R>(
  send: (...args: A) => Promise<R>,
  limit = 4,
  maxQueued = 512
): (...args: A) => Promise<R> {
  let active = 0
  const waiting: { reject: (e: unknown) => void; run: () => void }[] = []

  const pump = () => {
    while (active < limit && waiting.length) {
      waiting.shift()!.run()
    }
  }

  return (...args: A) =>
    new Promise<R>((resolve, reject) => {
      const run = () => {
        active += 1
        let p: Promise<R>

        try {
          p = send(...args)
        } catch (e) {
          p = Promise.reject(e)
        }

        void p.then(resolve, reject).finally(() => {
          active -= 1
          pump()
        })
      }

      waiting.push({ reject, run })

      if (waiting.length > maxQueued) {
        waiting.shift()!.reject(new Error('diagnostic queue full'))
      }

      pump()
    })
}
