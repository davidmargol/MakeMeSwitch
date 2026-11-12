import { useEffect, useRef } from 'react'
import { Terminal as XTerm } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { PageHeader, Card, Badge } from '../components/ui'
import { useStats } from '../stats'

export default function TerminalPage() {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const { connected } = useStats()

  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const term = new XTerm({
      cursorBlink: true,
      fontFamily: 'JetBrains Mono, ui-monospace, Menlo, monospace',
      fontSize: 13,
      lineHeight: 1.25,
      convertEol: true,
      scrollback: 4000,
      theme: {
        background: '#070b14',
        foreground: '#e6edf7',
        cursor: '#22d3ee',
        selectionBackground: 'rgba(34,211,238,0.25)',
        black: '#0b1220',
        red: '#f87171',
        green: '#34d399',
        yellow: '#fbbf24',
        blue: '#3b82f6',
        magenta: '#c084fc',
        cyan: '#22d3ee',
        white: '#e6edf7',
        brightBlack: '#5b6b82',
      },
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(container)
    fit.fit()
    term.focus()

    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const socket = new WebSocket(`${proto}://${window.location.host}/api/v1/ws/terminal`)
    let closed = false

    socket.onopen = () => {
      fit.fit()
      sendResize()
      socket.send(JSON.stringify({ type: 'data', data: '\r' }))
    }

    socket.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data)
        if (message.type === 'data') term.write(message.data)
      } catch {
        term.write(event.data)
      }
    }

    socket.onclose = () => {
      if (!closed) term.write('\r\n\x1b[33m[session closed]\x1b[0m\r\n')
    }

    const sendResize = () => {
      if (socket.readyState !== WebSocket.OPEN) return
      socket.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
    }

    const dataDisposable = term.onData((data) => {
      if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'data', data }))
    })

    const onResize = () => {
      try {
        fit.fit()
        sendResize()
      } catch {
        /* ignore */
      }
    }
    const observer = new ResizeObserver(onResize)
    observer.observe(container)
    window.addEventListener('resize', onResize)

    return () => {
      closed = true
      observer.disconnect()
      window.removeEventListener('resize', onResize)
      dataDisposable.dispose()
      socket.close()
      term.dispose()
    }
  }, [])

  return (
    <div className="space-y-4">
      <PageHeader
        title="Web terminal"
        subtitle="Authenticated session running the real MakeMeSwitch CLI — not a simulated shell"
        actions={<Badge tone={connected ? 'up' : 'warning'}>{connected ? 'stats live' : 'stats offline'}</Badge>}
      />
      <Card className="overflow-hidden">
        <div className="flex items-center gap-2 border-b border-edge bg-panel2/60 px-3 py-2 text-[11px] text-muted">
          <span className="h-2.5 w-2.5 rounded-full bg-bad/70" />
          <span className="h-2.5 w-2.5 rounded-full bg-warn/70" />
          <span className="h-2.5 w-2.5 rounded-full bg-ok/70" />
          <span className="ml-2 mono">makemeswitch — restricted CLI</span>
        </div>
        <div ref={containerRef} className="h-[68vh] w-full bg-[#070b14] p-2" />
      </Card>
      <p className="px-1 text-[11px] text-muted">
        The terminal is bound to your authenticated web session and runs the MakeMeSwitch CLI interpreter with your role. It does not expose a Linux shell.
      </p>
    </div>
  )
}
