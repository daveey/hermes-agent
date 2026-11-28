import fs from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

import { expect, test } from 'vitest'

import {
  createStaleGatewayRestarter,
  formatGatewayRestartLog,
  runGatewayEnsure,
  runGatewayOwnerRestart
} from './local-gateway'

const lines = (prefix: string, count: number) => Array.from({ length: count }, (_, i) => `${prefix} ${i + 1}`)

// A failed stale-owner restart used to log only the last line of stdout+stderr combined, so the
// reason (usually a stdout line above a trailing hint, or stderr above stdout's last line) was lost.
test('a failed restart keeps the last 20 stdout and last 20 stderr lines separately', () => {
  const stdout = [...lines('out', 30), ''].join('\n')
  const stderr = lines('err', 25).join('\r\n')
  const message = formatGatewayRestartLog('work', { code: 1, stdout, stderr })
  const logged = message.split('\n')

  expect(logged[0]).toContain('[gateway] hermes --profile work gateway restart exited 1')
  expect(message).toContain('hermes-sync owes the restart')

  const outAt = logged.indexOf('[gateway] restart stdout (last 20 lines):')
  const errAt = logged.indexOf('[gateway] restart stderr (last 20 lines):')

  expect(outAt).toBeGreaterThan(0)
  expect(errAt).toBeGreaterThan(outAt)
  expect(logged.slice(outAt + 1, errAt).map(line => line.trim())).toEqual(lines('out', 30).slice(-20))
  expect(logged.slice(errAt + 1).map(line => line.trim())).toEqual(lines('err', 25).slice(-20))
})

test('an empty stream on failure is named, not dropped', () => {
  const message = formatGatewayRestartLog('default', { code: 2, stdout: '', stderr: 'boom' })

  expect(message).toContain('[gateway] restart stdout (last 20 lines): (empty)')
  expect(message).toContain('[gateway] restart stderr (last 20 lines):\n  boom')
})

test('a successful restart stays one line', () => {
  const message = formatGatewayRestartLog('default', {
    code: 0,
    stdout: '→ draining\n✓ Gateway relaunched by its supervisor (PID 5555)\n',
    stderr: ''
  })

  expect(message).toBe(
    '[gateway] hermes --profile default gateway restart exited 0: ✓ Gateway relaunched by its supervisor (PID 5555)'
  )
})

// The real composition main.ts uses: bounded client → formatter → log, behind the one-attempt
// restarter. A failing owner restart is logged in full ONCE; the next dial on the same stale code
// attaches as-is instead of restarting again.
test.skipIf(process.platform === 'win32')(
  'a failing owner restart is attempted once and logged with both streams',
  async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'gw-restart-fail-'))
    const calls = path.join(dir, 'calls')
    const bin = path.join(dir, 'hermes')
    await fs.writeFile(
      bin,
      [
        '#!/bin/sh',
        `echo "$*" >> "${calls}"`,
        'i=1; while [ $i -le 25 ]; do echo "out $i"; i=$((i+1)); done',
        'echo "✗ Gateway service restart failed." ',
        'i=1; while [ $i -le 22 ]; do echo "err $i" >&2; i=$((i+1)); done',
        'exit 1',
        ''
      ].join('\n'),
      { mode: 0o755 }
    )

    const logs: string[] = []
    const run = (args: string[]) =>
      runGatewayEnsure(
        { command: bin, args, env: {}, shell: false },
        dir,
        dir,
        {},
        { timeoutMs: 5000, label: 'hermes gateway restart' }
      )

    const restartOwner = async (owner: string) => {
      logs.push(formatGatewayRestartLog(owner, await runGatewayOwnerRestart(run, owner)))
    }

    const restart = createStaleGatewayRestarter(restartOwner, message => logs.push(message))
    const owner = {
      profile_id: '/h',
      control_home: null,
      instance_id: 'i',
      authority_epoch: 1,
      runtime_protocol: 1,
      api_origin: 'http://127.0.0.1:1',
      capabilities: [],
      supervisor: 'launchd'
    }
    const stale = { gateway: 'old', client: 'new' }

    await (
      await restart(owner, stale)
    )?.done
    expect(await restart(owner, stale)).toBeNull()

    expect((await fs.readFile(calls, 'utf8')).trim().split('\n')).toEqual([
      '--profile default gateway restart --detach'
    ])
    const failure = logs.find(message => message.includes('exited 1')) ?? ''
    expect(failure).toContain('✗ Gateway service restart failed.')
    expect(failure).toContain('out 25')
    expect(failure).not.toContain('out 5\n')
    expect(failure).toContain('err 22')
    expect(failure).toContain('err 3')
    expect(failure).not.toContain('err 2\n')
    expect(failure).toContain('hermes-sync owes the restart')
    expect(logs.at(-1)).toContain('attaching as-is')
  }
)
