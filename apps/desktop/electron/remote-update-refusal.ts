// [daveey] The phrase every "remote update pauses SSH startup" refusal carries. The renderer only sees the refusal
// as text after IPC (src/app/gateway/remote-update-wait.ts matches it), so producers and the matcher share this
// one constant instead of each spelling it. Dependency-free: the renderer bundle imports it.
export const REMOTE_UPDATE_PAUSED_PHRASE = 'SSH startup is paused'
