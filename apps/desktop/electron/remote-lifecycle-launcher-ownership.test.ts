import assert from 'node:assert/strict'
import { type ChildProcess, spawn } from 'node:child_process'

import { test } from 'vitest'

import { pidIsOurDashboard, spawnTokenPath } from './remote-lifecycle'
import { shellSshDouble } from './remote-ssh-exec.test-helpers'

// A Hermes launcher runs the backend as `python -I -c "...; sys.argv = [...]; ..."`: the real `serve --isolated
// --ssh-owner-nonce ...` argv exists only inside the -c script. The ownership proof must read that list, or every
// such backend reads as FOREIGN and is never reused or reaped (seen live on a macOS remote, 2026-10-26).

const OWNERSHIP_ID = '0123456789abcdef0123456789abcdef'
const NONCE = 'fedcba9876543210'
const HERMES = '/opt/fake-hermes/.venv/bin/hermes'

function launcher(argvLiterals: string[][]): Promise<ChildProcess> {
  const assignments = argvLiterals.map(list => `sys.argv = ${JSON.stringify(list)}`).join('; ')
  const script = `import sys, time; ${assignments}; time.sleep(30)`
  const child = spawn('python3', ['-I', '-c', script], { stdio: 'ignore' })

  // Wait until the process has exec'd python with its -c script.
  return new Promise(resolve => setTimeout(() => resolve(child), 400))
}

function backendArgv(nonce = NONCE) {
  return [
    HERMES,
    'serve',
    '--isolated',
    '--host',
    '127.0.0.1',
    '--port',
    '0',
    '--ssh-session-token-file',
    spawnTokenPath(OWNERSHIP_ID, NONCE).replace(/^~/, process.env.HOME || '~'),
    '--ssh-owner-nonce',
    nonce
  ]
}

async function owned(child: ChildProcess) {
  return pidIsOurDashboard(shellSshDouble(), child.pid, NONCE, HERMES, '', OWNERSHIP_ID, '')
}

test('a launcher whose -c script carries the backend argv is recognized as ours', async () => {
  const child = await launcher([backendArgv()])

  try {
    assert.equal(await owned(child), true)
  } finally {
    child.kill()
  }
})

test('a launcher carrying a different nonce is foreign', async () => {
  const child = await launcher([backendArgv('0000000000000000')])

  try {
    assert.equal(await owned(child), false)
  } finally {
    child.kill()
  }
})

test('an ambiguous launcher (two sys.argv literals) is foreign', async () => {
  const child = await launcher([backendArgv(), backendArgv()])

  try {
    assert.equal(await owned(child), false)
  } finally {
    child.kill()
  }
})
