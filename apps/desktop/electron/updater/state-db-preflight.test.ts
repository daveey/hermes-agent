import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { once } from 'node:events'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { test } from 'vitest'

import {
  createBackupProgressParser,
  preflightStateDb,
  type StateDbBackupProgress,
  stateDbPreflightTimeoutMs
} from './state-db-preflight'

test('the desktop preflight publishes committed WAL rows before its caller can stop the backend', async (): Promise<void> => {
  const home: string = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-db-'))
  const python: string = process.env.HERMES_PYTHON || 'python3'
  const script: string = fileURLToPath(new URL('../../../../hermes_cli/backup_sqlite.py', import.meta.url))

  const child = spawn(
    python,
    [
      '-I',
      '-S',
      '-u',
      '-c',
      `
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute('PRAGMA journal_mode=WAL')
c.execute('PRAGMA wal_autocheckpoint=0')
c.execute('CREATE TABLE messages (body TEXT)')
c.commit()
c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
c.execute("INSERT INTO messages VALUES ('pending in WAL')")
c.commit()
print('ready', flush=True)
sys.stdin.readline()
c.close()
`,
      path.join(home, 'state.db')
    ],
    { stdio: ['pipe', 'pipe', 'pipe'] }
  )

  const logs: string[] = []

  try {
    await once(child.stdout!, 'data')
    await preflightStateDb({
      python,
      script,
      home,
      log: (message: string): void => {
        logs.push(message)
      }
    })
    assert.equal(child.exitCode, null)
    const backups: string[] = fs.readdirSync(home).filter((name: string): boolean => name.endsWith('.bak'))
    assert.equal(backups.length, 1, logs.join('\n'))

    const verify = spawnSync(
      python,
      [
        '-I',
        '-S',
        '-c',
        `
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as c:
    assert c.execute('SELECT body FROM messages').fetchall() == [('pending in WAL',)]
`,
        path.join(home, backups[0]!)
      ],
      { encoding: 'utf8' }
    )

    assert.equal(verify.status, 0, verify.stderr)
    const exited = once(child, 'exit')
    child.stdin!.end('\n')
    await exited
  } finally {
    if (child.exitCode === null && child.signalCode === null) {
      child.kill('SIGKILL')
      await once(child, 'exit')
    }

    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('a managed installation runs the snapshot through the installation launcher', async (): Promise<void> => {
  const home: string = fs.mkdtempSync(path.join(os.tmpdir(), 'managed-preflight-'))
  const shims: string = fs.mkdtempSync(path.join(os.tmpdir(), 'launcher-shim-'))
  const python: string = process.env.HERMES_PYTHON || 'python3'
  const script: string = fileURLToPath(new URL('../../../../hermes_cli/backup_sqlite.py', import.meta.url))

  // Stand-in for the installation launcher under `.hermes/bin`: it must accept
  // exactly what the runtime passes it — `--run-module hermes_cli.backup_sqlite
  // <home>` — and publish the snapshot like the real launcher does.
  const shim: string = path.join(shims, process.platform === 'win32' ? 'hermes.cmd' : 'hermes')
  fs.writeFileSync(
    shim,
    process.platform === 'win32'
      ? `@echo off\r\n"${python}" -I -S "${script}" %3\r\n`
      : `#!/bin/sh\nexec "${python}" -I -S "${script}" "$3"\n`
  )

  if (process.platform !== 'win32') {
    fs.chmodSync(shim, 0o755)
  }

  const logs: string[] = []

  try {
    const created = spawnSync(
      python,
      [
        '-I',
        '-S',
        '-c',
        "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute('CREATE TABLE t (x)'); c.commit(); c.close()",
        path.join(home, 'state.db')
      ],
      { encoding: 'utf8' }
    )

    assert.equal(created.status, 0, created.stderr)

    await preflightStateDb({
      python: null,
      launcher: shim,
      script,
      home,
      log: (message: string): void => {
        logs.push(message)
      }
    })

    const backups: string[] = fs.readdirSync(home).filter((name: string): boolean => name.endsWith('.bak'))
    assert.equal(backups.length, 1, logs.join('\n'))
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
    fs.rmSync(shims, { recursive: true, force: true })
  }
})

test('an older selected checkout without the snapshot helper refuses before backend stop', async (): Promise<void> => {
  const oldRoot: string = fs.mkdtempSync(path.join(os.tmpdir(), 'old-preflight-'))
  let stopped = false

  try {
    await assert.rejects(async (): Promise<void> => {
      await preflightStateDb({
        python: process.env.HERMES_PYTHON || 'python3',
        script: path.join(oldRoot, 'hermes_cli', 'backup_sqlite.py'),
        home: oldRoot,
        log: (): void => {}
      })
      stopped = true
    }, /snapshot|pre-flight/)
    assert.equal(stopped, false)
  } finally {
    fs.rmSync(oldRoot, { recursive: true, force: true })
  }
})

test('the deadline grows with the main database and WAL, but stays bounded', (): void => {
  const home: string = fs.mkdtempSync(path.join(os.tmpdir(), 'preflight-budget-'))

  try {
    const floor = stateDbPreflightTimeoutMs(home)

    for (const name of ['state.db', 'state.db-wal']) {
      fs.writeFileSync(path.join(home, name), '')
      fs.truncateSync(path.join(home, name), 2 * 1024 ** 3)
    }

    const withWal = stateDbPreflightTimeoutMs(home)
    fs.unlinkSync(path.join(home, 'state.db-wal'))
    const withoutWal = stateDbPreflightTimeoutMs(home)
    assert.ok(floor >= 5 * 60_000)
    assert.ok(withWal > withoutWal && withoutWal > floor)
    fs.truncateSync(path.join(home, 'state.db'), 100 * 1024 ** 3)
    assert.ok(stateDbPreflightTimeoutMs(home) <= 30 * 60_000)
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('the event loop stays available while a real snapshot process is running', async (): Promise<void> => {
  const home: string = fs.mkdtempSync(path.join(os.tmpdir(), 'preflight-responsive-'))
  const script = path.join(home, 'snapshot.py')
  fs.writeFileSync(
    script,
    `import pathlib, sys, time
home = pathlib.Path(sys.argv[1])
deadline = time.monotonic() + 3
while not (home / 'release').exists():
    if time.monotonic() > deadline:
        raise RuntimeError('parent event loop was blocked')
    time.sleep(0.01)
print('snapshot finished')
`
  )
  const timer = setTimeout(() => fs.writeFileSync(path.join(home, 'release'), ''), 100)

  try {
    await preflightStateDb({ python: process.env.HERMES_PYTHON || 'python3', script, home, log: (): void => {} })
  } finally {
    clearTimeout(timer)
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('the progress parser joins lines split across chunks and ignores garbage', (): void => {
  const events: StateDbBackupProgress[] = []
  const lines: string[] = []

  const parser = createBackupProgressParser(
    (event: StateDbBackupProgress): void => {
      events.push(event)
    },
    (line: string): void => {
      lines.push(line)
    }
  )

  parser.push('{"event": "backup_progress", "done_bytes": 10, "tot')
  parser.push('al_bytes": 100, "method": "copy"}\nWARNING: clone unavailable\r\n{not json}\n')
  parser.push('{"event": "something_else"}\n{"event": "backup_progress", "done_bytes": "x", "total_bytes": 1}\n')
  parser.push('{"event": "backup_progress", "done_bytes": 150, "total_bytes": 100, "method": "clone"}\n[1, 2]\n\n')
  parser.push('{"event": "backup_lock_wait"}\n')
  parser.push('{"event": "backup_check"}')
  // The unterminated last line waits for its newline (or the end of the stream).
  assert.equal(events.length, 2)
  parser.flush()

  assert.deepEqual(events, [
    { kind: 'progress', doneBytes: 10, totalBytes: 100, method: 'copy' },
    { kind: 'progress', doneBytes: 100, totalBytes: 100, method: 'clone' },
    { kind: 'check' }
  ])
  assert.deepEqual(lines, [
    'WARNING: clone unavailable',
    '{not json}',
    '{"event": "something_else"}',
    '{"event": "backup_progress", "done_bytes": "x", "total_bytes": 1}',
    '[1, 2]',
    'waiting (up to 2 s) for the state.db write lock another writer holds'
  ])
})

test('a real snapshot streams its progress while non-progress stderr goes to the log', async (): Promise<void> => {
  const home: string = fs.mkdtempSync(path.join(os.tmpdir(), 'preflight-progress-'))
  const python: string = process.env.HERMES_PYTHON || 'python3'
  const script: string = fileURLToPath(new URL('../../../../hermes_cli/backup_sqlite.py', import.meta.url))
  const previous = process.env.HERMES_PREFLIGHT_CLONE

  const created = spawnSync(
    python,
    [
      '-I',
      '-S',
      '-c',
      `
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute('PRAGMA journal_mode=WAL')
c.execute('CREATE TABLE t (x BLOB)')
c.executemany('INSERT INTO t VALUES (randomblob(4000))', [()] * 2000)
c.commit()
c.close()
`,
      path.join(home, 'state.db')
    ],
    { encoding: 'utf8' }
  )

  assert.equal(created.status, 0, created.stderr)

  try {
    for (const clone of ['0', '1']) {
      process.env.HERMES_PREFLIGHT_CLONE = clone
      const events: StateDbBackupProgress[] = []
      const logs: string[] = []

      await preflightStateDb({
        python,
        script,
        home,
        log: (message: string): void => {
          logs.push(message)
        },
        onProgress: (event: StateDbBackupProgress): void => {
          events.push(event)
        }
      })

      const progress = events.filter(
        (event: StateDbBackupProgress): event is Extract<StateDbBackupProgress, { kind: 'progress' }> =>
          event.kind === 'progress'
      )

      const last = progress.at(-1)!
      assert.ok(last.totalBytes > 0 && last.doneBytes === last.totalBytes, JSON.stringify(events))
      // No progress JSON reaches the log; the final result line still does.
      assert.ok(!logs.some((line: string): boolean => line.includes('"backup_progress"')), logs.join('\n'))
      assert.ok(
        logs.some((line: string): boolean => line.includes('"path"')),
        logs.join('\n')
      )

      if (clone === '0') {
        assert.equal(last.method, 'copy')
        assert.deepEqual(events.at(-1), { kind: 'check' })
      } else if (process.platform === 'darwin') {
        // APFS clones: one instant 100% line and no whole-database check.
        assert.deepEqual(events, [
          { kind: 'progress', doneBytes: last.totalBytes, totalBytes: last.totalBytes, method: 'clone' }
        ])
      }
    }
  } finally {
    if (previous === undefined) {
      delete process.env.HERMES_PREFLIGHT_CLONE
    } else {
      process.env.HERMES_PREFLIGHT_CLONE = previous
    }

    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('a failing snapshot names its stderr in the error, without progress lines', async (): Promise<void> => {
  const home: string = fs.mkdtempSync(path.join(os.tmpdir(), 'preflight-fail-'))
  const script = path.join(home, 'snapshot.py')
  fs.writeFileSync(
    script,
    `import sys
sys.stderr.write('{"event": "backup_progress", "done_bytes": 1, "total_bytes": 2, "method": "copy"}\\n')
sys.stderr.write('RuntimeError: disk full\\n')
sys.exit(1)
`
  )
  const events: StateDbBackupProgress[] = []

  try {
    await assert.rejects(
      preflightStateDb({
        python: process.env.HERMES_PYTHON || 'python3',
        script,
        home,
        log: (): void => {},
        onProgress: (event: StateDbBackupProgress): void => {
          events.push(event)
        }
      }),
      (error: Error): boolean =>
        /pre-flight failed/.test(error.message) &&
        /disk full/.test(error.message) &&
        !error.message.includes('backup_progress')
    )
    assert.deepEqual(events, [{ kind: 'progress', doneBytes: 1, totalBytes: 2, method: 'copy' }])
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test.skipIf(process.platform === 'win32')(
  'a snapshot that ignores SIGTERM past its deadline is killed',
  async (): Promise<void> => {
    const home: string = fs.mkdtempSync(path.join(os.tmpdir(), 'preflight-kill-'))
    const script = path.join(home, 'snapshot.py')
    const pidFile = path.join(home, 'pid')
    fs.writeFileSync(
      script,
      `import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open(${JSON.stringify(pidFile)}, 'w').write(str(os.getpid()))
time.sleep(120)
`
    )
    const logs: string[] = []
    const started = Date.now()

    try {
      await assert.rejects(
        preflightStateDb({
          python: process.env.HERMES_PYTHON || 'python3',
          script,
          home,
          log: (message: string): void => {
            logs.push(message)
          },
          timeoutMs: 500,
          killGraceMs: 300
        }),
        /timed out after 500 ms/
      )
      assert.ok(Date.now() - started < 30_000)
      assert.ok(
        logs.some((line: string): boolean => /ignored SIGTERM for 300 ms; killing it/.test(line)),
        logs.join('\n')
      )
      const pid = Number(fs.readFileSync(pidFile, 'utf8'))
      assert.throws((): void => {
        process.kill(pid, 0)
      })
    } finally {
      fs.rmSync(home, { recursive: true, force: true })
    }
  }
)
