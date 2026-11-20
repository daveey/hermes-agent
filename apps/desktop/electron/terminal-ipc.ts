// The embedded terminal's PTY host: shell resolution, env scrubbing, session
// registry, and the hermes:terminal:* IPC surface. Extracted from main.ts; the
// factory owns the session map and returns the dispose helpers main.ts needs
// for SSH teardown. findOnPath / logging / connection routing stay injected.
import { execFile } from 'node:child_process'
import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

import { app, ipcMain } from 'electron'
import nodePty from 'node-pty'

import { resolveTerminalConnection, resolveTerminalConnectionForSender } from './connection-apply'
import type { TerminalLauncher } from './external-terminal'
import { ensureSpawnHelperExecutable } from './spawn-helper-perms'
import { buildInteractiveSshArgs } from './ssh-connection'
import {
  requestedTerminalGateway,
  type SshTerminalTarget,
  type TerminalGatewayOwner,
  type TerminalGatewayTarget
} from './terminal-gateway-target'
import { createTerminalOutputGate } from './terminal-output-gate'
import { recoveryCliArgs, remoteRecoveryCommand, requestedRecovery } from './terminal-recovery'
import { applyWindowsMsysBashEnvDefaults } from './windows-msys-bash-env'
import { buildWindowsInteractiveCommand } from './windows-remote-lifecycle'

export interface TerminalIpcDeps {
  isWindows: boolean
  findOnPath: (command: string) => null | string
  rememberLog: (line: string) => void
  activeSshTerminalTarget: (webContentsId: number) => unknown
  /** The ssh client to spawn for remote terminals (resolveSshBinary). */
  sshBinary: () => string
  ensureBackend: (webContentsId: number) => Promise<unknown>
  getSshConnectionState: (scope: string) =>
    | undefined
    | {
        hermesHome?: null | string
        hermesPath?: null | string
        registryConnectionId?: null | string
        remotePlatform?: string
      }
  /** The host of the gateway a tab was opened on. A tab owns its gateway: the
   *  window-backend route (which a Sessions pick never re-points) only serves
   *  tabs that recorded none. */
  gatewayTarget: (owner: TerminalGatewayOwner) => TerminalGatewayTarget
  ensureGatewayBackend: (owner: TerminalGatewayOwner) => Promise<unknown>
  /** This device's Hermes launcher for `hermes <args>`, resolved the way the
   *  app resolves its own backend; null when no runtime is installed. Absent
   *  on hosts that cannot run an account recovery. */
  localHermesCli?: (args: string[]) => Promise<null | TerminalLauncher>
}

export interface TerminalIpcApi {
  disposeTerminalSession: (id: string) => boolean
  disposeTerminalSessionsForSshScope: (scope: string) => void
  disposeAllTerminalSessions: () => void
}

// macOS accepts the bare charset name "UTF-8" as a locale; glibc does not, so
// every Linux pane would print `bash: warning: setlocale: LC_CTYPE: cannot
// change locale (UTF-8)`. Reuse the user's LANG there, else the glibc-guaranteed
// C.UTF-8. Pure: the platform arrives as data so tests need not fake the host.
export function terminalLcCtype(
  env: { LANG?: string; LC_CTYPE?: string },
  platform: NodeJS.Platform = process.platform
): string {
  if (env.LC_CTYPE) {
    return env.LC_CTYPE
  }

  return platform === 'darwin' ? 'UTF-8' : env.LANG || 'C.UTF-8'
}

