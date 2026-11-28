import { afterEach, expect, it, vi } from 'vitest'

import { clearSingleFlightSessionResumeState, singleFlightSessionResume } from './single-flight-resume'
import { resumeStoredRuntimeSession } from './utils'

// Both production entry paths must wait INSIDE the shared flight, before any
// prompt can be admitted. No prompt replay is part of restart recovery.
afterEach(() => {
  clearSingleFlightSessionResumeState()
  vi.useRealTimers()
})

it('open resume holds draining then closed transport until the replacement answers', async () => {
  vi.useFakeTimers()

  const run = vi
    .fn()
    .mockRejectedValueOnce(
      Object.assign(new Error('runtime_draining'), { code: 4001, data: { reason: 'runtime_draining' } })
    )
    .mockRejectedValueOnce(new Error('Hermes gateway connection closed'))
    .mockResolvedValueOnce({ session_id: 'new-runtime' })

  const result = singleFlightSessionResume('drain-open', run, { scope: { connectionId: 'hub', profile: 'work' } })
  const joiner = singleFlightSessionResume('drain-open', run, { scope: { connectionId: 'hub', profile: 'work' } })
  const outcome = Promise.all([result, joiner])
  await vi.runAllTimersAsync()
  await expect(outcome).resolves.toEqual([{ session_id: 'new-runtime' }, { session_id: 'new-runtime' }])
  expect(run).toHaveBeenCalledTimes(3)
})

it('pre-send recovery waits through restart and submits exactly once on the new runtime', async () => {
  vi.useFakeTimers()

  const requestGateway = vi
    .fn()
    .mockRejectedValueOnce(new Error('Gateway is draining existing work; retry shortly.'))
    .mockResolvedValueOnce({ session_id: 'new-runtime' })

  const submit = vi.fn()

  const pending = resumeStoredRuntimeSession('drain-send', {
    requestGateway: requestGateway as never,
    resolveProfile: async () => 'work'
  }).then(runtime => submit(runtime))

  await vi.advanceTimersByTimeAsync(0)
  expect(submit).not.toHaveBeenCalled()
  await vi.runAllTimersAsync()
  await pending
  expect(submit).toHaveBeenCalledExactlyOnceWith('new-runtime')
  expect(requestGateway.mock.calls.map(([method, params]) => [method, params.profile])).toEqual([
    ['session.resume', 'work'],
    ['session.resume', 'work']
  ])
})

it('does not hide an ordinary timeout or permission refusal behind restart recovery', async () => {
  for (const error of [new Error('request timed out after 30s: session.resume'), new Error('permission_denied')]) {
    const run = vi.fn().mockRejectedValue(error)
    await expect(singleFlightSessionResume('terminal', run)).rejects.toBe(error)
    expect(run).toHaveBeenCalledOnce()
  }
})
