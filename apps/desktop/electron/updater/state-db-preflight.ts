import { spawn, type SpawnOptions } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

import { hiddenWindowsChildOptions } from '../windows-child-options'

const DEFAULT_TIMEOUT_MS = 5 * 60_000
const MAX_TIMEOUT_MS = 30 * 60_000
const COPY_RATE_BYTES_PER_SECOND = 5 * 1024 * 1024
const TIMEOUT_SLACK_MS = 60_000

export function stateDbPreflightTimeoutMs(home: string): number {
  let bytes = 0

  for (const name of ['state.db', 'state.db-wal']) {
    try {
      bytes += fs.statSync(path.join(home, name)).size
    } catch {
      // Missing WAL is normal; an unreadable size retains the conservative floor.
    }
  }

  return Math.min(
    MAX_TIMEOUT_MS,
    Math.max(DEFAULT_TIMEOUT_MS, Math.ceil(bytes / COPY_RATE_BYTES_PER_SECOND) * 1_000 + TIMEOUT_SLACK_MS)
  )
}

/** One progress report from `hermes_cli/backup_sqlite.py` (a JSON object per stderr line). */
export type StateDbBackupProgress =
  { kind: 'progress'; doneBytes: number; totalBytes: number; method: 'clone' | 'copy' } | { kind: 'check' }

function readProgressEvent(line: string): StateDbBackupProgress | null {
  const text = line.trim()

  if (!text.startsWith('{')) {
    return null
  }

  let value: unknown

  try {
    value = JSON.parse(text)
  } catch {
    return null
  }

  if (typeof value !== 'object' || value === null) {
    return null
  }

  const event = value as Record<string, unknown>

  if (event.event === 'backup_check') {
    return { kind: 'check' }
  }

  if (event.event !== 'backup_progress') {
    return null
  }

  const done = event.done_bytes
  const total = event.total_bytes

  if (
    typeof done !== 'number' ||
    typeof total !== 'number' ||
    !Number.isFinite(done) ||
    !Number.isFinite(total) ||
    done < 0 ||
    total < 0
  ) {
    return null
  }

  return {
    kind: 'progress',
    doneBytes: Math.min(done, total),
    totalBytes: total,
    method: event.method === 'clone' ? 'clone' : 'copy'
  }
}

/**
 * Splits the snapshot's stderr into lines as chunks arrive (a line may span
 * chunks) and routes each one: progress events to `onProgress`, everything
 * else (warnings, tracebacks) to `onLine`. `flush()` ends a final unterminated line.
 */
export function createBackupProgressParser(
  onProgress: (progress: StateDbBackupProgress) => void,
  onLine: (line: string) => void
): { push: (chunk: string) => void; flush: () => void } {
  let pending = ''

  const route = (line: string): void => {
    const event = readProgressEvent(line)

    if (!event && /^\s*\{\s*"event"\s*:\s*"backup_lock_wait"\s*\}\s*$/.test(line)) {
      onLine('waiting (up to 2 s) for the state.db write lock another writer holds')
    } else if (event) {
      onProgress(event)
    } else if (line.trim()) {
      onLine(line.replace(/\r$/, ''))
    }
  }

  return {
    push: (chunk: string): void => {
      pending += chunk
      const lines = pending.split('\n')
      pending = lines.pop() ?? ''
      lines.forEach(route)
    },
    flush: (): void => {
      const rest = pending
      pending = ''

      if (rest) {
        route(rest)
      }
    }
  }
}

interface StateDbPreflight {
  python: string | null
  script: string
  home: string
  log: (message: string) => void
  /**
   * The installation launcher of a PM-managed checkout (`.hermes/bin/hermes`).
   * A managed checkout carries no venv of its own — the launcher owns
   * interpreter and generation selection there — so the snapshot runs through
   * it exactly like the update check does (`readSourceUpdate`).
   */
  launcher?: string | null
  /** Snapshot progress as it arrives (stderr JSON lines); other stderr lines go to `log`. */
  onProgress?: (progress: StateDbBackupProgress) => void
  /** Deadline before SIGTERM; default from the database size (stateDbPreflightTimeoutMs). */
  timeoutMs?: number
  /** How long a SIGTERM'd snapshot gets to exit before SIGKILL. */
  killGraceMs?: number
}

const KILL_GRACE_MS = 10_000

interface SnapshotRun {
  stdout: string
  /** Non-progress stderr lines, for the failure message. */
  stderr: string
}

