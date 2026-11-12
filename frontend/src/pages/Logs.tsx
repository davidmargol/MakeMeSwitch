import { useEffect, useState } from 'react'
import { api, hasRole, timeAgo, type AuditRow, type EventRow, type Session } from '../api'
import { Badge, Card, Empty, PageHeader } from '../components/ui'

export default function Logs({ session }: { session: Session }) {
  const [events, setEvents] = useState<EventRow[]>([])
  const [audit, setAudit] = useState<AuditRow[]>([])
  const [tab, setTab] = useState<'events' | 'audit'>('events')
  const [error, setError] = useState<string | null>(null)

  const load = async () => {
    try {
      const data = await api.get<{ events: EventRow[] }>('/api/v1/events?limit=100')
      setEvents(data.events)
      if (hasRole(session, 'admin')) {
        const auditData = await api.get<{ entries: AuditRow[] }>('/api/v1/audit?limit=100')
        setAudit(auditData.entries)
      }
      setError(null)
    } catch (exc: any) {
      setError(exc.message)
    }
  }

  useEffect(() => {
    load()
    const timer = setInterval(load, 10000)
    return () => clearInterval(timer)
  }, [])

  return (
    <div className="space-y-5">
      <PageHeader title="Logs & events" subtitle="System events from the engine and the authenticated audit trail" />

      {error && <Card className="border-bad/40 p-4 text-sm text-bad">{error}</Card>}

      <Card className="overflow-hidden">
        <div className="flex items-center gap-1 border-b border-edge px-3 py-2">
          <button
            className={`rounded-md px-3 py-1.5 text-xs font-medium ${tab === 'events' ? 'bg-accent/10 text-accent' : 'text-muted hover:text-ink'}`}
            onClick={() => setTab('events')}
          >
            System events
          </button>
          {hasRole(session, 'admin') && (
            <button
              className={`rounded-md px-3 py-1.5 text-xs font-medium ${tab === 'audit' ? 'bg-accent/10 text-accent' : 'text-muted hover:text-ink'}`}
              onClick={() => setTab('audit')}
            >
              Audit log
            </button>
          )}
        </div>

        {tab === 'events' ? (
          <div className="divide-y divide-edge">
            {events.map((event) => (
              <div key={event.id} className="flex items-start gap-3 px-4 py-2.5">
                <Badge tone={event.severity}>{event.severity}</Badge>
                <div className="min-w-0 flex-1">
                  <div className="text-sm">{event.message}</div>
                  <div className="text-[11px] text-muted">
                    {event.source} · {timeAgo(event.ts)}
                  </div>
                </div>
                <div className="mono shrink-0 text-[10px] text-muted">{new Date(event.ts * 1000).toLocaleString()}</div>
              </div>
            ))}
            {events.length === 0 && <Empty title="No events" />}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[820px]">
              <thead className="bg-panel2/60">
                <tr>
                  <th className="th">Time</th>
                  <th className="th">User</th>
                  <th className="th">IP</th>
                  <th className="th">Action</th>
                  <th className="th">Target</th>
                  <th className="th">Result</th>
                  <th className="th">Detail</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-edge">
                {audit.map((row) => (
                  <tr key={row.id} className="hover:bg-panel2/50">
                    <td className="td mono text-[11px] text-muted">{new Date(row.ts * 1000).toLocaleString()}</td>
                    <td className="td text-sm">{row.username}</td>
                    <td className="td mono text-[11px] text-muted">{row.ip ?? '—'}</td>
                    <td className="td text-xs">{row.action}</td>
                    <td className="td text-xs text-muted">{row.target ?? '—'}</td>
                    <td className="td">
                      <Badge tone={row.result}>{row.result}</Badge>
                    </td>
                    <td className="td max-w-[260px] truncate text-[11px] text-muted">{row.detail ?? ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {audit.length === 0 && <Empty title="No audit entries" />}
          </div>
        )}
      </Card>
    </div>
  )
}
