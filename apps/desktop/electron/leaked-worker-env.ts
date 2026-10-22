// A Desktop launched from inside a kanban worker / delegate child (a worker's `hermes update`
// reopening the app with `open`) inherits that worker's identity env. Every backend, terminal
// and CLI the Desktop spawns would then believe it is that worker's delegate child: kanban tools
// refuse ("delegate_task child contexts cannot mutate Kanban") and the board is pinned to the
// worker's DB (2026-10-21, Hermes.app pid 59331). The Desktop is never a worker, so drop them at
// startup. Mirrors hermes_cli/top_level_env.py. [daveey]

// Presence of any of these means the env was inherited from a worker or delegate child.
export const WORKER_MARKER_KEYS = [
  'HERMES_DELEGATED_CHILD_CONTEXT',
  'HERMES_KANBAN_TASK',
  'HERMES_KANBAN_WORKSPACE',
  'HERMES_KANBAN_RUN_ID',
  'HERMES_KANBAN_CLAIM_LOCK'
] as const

// Board pins are only dropped together with a marker: set alone they are an operator choice.
export const WORKER_ENV_KEYS = [
  ...WORKER_MARKER_KEYS,
  'HERMES_KANBAN_BRANCH',
  'HERMES_KANBAN_GOAL_MODE',
  'HERMES_KANBAN_GOAL_MAX_TURNS',
  'HERMES_KANBAN_DB',
  'HERMES_KANBAN_BOARD',
  'HERMES_KANBAN_WORKSPACES_ROOT'
] as const

/** Remove leaked worker identity keys from `env` in place; returns the removed key names. */
export function scrubLeakedWorkerEnv(env: NodeJS.ProcessEnv = process.env): string[] {
  if (!WORKER_MARKER_KEYS.some(key => Boolean(env[key]))) {
    return []
  }

  const removed = WORKER_ENV_KEYS.filter(key => key in env)

  for (const key of removed) {
    delete env[key]
  }

  return removed
}
