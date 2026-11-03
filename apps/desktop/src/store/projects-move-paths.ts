// Pure path / error helpers for moving sessions into projects across gateways.

export const labelKey = (label: string): string => label.trim().toLowerCase()

/** `session.workspace.move`'s refusal of a folder that doesn't exist on that host. */
export const MISSING_WORKDIR_RE = /working directory does not exist/
// projects_db.create_project's refusal when another project already owns the folder.
export const DUPLICATE_FOLDER_RE = /folder already belongs to project '[^']*' \((p_[0-9a-f]+)\)/

/** Shell word for a path; a `~/` prefix stays expandable. */
export function shellPath(path: string): string {
  const quote = (s: string) => `'${s.replace(/'/g, `'\\''`)}'`

  return path.startsWith('~/') ? `"$HOME"/${quote(path.slice(2))}` : quote(path)
}

/**
 * `/Users/x/code/a`, `/home/x/code/a`, `/root/code/a` or `/opt/data/code/a`
 * (the Docker image's home) → `~/code/a`, so it lands in the other host's home.
 */
export function homeRelativePath(path: string): string {
  const m = path.match(/^(?:\/Users|\/home)\/[^/]+(\/.*)?$/) ?? path.match(/^\/(?:root|opt\/data)(\/.*)?$/)

  return m ? `~${m[1] ?? ''}` : path
}

/** Default folder for a project created by name alone: `~/code/<slug>`. */
export function newProjectFolder(name: string): string {
  const slug =
    name
      .trim()
      .toLowerCase()
      .replace(/[^\p{L}\p{N}._-]+/gu, '-')
      .replace(/^[-.]+|-+$/g, '') || 'project'

  return `~/code/${slug}`
}

export interface ShellResult {
  code: number
  stderr: string
  stdout: string
}
