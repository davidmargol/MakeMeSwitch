import { useEffect, useMemo, useState } from 'react'
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { Activity, ArrowDownUp, RefreshCw } from 'lucide-react'
import { api, formatBps, formatBytes, type InterfaceRow, type StatsPort } from '../api'
import { Badge, Card, Empty, PageHeader } from '../components/ui'
import { useStats } from '../stats'

interface Sample {
  ts: number
  port: string
  rx_bytes: number
  tx_bytes: number
  rx_packets: number
  tx_packets: number
  rx_errors: number
  tx_errors: number
}

const RANGES = [
  { label: '5 min', value: 300 },
  { label: '15 min', value: 900 },
  { label: '1 hour', value: 3600 },
  { label: '6 hours', value: 21600 },
]

export default function Monitoring() {
  const [range, setRange] = useState(900)
  const [port, setPort] = useState('')
  const [rows, setRows] = useState<InterfaceRow[]>([])
  const [samples, setSamples] = useState<Sample[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const { snapshot, history, connected } = useStats()

  useEffect(() => {
    api
      .get<{ interfaces: InterfaceRow[] }>('/api/v1/interfaces')
      .then((data) => {
        setRows(data.interfaces)
        if (!port && data.interfaces.length > 0) setPort(data.interfaces[0].name)
      })
      .catch((exc) => setError(exc.message))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const load = async () => {
    setLoading(true)
    try {
      const data = await api.get<{ samples: Sample[] }>(`/api/v1/stats/history?range=${range}${port ? `&port=${encodeURIComponent(port)}` : ''}`)
      setSamples(data.samples)
      setError(null)
    } catch (exc: any) {
      setError(exc.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    const timer = setInterval(load, 15000)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [range, port])

  const chartData = useMemo(() => {
    const points = samples.map((sample, index) => {
      const prev = samples[index - 1]
      const dt = prev ? Math.max(sample.ts - prev.ts, 0.5) : 0
      return {
        time: new Date(sample.ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
        rx: prev ? ((sample.rx_bytes - prev.rx_bytes) * 8) / dt : 0,
        tx: prev ? ((sample.tx_bytes - prev.tx_bytes) * 8) / dt : 0,
        errors: sample.rx_errors + sample.tx_errors,
      }
    })
    return points.filter((point) => Number.isFinite(point.rx) && point.rx >= 0)
  }, [samples])

  const livePorts: StatsPort[] = snapshot?.ports ?? []
  const selectedLive = livePorts.find((p) => p.name === port)
  const maxRate = Math.max(1, ...livePorts.map((p) => p.rates.rx_bps + p.rates.tx_bps))

  return (
    <div className="space-y-5">
      <PageHeader
        title="Traffic monitoring"
        subtitle="Live counters from OVS and historical samples stored locally"
        actions={
          <>
            <Badge tone={connected ? 'up' : 'warning'}>{connected ? 'live feed' : 'offline'}</Badge>
            <button className="btn" onClick={load} disabled={loading}>
              <RefreshCw size={15} className={loading ? 'animate-spin' : ''} /> Refresh
            </button>
          </>
        }
      />

      {error && <Card className="border-bad/40 p-4 text-sm text-bad">{error}</Card>}

      <div className="grid gap-4 xl:grid-cols-3">
        <Card className="p-4 xl:col-span-2">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div>
              <div className="text-sm font-semibold">{port || 'Select a port'}</div>
              <div className="text-xs text-muted">
                {selectedLive
                  ? `RX ${formatBps(selectedLive.rates.rx_bps)} · TX ${formatBps(selectedLive.rates.tx_bps)} (live)`
                  : 'historical throughput'}
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              <select className="input !py-1.5 w-44" value={port} onChange={(e) => setPort(e.target.value)}>
                {rows.map((row) => (
                  <option key={row.name} value={row.name}>
                    {row.name} {row.linux_if ? `(${row.linux_if})` : ''}
                  </option>
                ))}
              </select>
              {RANGES.map((item) => (
                <button
                  key={item.value}
                  className={`btn !px-2 !py-1 text-xs ${range === item.value ? 'border-accent/60 text-accent' : ''}`}
                  onClick={() => setRange(item.value)}
                >
                  {item.label}
                </button>
              ))}
            </div>
          </div>
          <div className="h-64">
            {chartData.length < 2 ? (
              <Empty title="Not enough samples yet" hint="Samples are stored every 10 seconds while the panel or API service is running." />
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chartData} margin={{ top: 4, right: 6, left: 0, bottom: 0 }}>
                  <defs>
                    <linearGradient id="monRx" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="rgb(var(--c-accent))" stopOpacity={0.35} />
                      <stop offset="100%" stopColor="rgb(var(--c-accent))" stopOpacity={0} />
                    </linearGradient>
                    <linearGradient id="monTx" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="rgb(var(--c-accent2))" stopOpacity={0.35} />
                      <stop offset="100%" stopColor="rgb(var(--c-accent2))" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid stroke="rgb(var(--c-edge))" strokeDasharray="3 3" vertical={false} />
                  <XAxis dataKey="time" tick={{ fontSize: 10, fill: 'rgb(var(--c-muted))' }} tickLine={false} axisLine={false} minTickGap={30} />
                  <YAxis
                    tick={{ fontSize: 10, fill: 'rgb(var(--c-muted))' }}
                    tickLine={false}
                    axisLine={false}
                    tickFormatter={formatBps}
                    width={70}
                  />
                  <Tooltip
                    contentStyle={{ background: 'rgb(var(--c-panel))', border: '1px solid rgb(var(--c-edge))', borderRadius: 8, fontSize: 12 }}
                    formatter={(value: number, name) => [formatBps(value), name === 'rx' ? 'RX' : 'TX']}
                  />
                  <Area type="monotone" dataKey="rx" stroke="rgb(var(--c-accent))" fill="url(#monRx)" strokeWidth={1.5} isAnimationActive={false} />
                  <Area type="monotone" dataKey="tx" stroke="rgb(var(--c-accent2))" fill="url(#monTx)" strokeWidth={1.5} isAnimationActive={false} />
                </AreaChart>
              </ResponsiveContainer>
            )}
          </div>
        </Card>

        <Card className="p-4">
          <div className="mb-3 flex items-center gap-1.5 text-sm font-semibold">
            <Activity size={15} /> Aggregate (live)
          </div>
          <div className="h-40">
            {history.length < 2 ? (
              <Empty title="Waiting for traffic…" />
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={history.map((point) => ({ t: point.t, total: point.rx + point.tx }))}>
                  <CartesianGrid stroke="rgb(var(--c-edge))" strokeDasharray="3 3" vertical={false} />
                  <XAxis dataKey="t" hide />
                  <YAxis hide />
                  <Tooltip
                    contentStyle={{ background: 'rgb(var(--c-panel))', border: '1px solid rgb(var(--c-edge))', borderRadius: 8, fontSize: 12 }}
                    labelFormatter={() => ''}
                    formatter={(value: number) => [formatBps(value), 'total']}
                  />
                  <Area type="monotone" dataKey="total" stroke="rgb(var(--c-accent))" fill="rgb(var(--c-accent) / 0.15)" strokeWidth={1.5} isAnimationActive={false} />
                </AreaChart>
              </ResponsiveContainer>
            )}
          </div>
          <div className="mt-2 grid grid-cols-2 gap-2 text-center">
            <div className="rounded-lg border border-edge bg-panel2 px-2 py-1.5">
              <div className="text-[10px] uppercase tracking-wider text-muted">RX</div>
              <div className="text-sm font-medium">{formatBps(snapshot?.totals.rx_bps ?? 0)}</div>
            </div>
            <div className="rounded-lg border border-edge bg-panel2 px-2 py-1.5">
              <div className="text-[10px] uppercase tracking-wider text-muted">TX</div>
              <div className="text-sm font-medium">{formatBps(snapshot?.totals.tx_bps ?? 0)}</div>
            </div>
          </div>
        </Card>
      </div>

      <Card className="overflow-hidden">
        <div className="flex items-center justify-between border-b border-edge px-4 py-3">
          <div className="text-sm font-semibold">Per-port counters</div>
          <div className="flex items-center gap-1.5 text-[11px] text-muted">
            <ArrowDownUp size={12} /> cumulative since port creation
          </div>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[860px]">
            <thead className="bg-panel2/60">
              <tr>
                <th className="th">Port</th>
                <th className="th">Link</th>
                <th className="th">RX rate</th>
                <th className="th">TX rate</th>
                <th className="th">RX / TX bytes</th>
                <th className="th">Packets</th>
                <th className="th">Errors</th>
                <th className="th">Dropped</th>
                <th className="th w-40">Activity</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-edge">
              {livePorts.map((p) => {
                const total = p.rates.rx_bps + p.rates.tx_bps
                return (
                  <tr key={p.name} className="hover:bg-panel2/50">
                    <td className="td font-medium">{p.name}</td>
                    <td className="td">
                      <Badge tone={p.status}>{p.status}</Badge>
                    </td>
                    <td className="td mono text-xs">{formatBps(p.rates.rx_bps)}</td>
                    <td className="td mono text-xs">{formatBps(p.rates.tx_bps)}</td>
                    <td className="td mono text-[11px] text-muted">
                      {formatBytes(p.stats.rx_bytes ?? 0)} / {formatBytes(p.stats.tx_bytes ?? 0)}
                    </td>
                    <td className="td mono text-[11px] text-muted">
                      {Math.round(p.stats.rx_packets ?? 0)} / {Math.round(p.stats.tx_packets ?? 0)}
                    </td>
                    <td className="td text-xs">
                      <span className={(p.stats.rx_errors ?? 0) + (p.stats.tx_errors ?? 0) > 0 ? 'text-bad' : 'text-muted'}>
                        {Math.round((p.stats.rx_errors ?? 0) + (p.stats.tx_errors ?? 0))}
                      </span>
                    </td>
                    <td className="td text-xs text-muted">{Math.round((p.stats.rx_dropped ?? 0) + (p.stats.tx_dropped ?? 0))}</td>
                    <td className="td">
                      <div className="h-1.5 overflow-hidden rounded bg-edge/60">
                        <div className="h-full bg-accent" style={{ width: `${Math.min(100, (total / maxRate) * 100)}%` }} />
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          {livePorts.length === 0 && <Empty title="No port statistics yet" hint="Statistics appear once ports are assigned to the switch." />}
        </div>
      </Card>
    </div>
  )
}