function runSnapshot(
  command: string,
  args: string[],
  options: SpawnOptions,
  timeoutMs: number,
  killGraceMs: number,
  onProgress: (progress: StateDbBackupProgress) => void,
  log: (message: string) => void
): Promise<SnapshotRun> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { ...options, stdio: ['ignore', 'pipe', 'pipe'] })
    const stderrLines: string[] = []
    let stdout = ''
    let timedOut = false

    const parser = createBackupProgressParser(
      (progress: StateDbBackupProgress): void => {
        try {
          onProgress(progress)
        } catch (error: unknown) {
          log(`[updates] state.db pre-flight progress handler failed: ${String(error)}`)
        }
      },
      (line: string): void => {
        stderrLines.push(line)
        log(`[updates] state.db pre-flight: ${line}`)
      }
    )

    let killTimer: ReturnType<typeof setTimeout> | null = null

    // Same stop as execFile's timeout: SIGTERM, which the snapshot turns into its staging cleanup.
    // A snapshot that does not exit within the grace (stuck in one long C call) gets SIGKILL.
    const timer = setTimeout((): void => {
      timedOut = true
      child.kill('SIGTERM')
      killTimer = setTimeout((): void => {
        log(`[updates] state.db pre-flight ignored SIGTERM for ${killGraceMs} ms; killing it`)
        child.kill('SIGKILL')
      }, killGraceMs)
    }, timeoutMs)

    const clearTimers = (): void => {
      clearTimeout(timer)

      if (killTimer) {
        clearTimeout(killTimer)
      }
    }

    child.stdout!.setEncoding('utf8')
    child.stderr!.setEncoding('utf8')
    child.stdout!.on('data', (chunk: string): void => {
      stdout += chunk
    })
    child.stderr!.on('data', parser.push)
    child.on('error', (error: Error): void => {
      clearTimers()
      reject(error)
    })
    child.on('close', (code: number | null, signal: NodeJS.Signals | null): void => {
      clearTimers()
      parser.flush()
      const stderr = stderrLines.join('\n')

      if (code === 0) {
        resolve({ stdout, stderr })

        return
      }

      const reason = timedOut
        ? `timed out after ${timeoutMs} ms`
        : signal
          ? `killed by ${signal}`
          : `exited with code ${code}`

      reject(new Error(`Command failed (${reason}): ${[command, ...args].join(' ')}${stderr ? `\n${stderr}` : ''}`))
    })
  })
}

// Await completion before stopping the backend, while keeping Electron responsive.
export async function preflightStateDb({
  python,
  script,
  home,
  log,
  launcher = null,
  onProgress = (): void => {},
  timeoutMs,
  killGraceMs = KILL_GRACE_MS
}: StateDbPreflight): Promise<void> {
  try {
    const command: string | null = launcher ?? python

    if (!command) {
      throw new Error('Python not found')
    }

    const args: string[] = launcher ? ['--run-module', 'hermes_cli.backup_sqlite', home] : ['-I', '-S', script, home]

    // Node refuses direct .cmd execFile; an older published launcher can still
    // be one. Same fail-closed guard as the update check: shell:true would
    // interpolate untrusted paths, so keep cmd.exe's one unavoidable parse
    // closed instead.
    const viaCmd: boolean = process.platform === 'win32' && /\.cmd$/i.test(command)

    if (viaCmd && [command, ...args].some((value: string): boolean => /["%&|<>^\r\n]/.test(value))) {
      throw new Error('The pre-flight snapshot contains an unsafe Windows command argument.')
    }

    const { stdout } = await runSnapshot(
      viaCmd ? (process.env.ComSpec ?? 'cmd.exe') : command,
      viaCmd
        ? ['/d', '/v:off', '/s', '/c', `""${command}" ${args.map((arg: string): string => `"${arg}"`).join(' ')}"`]
        : args,
      hiddenWindowsChildOptions({ windowsVerbatimArguments: viaCmd }),
      timeoutMs ?? stateDbPreflightTimeoutMs(home),
      killGraceMs,
      onProgress,
      log
    )

    log(`[updates] state.db pre-flight: ${stdout.trim()}`)
  } catch (error: unknown) {
    const message =
      `state.db pre-flight failed: ${error instanceof Error ? error.message : String(error)}. ` +
      'Update cancelled before backend shutdown. Update the selected installation with its hermes update command, then retry.'

    log(`[updates] ${message}`)
    throw new Error(message, { cause: error })
  }
}
