import { useCallback, useState } from 'react'

/** Persist only the sidebar navigation disclosure state. */
export function useSidebarNavCollapsed() {
  const [navCollapsed, setNavCollapsed] = useState(() => {
    try {
      return localStorage.getItem('sidebar.nav.collapsed') === '1'
    } catch {
      return false
    }
  })

  const toggleNavCollapsed = useCallback(() => {
    setNavCollapsed(prev => {
      const next = !prev

      try {
        localStorage.setItem('sidebar.nav.collapsed', next ? '1' : '0')
      } catch {
        // ignore quota / disabled storage
      }

      return next
    })
  }, [])

  return { navCollapsed, toggleNavCollapsed }
}
