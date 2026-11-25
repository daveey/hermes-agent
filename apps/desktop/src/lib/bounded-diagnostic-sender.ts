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
  maxQueued = 512,
  keyOf: (...args: A) => unknown = () => undefined
): (...args: A) => Promise<R> {
  // A half-open gateway must not hold another connection's diagnostics behind its timeouts.
  const budgets = new Map<unknown, { active: number; waiting: { reject: (e: unknown) => void; run: () => void }[] }>()

  return (...args: A) =>
    new Promise<R>((resolve, reject) => {
      const key = keyOf(...args)
      let budget = budgets.get(key)

      if (!budget) {
        budget = { active: 0, waiting: [] }
        budgets.set(key, budget)
      }

      const state = budget

      const pump = () => {
        while (state.active < limit && state.waiting.length) {
          state.waiting.shift()!.run()
        }

        if (state.active === 0 && state.waiting.length === 0) {
          budgets.delete(key)
        }
      }

      const run = () => {
        state.active += 1
        let p: Promise<R>

        try {
          p = send(...args)
        } catch (e) {
          p = Promise.reject(e)
        }

        void p.then(resolve, reject).finally(() => {
          state.active -= 1
          pump()
        })
      }

      state.waiting.push({ reject, run })

      if (state.waiting.length > maxQueued) {
        state.waiting.shift()!.reject(new Error('diagnostic queue full'))
      }

      pump()
    })
}
