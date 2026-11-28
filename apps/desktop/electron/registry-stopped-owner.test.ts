import { execSync, spawn } from 'node:child_process'

import { expect, test, vi } from 'vitest'

import { proveRegistryOwnerStopped } from './registry-stopped-owner'
import { fingerprintToken, spawnLogPath } from './remote-lifecycle'

const ownershipId = '0123456789abcdef0123456789abcdef'
const spawnNonce = 'abcdef0123456789'

const owner = {
  schemaVersion: 2,
  protocolVersion: 1,
  ownershipId,
  spawnNonce,
  pid: 333,
  port: 40000,
  profile: '',
  hermesPath: '~/.local/bin/hermes',
  hermesHome: '~/.hermes',
  logPath: spawnLogPath(ownershipId, spawnNonce),
  tokenFingerprint: fingerprintToken('synthetic'),
  startedAt: '2026-07-14T00:00:00.000Z',
  creationTime: 'linux:123456'
}

test.each(['STOPPED', 'LIVE', 'UNKNOWN', '', 'STOPPED\nnoise'])(
  'requires positive stopped proof: %j',
  async verdict => {
    const ssh = { exec: vi.fn(async (cmd: string) => (cmd.includes('cat ') ? JSON.stringify(owner) : verdict)) }
    expect(await proveRegistryOwnerStopped(ssh, owner)).toBe(verdict === 'STOPPED')
  }
)

test('replacement during stopped proof refuses cleanup', async () => {
  let reads = 0

  const ssh = {
    exec: vi.fn(async (cmd: string) =>
      cmd.includes('cat ')
        ? JSON.stringify(++reads === 1 ? owner : { ...owner, spawnNonce: 'fedcba9876543210' })
        : 'STOPPED'
    )
  }

  expect(await proveRegistryOwnerStopped(ssh, owner)).toBe(false)
})

test('transport failure propagates without stopped authority', async () => {
  const ssh = {
    exec: vi.fn(async (cmd: string) => {
      if (cmd.includes('cat ')) {
        return JSON.stringify(owner)
      }
      throw new Error('SSH offline')
    })
  }

  await expect(proveRegistryOwnerStopped(ssh, owner)).rejects.toThrow('SSH offline')
})

test.skipIf(process.platform !== 'linux')(
  'real read-only probe preserves a live process and recognizes an exited one',
  async () => {
    const child = spawn('python3', ['-c', 'pass'])
    const exitedPid = child.pid!
    await new Promise<void>((resolve, reject) => {
      child.once('exit', () => resolve())
      child.once('error', reject)
    })

    for (const pid of [process.pid, exitedPid]) {
      const expected = { ...owner, pid }

      const ssh = {
        exec: async (cmd: string) =>
          cmd.includes('cat ') ? JSON.stringify(expected) : execSync(cmd, { encoding: 'utf8', timeout: 3000 })
      }

      expect(await proveRegistryOwnerStopped(ssh, expected)).toBe(pid !== process.pid)
    }
  }
)
