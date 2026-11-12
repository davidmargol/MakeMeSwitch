import React, { createContext, useContext, useEffect, useRef, useState } from 'react'
import type { StatsSnapshot } from './api'

interface StatsContextValue {
  snapshot: StatsSnapshot | null
  connected: boolean
  history: { t: number; rx: number; tx: number }[]
}

const StatsContext = createContext<StatsContextValue>({ snapshot: null, connected: false, history: [] })

export function StatsProvider({ children }: { children: React.ReactNode }) {
  const [snapshot, setSnapshot] = useState<StatsSnapshot | null>(null)
  const [connected, setConnected] = useState(false)
  const [history, setHistory] = useState<{ t: number; rx: number; tx: number }[]>([])
  const socketRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    let closed = false
    let retryTimer: ReturnType<typeof setTimeout>
    let pingTimer: ReturnType<typeof setInterval>

    const connect = () => {
      if (closed) return
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
      const socket = new WebSocket(`${proto}://${window.location.host}/api/v1/ws/stats`)
      socketRef.current = socket
      socket.onopen = () => {
        setConnected(true)
        pingTimer = setInterval(() => {
          if (socket.readyState === WebSocket.OPEN) socket.send('ping')
        }, 25000)
      }
      socket.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data)
          if (data.type === 'pong') return
          if (!data.ports) return
          setSnapshot(data as StatsSnapshot)
          setHistory((prev) => {
            const next = [...prev, { t: data.ts, rx: data.totals.rx_bps, tx: data.totals.tx_bps }]
            return next.slice(-90)
          })
        } catch {
          /* ignore malformed frames */
        }
      }
      socket.onclose = () => {
        setConnected(false)
        clearInterval(pingTimer)
        if (!closed) retryTimer = setTimeout(connect, 3000)
      }
      socket.onerror = () => socket.close()
    }

    connect()
    return () => {
      closed = true
      clearTimeout(retryTimer)
      clearInterval(pingTimer)
      socketRef.current?.close()
    }
  }, [])

  return <StatsContext.Provider value={{ snapshot, connected, history }}>{children}</StatsContext.Provider>
}

export function useStats() {
  return useContext(StatsContext)
}
