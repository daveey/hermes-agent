/** Supported read-only ESM entry. Import the manifest-pinned URL in the SAME operation realm.
 * Never reach this binding through the mutable SDK host/global object.
 * Serving process/build custody and lifecycle fences remain the consumer's responsibility.
 */
export { readSessionTasksLoadedIdentityJson } from './contrib/runtime-loader'
