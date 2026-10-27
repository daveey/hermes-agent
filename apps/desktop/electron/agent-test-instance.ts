// A worker that reproduces a Desktop bug runs its own instance (`electron .` from a checkout, or
// a packaged build with a scratch `--user-data-dir`). That instance showed up as a second Hermes
// app with its own Dock icon and focused windows on the owner's screen (2026-10-27, card
// t_ed201037, pid 26918). It is driven over CDP, so it needs no Dock icon and no visible window.
// The owner's own Desktop relaunched from a worker (a worker's `hermes update`, mac-fixer) stays
// visible: it is packaged and uses the default profile. HERMES_DESKTOP_AGENT_VISIBLE=1 opts a
// test instance back in. [daveey]

export interface AgentTestInstanceInput {
  /** Keys scrubLeakedWorkerEnv() removed: non-empty means a worker launched this process. */
  leakedWorkerEnv: readonly string[]
  isPackaged: boolean
  argv: readonly string[]
  env: NodeJS.ProcessEnv
}

export function isHiddenAgentTestInstance({ leakedWorkerEnv, isPackaged, argv, env }: AgentTestInstanceInput): boolean {
  if (env.HERMES_DESKTOP_AGENT_VISIBLE === '1' || leakedWorkerEnv.length === 0) {
    return false
  }

  return !isPackaged || argv.some(arg => arg === '--user-data-dir' || arg.startsWith('--user-data-dir='))
}

interface HideableWindow {
  show: () => void
  showInactive: () => void
  focus: () => void
  moveTop: () => void
}

interface HideableApp {
  on: (event: 'browser-window-created', listener: (event: unknown, win: HideableWindow) => void) => unknown
  setActivationPolicy?: (policy: 'regular' | 'accessory' | 'prohibited') => void
  dock?: { hide: () => void }
  focus: (options?: { steal: boolean }) => void
}

/** No Dock icon or menu bar (macOS accessory policy), and no window is ever shown or focused.
 *  Windows still render (they are created with `show: false`), so CDP drives and screenshots them. */
export function hideAgentTestInstance(app: HideableApp, platform: NodeJS.Platform = process.platform): void {
  if (platform === 'darwin') {
    app.setActivationPolicy?.('accessory')
    app.dock?.hide()
  }

  app.focus = () => {}
  app.on('browser-window-created', (_event, win) => {
    const noop = () => {}

    win.show = noop
    win.showInactive = noop
    win.focus = noop
    win.moveTop = noop
  })
}
