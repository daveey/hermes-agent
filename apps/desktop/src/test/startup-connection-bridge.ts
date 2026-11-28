import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { runInNewContext } from 'node:vm'

import ts from 'typescript'

import * as readiness from '../../../shared/src/backend-readiness-timeout'

function compile(source: string) {
  return ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
  }).outputText
}

/** Real main registrations + whole preload; only unrelated Electron bootstrap is excluded.
 * Clone successful IPC/contextBridge values and deliberately discard thrown Error properties. */
export function startupConnectionBridge(connect: (...args: unknown[]) => Promise<unknown>) {
  const handlers = new Map<string, (event: unknown, payload: unknown) => Promise<unknown>>()
  const source = ts.createSourceFile(
    'main.ts',
    readFileSync(join(__dirname, '../../electron/main.ts'), 'utf8'),
    ts.ScriptTarget.Latest,
    true
  )
  const channels = ['hermes:connection', 'hermes:connection:for']

  const statements = source.statements.filter(statement => {
    if (!ts.isExpressionStatement(statement) || !ts.isCallExpression(statement.expression)) {
      return false
    }
    const call = statement.expression

    return (
      ts.isPropertyAccessExpression(call.expression) &&
      call.expression.expression.getText(source) === 'ipcMain' &&
      call.expression.name.text === 'handle' &&
      ts.isStringLiteral(call.arguments[0]) &&
      channels.includes(call.arguments[0].text)
    )
  })

  runInNewContext(compile(statements.map(statement => statement.getText(source)).join('\n')), {
    ipcMain: {
      handle: (channel: string, handler: (event: unknown, payload: unknown) => Promise<unknown>) =>
        handlers.set(channel, handler)
    },
    windowConnectionRoutes: new Map(),
    primaryProfileKey: () => 'default',
    resolveDesktopConnectionRequest: (profile?: string) => ({ profile: profile ?? 'default' }),
    desktopConnectionRequestInheritsPrimary: () => true,
    readDesktopConnectionsRegistry: () => ({ primary: 'local' }),
    registryDialConnectionId: (id: string) => id,
    connectDesktopProfileRoute: connect,
    captureStartupDeadline: readiness.captureStartupDeadline
  })
  let bridge: NonNullable<Window['hermesDesktop']> | undefined
  runInNewContext(compile(readFileSync(join(__dirname, '../../electron/preload.ts'), 'utf8')), {
    exports: {},
    process: { platform: 'linux' },
    require: (name: string) => {
      if (name === '../../shared/src/backend-readiness-timeout') {
        return readiness
      }

      if (name === './window-controls') {
        return { customWindowControlsEnabled: () => false }
      }

      if (name === './api-expected-404') {
        return { unwrapExpectedNotFound: (value: unknown) => value }
      }

      if (name !== 'electron') {
        throw new Error(`Unexpected preload dependency: ${name}`)
      }

      return {
        contextBridge: {
          exposeInMainWorld: (key: string, api: NonNullable<Window['hermesDesktop']>) => {
            if (key === 'hermesDesktop') {
              bridge = api
            }
          }
        },
        ipcRenderer: {
          sendSync: () => undefined,
          invoke: async (channel: string, payload: unknown) => {
            const handler = handlers.get(channel)

            if (!handler) {
              throw new Error(`Unregistered channel ${channel}`)
            }

            try {
              return structuredClone(await handler({ sender: { id: 1 } }, payload))
            } catch (error) {
              throw new Error(`Error invoking remote method '${channel}': Error: ${(error as Error).message}`)
            }
          }
        },
        webFrame: {},
        webUtils: {}
      }
    }
  })

  return {
    getStartupConnection: async (route?: { connectionId?: string | null; profile?: string | null } | null) =>
      structuredClone(await bridge!.getStartupConnection!(route)),
    getConnection: (profile?: string) => bridge!.getConnection(profile),
    getConnectionFor: (route: { connectionId?: string | null; profile?: string | null }) =>
      bridge!.getConnectionFor!(route)
  }
}
