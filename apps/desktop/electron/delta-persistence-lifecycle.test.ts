import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, describe, expect, it, vi } from 'vitest'

import { bodyHash, DELTA_STORE_MAX_BYTES, DeltaBodyStore } from './delta-reads'

const dirs: string[] = []

const directory = () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'delta-lifecycle-'))
  dirs.push(dir)

  return dir
}

const reopen = (dir: string) => {
  const store = new DeltaBodyStore()
  store.persistTo(dir)

  return store
}

const gate = () => {
  let release!: () => void
  const promise = new Promise<void>(resolve => {
    release = resolve
  })

  return { promise, release }
}

afterEach(() => {
  vi.restoreAllMocks()

  for (const dir of dirs.splice(0)) {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

describe('delta persistence lifecycle ordering', () => {
  it('quit drains a copy whose async flush is waiting for mkdir', async () => {
    const dir = directory()
    const store = reopen(dir)
    const body = Buffer.from('pending body')
    const entered = gate()
    const resume = gate()
    const mkdir = fs.promises.mkdir.bind(fs.promises)
    vi.spyOn(fs.promises, 'mkdir').mockImplementation(async (...args) => {
      entered.release()
      await resume.promise

      return mkdir(...args)
    })
    store.put('k', bodyHash(body), body)
    const flushing = store.flush()
    await entered.promise

    try {
      store.flushSync()
      expect(reopen(dir).get('k')?.body).toEqual(body)
    } finally {
      resume.release()
      await flushing
    }

    expect(reopen(dir).get('k')?.body).toEqual(body)
  })

  it.each(['delete', 'replace', 'quit'] as const)('a delayed writer cannot undo %s', async action => {
    const dir = directory()
    const store = reopen(dir)
    const old = Buffer.from('old body')
    const next = Buffer.from('new body')
    const entered = gate()
    const resume = gate()
    const write = fs.promises.writeFile.bind(fs.promises)
    vi.spyOn(fs.promises, 'writeFile').mockImplementationOnce(async (...args) => {
      await write(...args)
      entered.release()
      await resume.promise
    })
    store.put('k', bodyHash(old), old)
    const flushing = store.flush()
    await entered.promise

    try {
      if (action === 'delete') {
        const rm = fs.rm.bind(fs)
        let removed: Promise<void> | undefined
        vi.spyOn(fs, 'rm').mockImplementation((file, options, callback) => {
          removed = new Promise(resolve =>
            rm(file, options, error => {
              callback(error)
              resolve()
            })
          )
        })
        store.delete('k')

        if (removed) {
          await removed
        }
        // The async deletion must fence publication even without a quit epoch change.
      } else {
        store.put('k', bodyHash(next), next)

        if (action === 'quit') {
          store.flushSync()
        }
      }
    } finally {
      resume.release()
      await flushing
      await store.flush()
    }

    expect(reopen(dir).get('k')?.body).toEqual(action === 'delete' ? undefined : next)
    expect(fs.readdirSync(dir).filter(name => name.endsWith('.tmp'))).toEqual([])
  })

  it('memory eviction is not a disk invalidation', async () => {
    const dir = directory()
    const store = reopen(dir)
    const old = Buffer.from('persisted')
    store.put('k', bodyHash(old), old)
    await store.flush()
    store.put('k', bodyHash(Buffer.from('changed')), Buffer.from('changed'))

    for (let i = 0; i < 100; i++) {
      store.put(`other${i}`, bodyHash(old), old)
    }
    await store.flush()
    expect(reopen(dir).get('k')?.body).toEqual(old)
  })

  it('interrupted temporary writes count toward the quit budget', () => {
    const dir = directory()
    const tmp = path.join(dir, `${'a'.repeat(32)}.base.123.tmp`)
    fs.writeFileSync(tmp, Buffer.alloc(DELTA_STORE_MAX_BYTES))
    fs.utimesSync(tmp, new Date(0), new Date(0))
    const store = reopen(dir)
    const body = Buffer.from('newest')
    store.put('k', bodyHash(body), body)
    store.flushSync()
    expect(fs.existsSync(tmp)).toBe(false)
    expect(reopen(dir).get('k')?.body).toEqual(body)
  })

  it('quit-only writes across reopened stores stay within the disk budget', () => {
    const dir = directory()

    for (let i = 0; i < 4; i++) {
      const store = reopen(dir)
      const body = Buffer.alloc(DELTA_STORE_MAX_BYTES / 3, i)
      store.put(`k${i}`, bodyHash(body), body)
      store.flushSync()
      const files = fs.readdirSync(dir)
      expect(files.reduce((sum, name) => sum + fs.statSync(path.join(dir, name)).size, 0)).toBeLessThanOrEqual(
        DELTA_STORE_MAX_BYTES
      )

      for (const name of files) {
        expect(fs.statSync(path.join(dir, name)).mode & 0o777).toBe(0o600)
      }
      expect(reopen(dir).get(`k${i}`)?.hash).toBe(bodyHash(body))
    }
  })
})