export function registerTerminalIpc({
  isWindows,
  findOnPath,
  rememberLog,
  activeSshTerminalTarget,
  sshBinary,
  ensureBackend,
  getSshConnectionState,
  gatewayTarget,
  ensureGatewayBackend,
  localHermesCli
}: TerminalIpcDeps): TerminalIpcApi {
  const terminalSessions = new Map()

  function isExecutableFile(filePath) {
    if (!filePath || !path.isAbsolute(filePath)) {
      return false
    }

    try {
      fs.accessSync(filePath, fs.constants.X_OK)

      return true
    } catch {
      return false
    }
  }

  function posixShellSpec(shellPath) {
    const shellName = path.basename(shellPath)
    const interactiveArgs = shellName.includes('zsh') || shellName.includes('bash') ? ['-il'] : ['-i']

    return { args: interactiveArgs, command: shellPath, name: shellName }
  }

  // Windows PowerShell 5.1 ships at a fixed System32 path on every Windows box;
  // prefer it only after PowerShell 7+ (`pwsh`).
  function windowsPowerShellPath() {
    const systemRoot = process.env.SystemRoot || process.env.windir || 'C:\\Windows'
    const builtin = path.join(systemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')

    return isExecutableFile(builtin) ? builtin : findOnPath('powershell.exe')
  }

  // Map a resolved shell path to its spawn spec, picking interactive flags by
  // family: PowerShell drops its logo banner (so the prompt sits flush like the
  // POSIX shells), cmd needs nothing, and everything else (zsh/bash/fish/sh…)
  // gets POSIX interactive-login flags.
  function shellSpecFor(shellPath) {
    const name = path.basename(shellPath).toLowerCase()

    if (name.startsWith('pwsh') || name.startsWith('powershell')) {
      return { args: ['-NoLogo'], command: shellPath, name }
    }

    if (name.startsWith('cmd')) {
      return { args: [], command: shellPath, name }
    }

    return posixShellSpec(shellPath)
  }

  // Best installed Windows shell: PowerShell 7+ (`pwsh`), then Windows PowerShell
  // 5.1, then comspec/cmd.exe as the universal fallback.
  function windowsShellSpec() {
    const command =
      findOnPath('pwsh.exe') || findOnPath('pwsh') || windowsPowerShellPath() || process.env.COMSPEC || 'cmd.exe'

    return shellSpecFor(command)
  }

  // Resolve the interactive shell for the embedded terminal: an explicit user
  // override wins, otherwise auto-detect the best one installed for the platform.
  function terminalShellCommand() {
    // HERMES_DESKTOP_SHELL is the cross-platform escape hatch (a path or a bare
    // name on PATH); $SHELL is honored on POSIX, where it's the user's canonical
    // choice, but ignored on Windows, where it's usually a stray MSYS/Git path
    // node-pty can't spawn natively.
    const override = (process.env.HERMES_DESKTOP_SHELL || (isWindows ? '' : process.env.SHELL) || '').trim()

    if (override) {
      const resolved = isExecutableFile(override) ? override : findOnPath(override)

      if (resolved) {
        return shellSpecFor(resolved)
      }
    }

    if (isWindows) {
      return windowsShellSpec()
    }

    const shellPath = ['/bin/zsh', '/bin/bash', '/bin/sh'].find(candidate => isExecutableFile(candidate))

    return posixShellSpec(shellPath || '/bin/sh')
  }

  function safeTerminalCwd(cwd) {
    const candidate = path.resolve(String(cwd || app.getPath('home')))

    try {
      const stat = fs.statSync(candidate)

      return stat.isDirectory() ? candidate : path.dirname(candidate)
    } catch {
      return app.getPath('home')
    }
  }

  function terminalShellEnv() {
    const env = { ...process.env }

    // Electron is commonly launched through `pnpm run dev`; do not leak the
    // package manager's settings into a user's interactive shell (nvm/proto warn
    // loudly, and the user's own pnpm would inherit them). Package managers inject
    // lowercase names: a user's own NPM_CONFIG_REGISTRY stays.
    for (const key of Object.keys(env)) {
      if (key.startsWith('npm_config_') || key.startsWith('npm_package_') || key.startsWith('pnpm_config_')) {
        delete env[key]
      }
    }

    // Strip color/theme-detection vars that ride along when Electron is launched
    // from a non-tty agent shell (Cursor's runner sets NO_COLOR/FORCE_COLOR=0
    // /TERM=dumb; some terminals set COLORFGBG which would flip Hermes' TUI into
    // light-mode). Our PTY is a real xterm-compat terminal — force truecolor.
    delete env.NO_COLOR
    delete env.FORCE_COLOR
    delete env.COLORFGBG

    env.COLORTERM = 'truecolor'
    env.LC_CTYPE = terminalLcCtype(env)
    env.TERM = 'xterm-256color'
    env.TERM_PROGRAM = 'Hermes'
    env.TERM_PROGRAM_VERSION = app.getVersion()

    // Let a hermes/--tui launched in this pane know it's embedded in the desktop
    // GUI (build_environment_hints surfaces this). Distinct from HERMES_DESKTOP,
    // which marks the agent *backend* and gates cron/gateway behavior.
    env.HERMES_DESKTOP_TERMINAL = '1'

    return applyWindowsMsysBashEnvDefaults(env, isWindows)
  }

  function terminalChannel(id, suffix) {
    return `hermes:terminal:${id}:${suffix}`
  }

  // Best-effort read of a live PTY child's current working directory so a
  // reopened tab can restart the shell where the user last `cd`'d, instead of the
  // tab's original launch dir. Shell-agnostic (no prompt/OSC config needed) on
  // POSIX; Windows has no cheap per-process cwd query without a native module, so
  // it returns null and the caller falls back to the launch cwd.
  function readProcessCwd(pid) {
    return new Promise(resolve => {
      if (!Number.isInteger(pid) || pid <= 0) {
        resolve(null)

        return
      }

      if (process.platform === 'linux') {
        fs.promises
          .readlink(`/proc/${pid}/cwd`)
          .then(target => resolve(target || null))
          .catch(() => resolve(null))

        return
      }

      if (process.platform === 'darwin') {
        // lsof ships with macOS; -Fn emits the cwd fd's path on an `n<path>` line.
        execFile('lsof', ['-a', '-p', String(pid), '-d', 'cwd', '-Fn'], { timeout: 2000 }, (err, stdout) => {
          if (err) {
            resolve(null)

            return
          }

          const line = String(stdout || '')
            .split('\n')
            .find(entry => entry.startsWith('n'))

          resolve(line ? line.slice(1) : null)
        })

        return
      }

      resolve(null)
    })
  }

  function disposeTerminalSession(id: string) {
    const sessionInfo = terminalSessions.get(id)

    if (!sessionInfo) {
      return false
    }

    terminalSessions.delete(id)

    try {
      sessionInfo.pty.kill()
    } catch {
      // Process may already be gone.
    }

    return true
  }

  // SSH teardown: end every pane whose PTY rode the disconnected tunnel.
  // The tab survives as disconnected: Enter re-resolves its gateway, so an
  // edited gateway reconnects and a removed one says so.
  function disposeTerminalSessionsForSshScope(scope: string) {
    for (const [id, info] of [...terminalSessions.entries()]) {
      if (info.sshScope !== scope) {
        continue
      }

      info.outputGate.exit({ code: null, signal: 'disconnected' })

      // An attached tab got the exit and the flush already disposed the
      // session. An unattached one keeps its entry (as a normal exit does) so
      // the late attach still delivers `disconnected` and arms Enter; only the
      // shell stops now.
      if (terminalSessions.has(id)) {
        try {
          info.pty.kill()
        } catch {
          // Already gone.
        }
      }
    }
  }

  // App shutdown: kill every open PTY before environment teardown.
  function disposeAllTerminalSessions() {
    for (const [id, info] of [...terminalSessions.entries()]) {
      info.outputGate.silence()
      disposeTerminalSession(id)
    }
  }

  // node-pty's published tarball ships the POSIX `spawn-helper` without an exec
  // bit; the dev flow resolves node-pty straight from node_modules (nothing
  // chmods it there), so the first terminal spawn dies with `posix_spawnp
  // failed`. Restore the bit once, lazily, right before the first spawn. Packaged
  // builds already stage an executable copy, so this is a no-op there.
  let _spawnHelperEnsured = false

  function ensureNodePtySpawnHelper() {
    if (_spawnHelperEnsured || isWindows) {
      return
    }

    _spawnHelperEnsured = true

    try {
      const nodePtyRoot = path.dirname(require.resolve('node-pty/package.json'))
      const { fixed, errors } = ensureSpawnHelperExecutable(nodePtyRoot)

      for (const helperPath of fixed) {
        rememberLog(`[terminal] restored +x on node-pty spawn-helper: ${helperPath}`)
      }

      for (const failure of errors) {
        rememberLog(`[terminal] could not chmod spawn-helper ${failure.path}: ${failure.error}`)
      }
    } catch (error) {
      rememberLog(
        `[terminal] spawn-helper exec check skipped: ${error instanceof Error ? error.message : String(error)}`
      )
    }
  }

  // The tab's recorded gateway decides the host; only a tab that recorded none
  // follows the window-backend route.
  async function resolveStartTarget(
    owner: null | TerminalGatewayOwner,
    webContentsId: number
  ): Promise<{ sshTarget: null | SshTerminalTarget } | { unavailable: 'missing' | 'no-shell' }> {
    if (!owner) {
      return {
        sshTarget: await resolveTerminalConnectionForSender(webContentsId, activeSshTerminalTarget, ensureBackend)
      }
    }

    const target = await resolveTerminalConnection(
      () => gatewayTarget(owner),
      () => ensureGatewayBackend(owner)
    )

    if (target === 'missing' || target === 'no-shell') {
      return { unavailable: target }
    }

    return { sshTarget: target === 'local' ? null : target }
  }

  // Where the shell runs, so a tab that recorded no gateway adopts this one.
  function spawnedOn(owner: null | TerminalGatewayOwner, sshTarget: null | SshTerminalTarget): null | string {
    if (owner) {
      return owner.connectionId
    }

    return sshTarget ? getSshConnectionState(sshTarget.scope)?.registryConnectionId || null : 'local'
  }

  // Wire a spawned PTY to the renderer that asked for it and register it.
  function trackSession(event, id: string, ptyProcess, extra: Record<string, unknown>) {
    const send = (suffix, payload) => {
      if (event.sender.isDestroyed()) {
        return
      }

      event.sender.send(terminalChannel(id, suffix), payload)
    }

    const outputGate = createTerminalOutputGate({
      onExitFlushed: () => disposeTerminalSession(id),
      sendData: data => send('data', data),
      sendExit: payload => send('exit', payload)
    })

    terminalSessions.set(id, { outputGate, pty: ptyProcess, webContentsId: event.sender.id, ...extra })

    ptyProcess.onData(data => outputGate.data(data))
    ptyProcess.onExit(({ exitCode, signal }) => {
      outputGate.exit({ code: exitCode, signal: signal == null ? null : String(signal) })

      // The child is gone but node-pty keeps the /dev/ptmx master fd open
      // until kill() runs; release it here instead of waiting for the tab to
      // close (or the renderer to attach), or repeated `exit`s exhaust macOS
      // PTYs (#128942). The map entry stays until the gate flushes the
      // buffered exit, so a late attach still receives it.
      try {
        ptyProcess.kill()
      } catch {
        // Already reaped.
      }
    })
    event.sender.once('destroyed', () => disposeTerminalSession(id))
  }

  // An account-recovery PTY carries a sign-in: only the renderer that started
  // it may type into it, size it, read its cwd or end it.
  function sessionFor(event, id) {
    const sessionInfo = terminalSessions.get(String(id || ''))

    if (!sessionInfo || (sessionInfo.recovery && sessionInfo.webContentsId !== event?.sender?.id)) {
      return null
    }

    return sessionInfo
  }

  const ptyDimension = (value: unknown, fallback: number) =>
    Math.max(2, Number.parseInt(String(value || fallback), 10) || fallback)

  // The one process a recovery may start on its gateway, or why none can.
  async function recoveryLaunch(
    target: Exclude<TerminalGatewayTarget, 'missing' | 'no-shell' | 'pending'>,
    args: string[]
  ) {
    if (target === 'local') {
      const cli = localHermesCli ? await localHermesCli(args) : null

      // A shell shim would put the arguments on a shell command line; only a direct exec runs here.
      return cli?.command && !cli.shell
        ? { args: cli.args, command: cli.command, env: { ...terminalShellEnv(), ...cli.env } }
        : 'no-runtime'
    }

    const state = getSshConnectionState(target.scope)
    const posix = state?.remotePlatform === 'Linux' || state?.remotePlatform === 'Darwin'
    const remoteCommand = posix ? remoteRecoveryCommand(state, args) : null

    return remoteCommand
      ? {
          args: buildInteractiveSshArgs(target.ssh, '', undefined, remoteCommand),
          command: sshBinary(),
          env: terminalShellEnv()
        }
      : 'unsupported'
  }

  // Account recovery: run `hermes auth reauth <provider> <row>` on the gateway
  // that owns the row, and nothing else. The request names a gateway, a
  // provider and a row id; a request that fails any check starts no process,
  // and a gateway without a shell transport is reported, never replaced by
  // this device.
  ipcMain.handle('hermes:terminal:recovery:start', async (event, payload = {}) => {
    const refused = (unavailable: string) => ({ id: '', recovery: true, unavailable })
    const request = requestedRecovery(payload)
    const owner = requestedTerminalGateway(payload)
    const args = request && owner ? recoveryCliArgs(request, owner.profile) : null

    if (!request || !owner || !args) {
      return refused('invalid')
    }

    if (isWindows) {
      return refused('unsupported')
    }

    const target = await resolveTerminalConnection(
      () => gatewayTarget(owner),
      () => ensureGatewayBackend(owner)
    )

    if (target === 'missing' || target === 'no-shell') {
      return refused(target)
    }

    const launch = await recoveryLaunch(target, args)

    if (typeof launch === 'string') {
      return refused(launch)
    }

    ensureNodePtySpawnHelper()

    const ptyProcess = nodePty.spawn(launch.command, launch.args, {
      cols: ptyDimension(payload?.cols, 80),
      cwd: app.getPath('home'),
      env: launch.env,
      name: 'xterm-256color',
      rows: ptyDimension(payload?.rows, 24)
    })

    const id = crypto.randomUUID()

    trackSession(event, id, ptyProcess, {
      recovery: true,
      ...(target === 'local' ? {} : { sshScope: target.scope })
    })

    return { connectionId: owner.connectionId, id, recovery: true }
  })

  ipcMain.handle('hermes:terminal:start', async (event, payload = {}) => {
    ensureNodePtySpawnHelper()

    const id = crypto.randomUUID()
    const { args, command, name } = terminalShellCommand()
    const cwd = safeTerminalCwd(payload?.cwd)
    const cols = Math.max(2, Number.parseInt(String(payload?.cols || 80), 10) || 80)
    const rows = Math.max(2, Number.parseInt(String(payload?.rows || 24), 10) || 24)

    const owner = requestedTerminalGateway(payload)
    const resolved = await resolveStartTarget(owner, event.sender.id)

    // Removed gateway, or one with no shell transport: report it instead of
    // spawning a local shell the tab would label with the remote's name.
    if ('unavailable' in resolved) {
      return {
        connectionId: owner?.connectionId ?? null,
        cwd: null,
        id: '',
        shell: '',
        unavailable: resolved.unavailable
      }
    }

    const { sshTarget } = resolved
    const remote = Boolean(sshTarget)
    const remoteState = remote ? getSshConnectionState(sshTarget.scope) : null

    const remoteCommand =
      remoteState?.remotePlatform === 'Windows'
        ? buildWindowsInteractiveCommand(String(payload?.cwd || '').trim())
        : undefined

    const ptyProcess = remote
      ? nodePty.spawn(
          sshBinary(),
          buildInteractiveSshArgs(sshTarget.ssh, String(payload?.cwd || '').trim(), undefined, remoteCommand),
          { cols, cwd: app.getPath('home'), env: terminalShellEnv(), name: 'xterm-256color', rows }
        )
      : nodePty.spawn(command, args, { cols, cwd, env: terminalShellEnv(), name: 'xterm-256color', rows })

    trackSession(
      event,
      id,
      ptyProcess,
      remote ? { sshScope: sshTarget.scope, remoteCwd: String(payload?.cwd || '') } : {}
    )

    return { connectionId: spawnedOn(owner, sshTarget), cwd: remote ? null : cwd, id, shell: remote ? 'ssh' : name }
  })

  ipcMain.handle('hermes:terminal:attach', (event, id) => {
    const sessionInfo = terminalSessions.get(String(id || ''))

    if (!sessionInfo || sessionInfo.webContentsId !== event.sender.id) {
      return false
    }

    sessionInfo.outputGate.attach()

    return true
  })

  ipcMain.handle('hermes:terminal:write', (event, id, data) => {
    const sessionInfo = sessionFor(event, id)

    if (!sessionInfo) {
      return false
    }

    sessionInfo.pty.write(String(data || ''))

    return true
  })

  ipcMain.handle('hermes:terminal:resize', (event, id, size = {}) => {
    const sessionInfo = sessionFor(event, id)

    if (!sessionInfo) {
      return false
    }

    const cols = Math.max(2, Number.parseInt(String(size?.cols || 80), 10) || 80)
    const rows = Math.max(2, Number.parseInt(String(size?.rows || 24), 10) || 24)

    sessionInfo.pty.resize(cols, rows)

    return true
  })
  ipcMain.handle('hermes:terminal:cwd', async (event, id) => {
    const sessionInfo = sessionFor(event, id)

    if (!sessionInfo || sessionInfo.recovery) {
      return null
    }

    return sessionInfo.sshScope !== undefined ? null : readProcessCwd(sessionInfo.pty.pid)
  })

  ipcMain.handle('hermes:terminal:dispose', (event, id) =>
    sessionFor(event, id) ? disposeTerminalSession(String(id || '')) : false
  )

  return { disposeTerminalSession, disposeTerminalSessionsForSshScope, disposeAllTerminalSessions }
}
