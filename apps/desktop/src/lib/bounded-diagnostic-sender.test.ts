import { describe, expect, it } from 'vitest'

import { boundDiagnosticSender } from './bounded-diagnostic-sender'

const deferred = () => {
  let resolve!: (v: number) => void
  const promise = new Promise<number>(a => (resolve = a))

  return { promise, resolve }
}

describe('boundDiagnosticSender', () => {
  it('never exceeds the in-flight limit and sends the rest in order as slots free', async () => {
    const gates = Array.from({ length: 10 }, deferred)
    const started: number[] = []
    let active = 0
    let peak = 0

    const send = boundDiagnosticSender(
      async (i: number) => {
        started.push(i)
        peak = Math.max(peak, ++active)

        try {
          return await gates[i]!.promise
        } finally {
          active -= 1
        }
      },
      3
    )

    const results = gates.map((_, i) => send(i))
    expect(started).toEqual([0, 1, 2])
    gates[0]!.resolve(100)
    await results[0]
    await new Promise(r => setTimeout(r, 0))
    expect(started).toEqual([0, 1, 2, 3])
    gates.forEach((g, i) => g.resolve(i + 100))
    expect(await Promise.all(results)).toEqual(gates.map((_, i) => i + 100))
    expect(peak).toBe(3)
    expect(started).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
  })

  it('frees the slot when a send rejects or throws synchronously', async () => {
    let n = 0

    const send = boundDiagnosticSender((): Promise<number> => {
      n += 1

      if (n === 1) {
        throw new Error('sync')
      }

      return n === 2 ? Promise.reject(new Error('async')) : Promise.resolve(n)
    }, 1)

    const [a, b, c] = [send(), send(), send()]
    await expect(a).rejects.toThrow('sync')
    await expect(b).rejects.toThrow('async')
    expect(await c).toBe(3)
  })

  it('rejects the oldest waiter when the queue overflows', async () => {
    const gate = deferred()
    const send = boundDiagnosticSender(() => gate.promise, 1, 2)
    const p = [send(), send(), send(), send()]
    await expect(p[1]).rejects.toThrow('diagnostic queue full')
    gate.resolve(1)
    expect(await p[0]).toBe(1)
    expect(await p[3]).toBe(1)
  })
})
