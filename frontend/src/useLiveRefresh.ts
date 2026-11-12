import { useCallback, useEffect, useRef } from 'react'

// Polling also observes changes made by the separate CLI process. Mutations in
// this tab and returning from a terminal/tab trigger an immediate refresh.
export function useLiveRefresh(fetchData: () => Promise<void>, interval = 5000) {
  const latest = useRef(fetchData)
  latest.current = fetchData
  const active = useRef(false)
  const pending = useRef(false)
  const mounted = useRef(false)
  const refresh = useCallback(async () => {
    if (active.current) { pending.current = true; return }
    active.current = true
    try {
      do {
        pending.current = false
        await latest.current()
      } while (pending.current && mounted.current)
    } finally { active.current = false }
  }, [])
  useEffect(() => {
    mounted.current = true
    void refresh()
    const visibleRefresh = () => { if (!document.hidden) void refresh() }
    const timer = window.setInterval(visibleRefresh, interval)
    window.addEventListener('focus', visibleRefresh)
    window.addEventListener('osw:config-changed', visibleRefresh)
    document.addEventListener('visibilitychange', visibleRefresh)
    return () => {
      mounted.current = false
      window.clearInterval(timer)
      window.removeEventListener('focus', visibleRefresh)
      window.removeEventListener('osw:config-changed', visibleRefresh)
      document.removeEventListener('visibilitychange', visibleRefresh)
    }
  }, [refresh, interval])
  return refresh
}
