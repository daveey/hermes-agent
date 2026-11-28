/** Drop OS files onto a task (board card or open task dialog) to attach them.
 *  Only real file drags count — a card being moved between lanes carries
 *  `text/plain`, never `Files` — so lane moves and attachment drops never
 *  collide. Uploads go one at a time through the existing attachment endpoint,
 *  so the backend's per-file size cap and name sanitising still apply. */

import { type DragEvent as ReactDragEvent, useRef, useState } from 'react'

import { type TaskOwner, uploadAttachment } from './api'

export const isFileDrag = (dt: DataTransfer | null | undefined) => !!dt && Array.from(dt.types ?? []).includes('Files')

/** Sequential uploads; resolves with the count stored. The first failure
 *  rejects (earlier files stay attached — the caller's refresh shows them). */
export async function uploadFiles(taskId: string, files: readonly File[], owner?: TaskOwner): Promise<number> {
  let stored = 0

  for (const file of files) {
    await uploadAttachment(
      taskId,
      {
        bytes: await file.arrayBuffer(),
        contentType: file.type || undefined,
        filename: file.name
      },
      owner
    )
    stored += 1
  }

  return stored
}

/** Drag handlers + hover state for a file drop zone. Enter/leave are counted
 *  so crossing child elements doesn't flicker the highlight. Handlers stop
 *  propagation on file drags so an enclosing lane never sees them. */
export function useFileDrop(onFiles: (files: File[]) => void, enabled = true) {
  const [over, setOver] = useState(false)
  const depth = useRef(0)

  const reset = () => {
    depth.current = 0
    setOver(false)
  }

  const handlers = {
    onDragEnter: (event: ReactDragEvent<HTMLElement>) => {
      if (!enabled || !isFileDrag(event.dataTransfer)) {
        return
      }

      event.preventDefault()
      event.stopPropagation()
      depth.current += 1
      setOver(true)
    },
    onDragLeave: (event: ReactDragEvent<HTMLElement>) => {
      if (!enabled || !isFileDrag(event.dataTransfer)) {
        return
      }

      event.stopPropagation()
      depth.current = Math.max(0, depth.current - 1)

      if (depth.current === 0) {
        setOver(false)
      }
    },
    onDragOver: (event: ReactDragEvent<HTMLElement>) => {
      if (!enabled || !isFileDrag(event.dataTransfer)) {
        return
      }

      event.preventDefault()
      event.stopPropagation()
      event.dataTransfer.dropEffect = 'copy'
    },
    onDrop: (event: ReactDragEvent<HTMLElement>) => {
      if (!enabled || !isFileDrag(event.dataTransfer)) {
        return
      }

      event.preventDefault()
      event.stopPropagation()
      reset()
      const files = Array.from(event.dataTransfer.files ?? [])

      if (files.length > 0) {
        onFiles(files)
      }
    }
  }

  return { handlers, over }
}
