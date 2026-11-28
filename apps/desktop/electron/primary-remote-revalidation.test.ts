import { expect, test } from 'vitest'

import { capturePrimarySshTeardown, createPrimaryRemoteRevalidator } from './primary-remote-revalidation'
import { RemoteLivenessTracker, RemoteRevalidationCoordinator } from './remote-liveness'
import { createBootstrapCoordinator } from './ssh-bootstrap-coordinator'

test('arms the SSH retirement fence before invalidating the cached primary', async () => {
  const connection = Promise.resolve({ mode: 'remote', remoteKind: 'ssh', baseUrl: 'http://127.0.0.1:12345' })
  let current: typeof connection | null = connection
  let fenced = false
  let release!: () => void

  const closed = new Promise<void>(resolve => {
    release = resolve
  })

  const events: string[] = []

  const revalidate = createPrimaryRemoteRevalidator({
    currentConnectionPromise: () => current,
    coordinator: new RemoteRevalidationCoordinator(),
    tracker: new RemoteLivenessTracker(1),
    probe: async () => {
      throw Error('stalled return path')
    },
    log() {},
    captureSshTeardown: () => async () => {
      fenced = true
      events.push('fence')
      await closed
      events.push('closed')
    },
    resetConnection: () => {
      expect(fenced).toBe(true)
      current = null
      events.push('invalidate')
      release()
    }
  })

  try {
    await expect(revalidate()).resolves.toEqual({ ok: true, rebuilt: true })
    expect(events).toEqual(['fence', 'invalidate', 'closed'])
  } finally {
    release()
  }
})

test.each(['', 'default', 'registry:hub'])('pins exact SSH scope %j without retiring another profile', async scope => {
  const coordinator = createBootstrapCoordinator()
  const selected = { localPort: 12345 }
  const bystander = { localPort: 23456 }

  const states = new Map([
    [scope, selected],
    ['bystander', bystander]
  ])

  const retired: string[] = []

  const teardown = capturePrimarySshTeardown(
    { baseUrl: 'http://127.0.0.1:12345' },
    {
      states,
      cancelAndWait: (key, stop) => coordinator.cancelAndWait(key, stop),
      teardown: async key => {
        retired.push(key)
        states.delete(key)
      }
    }
  )

  await teardown()
  expect(retired).toEqual([scope])
  expect(states.get('bystander')).toBe(bystander)
})

test('a replacement in the captured scope survives a late teardown', async () => {
  const old = { localPort: 12345 }
  const fresh = { localPort: 34567 }
  const states = new Map([['', old]])
  let retired = 0

  const teardown = capturePrimarySshTeardown(
    { baseUrl: 'http://127.0.0.1:12345' },
    {
      states,
      cancelAndWait: async (_key, stop) => {
        states.set('', fresh)
        await stop()
      },
      teardown: async () => {
        retired++
      }
    }
  )

  await teardown()
  expect(retired).toBe(0)
  expect(states.get('')).toBe(fresh)
})

test('an ordinary redial cannot pass the real coordinator drain while the old SSH scope closes', async () => {
  const coordinator = createBootstrapCoordinator()
  const state = { localPort: 12345 }
  const states = new Map([['', state]])
  const connection = Promise.resolve({ mode: 'remote', remoteKind: 'ssh', baseUrl: 'http://127.0.0.1:12345' })
  let current: typeof connection | null = connection
  let closeEntered!: () => void

  const closing = new Promise<void>(resolve => {
    closeEntered = resolve
  })

  let release!: () => void

  const closed = new Promise<void>(resolve => {
    release = resolve
  })

  let replacement: Promise<unknown> | undefined
  let redialStarted = false

  const revalidate = createPrimaryRemoteRevalidator({
    currentConnectionPromise: () => current,
    coordinator: new RemoteRevalidationCoordinator(),
    tracker: new RemoteLivenessTracker(1),
    probe: async () => {
      throw Error('stalled return path')
    },
    log() {},
    captureSshTeardown: descriptor =>
      capturePrimarySshTeardown(descriptor, {
        states,
        cancelAndWait: (scope, stop) => coordinator.cancelAndWait(scope, stop),
        teardown: async () => {
          closeEntered()
          await closed
          states.delete('')
        }
      }),
    resetConnection: () => {
      current = null
      replacement = coordinator.start('', 'fresh', async () => {
        redialStarted = true
      })
    }
  })

  const pending = revalidate()

  try {
    await closing
    expect(redialStarted).toBe(false)
  } finally {
    release()
    await pending
    await replacement
  }

  expect(redialStarted).toBe(true)
})
