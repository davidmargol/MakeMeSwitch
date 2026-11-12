import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { Activity, Cpu, HardDrive, Layers, Network, Server, Thermometer, Timer, Zap } from 'lucide-react'
import { api, formatBps, formatBytes, formatUptime, timeAgo, type SystemInfo } from '../api'
import { Badge, Card, Empty, PageHeader, StatCard } from '../components/ui'
import { useStats } from '../stats'

export default function Dashboard({ session }: { session: { username: string; role: string } }) {
  const [info, setInfo] = useState<SystemInfo | null>(null)
  const [error, setError] = useState<string | null>(null)
  const { snapshot, connected, history } = useStats()

  const load = () =>
    api
      .get<SystemInfo>('/api/v1/system')
      .then(setInfo)
      .catch((exc) => setError(exc.message))

  useEffect(() => {
    load()
    const timer = setInterval(load, 10000)
    return () => clearInterval(timer)
  }, [])

  if (error) return <Card className="p-5 text-sm text-bad">Unable to load system information: {error}</Card>
  if (!info) return <Empty title="Loading system information…" />

  const chartData = history.map((point) => ({
    time: new Date(point.t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }),
    rx: point.rx,
    tx: point.tx,
  }))

  const activePorts = snapshot?.system.ports_active ?? info.ports_active
  const totalPorts = info.ports_total
  const traffic = snapshot?.totals

  return (
    <div className="space-y-5">
      <PageHeader
        title={info.config_hostname}
        subtitle={`${info.platform} · kernel ${info.kernel} · ${session.role}`}
        actions={
          <>
            <Badge tone={info.ovs.running ? 'up' : 'down'}>{info.ovs.running ? 'OVS running' : 'OVS down'}</Badge>
            <Badge tone={info.unsaved_changes ? 'warning' : 'success'}>{info.unsaved_changes ? 'unsaved changes' : 'saved'}</Badge>
            <Link to="/ports" className="btn-primary">
              <Network size={15} /> Switch ports
            </Link>
          </>
        }
      />

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard label="Hostname" value={info.config_hostname} sub={`${info.project} ${info.makemeswitch_version}`} icon={<Server size={18} />} />
        <StatCard
          label="Uptime"
          value={formatUptime(snapshot?.system.uptime_seconds ?? info.uptime_seconds)}
          sub="host uptime"
          icon={<Timer size={18} />}
        />
        <StatCard
          label="CPU"
          value={`${Math.round(snapshot?.system.cpu_percent ?? info.cpu_percent)}%`}
          sub={`load ${info.load.join(' / ')} · ${info.cpu_count} cores`}
          icon={<Cpu size={18} />}
          tone={(snapshot?.system.cpu_percent ?? info.cpu_percent) > 85 ? 'text-warn' : 'text-ink'}
        />
        <StatCard
          label="Memory"
          value={`${Math.round(snapshot?.system.memory_percent ?? info.memory_percent)}%`}
          sub={`${formatBytes(info.memory_total - info.memory_available)} of ${formatBytes(info.memory_total)}`}
          icon={<HardDrive size={18} />}
        />
        <StatCard
          label="Temperature"
          value={info.temperature_c !== null ? `${info.temperature_c} °C` : 'n/a'}
          sub={info.temperature_c === null ? 'no sensor exposed' : 'system sensor'}
          icon={<Thermometer size={18} />}
        />
        <StatCard
          label="Ports active"
          value={`${activePorts}/${totalPorts}`}
          sub={`${info.summary.ports_assigned} assigned · ${info.summary.trunks} trunk`}
          icon={<Network size={18} />}
        />
        <StatCard label="VLANs" value={info.summary.vlans} sub={`bridge ${info.bridge}`} icon={<Layers size={18} />} />
        <StatCard
          label="Traffic"
          value={formatBps((traffic?.rx_bps ?? 0) + (traffic?.tx_bps ?? 0))}
          sub={`RX ${formatBps(traffic?.rx_bps ?? 0)} · TX ${formatBps(traffic?.tx_bps ?? 0)}`}
          icon={<Activity size={18} />}
        />
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <Card className="p-4 xl:col-span-2">
          <div className="mb-3 flex items-center justify-between">
            <div>
              <div className="text-sm font-semibold">Aggregate throughput</div>
              <div className="text-xs text-muted">Live totals across all switch ports</div>
            </div>
            <Badge tone={connected ? 'up' : 'warning'}>{connected ? 'live' : 'offline'}</Badge>
          </div>
          <div className="h-52">
            {chartData.length < 2 ? (
              <Empty title="Waiting for traffic samples…" hint="The switch broadcasts statistics every two seconds." />
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chartData} margin={{ top: 4, right: 6, left: 0, bottom: 0 }}>
                  <defs>
                    <linearGradient id="rxGradient" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="rgb(var(--c-accent))" stopOpacity={0.35} />
                      <stop offset="100%" stopColor="rgb(var(--c-accent))" stopOpacity={0} />
                    </linearGradient>
                    <linearGradient id="txGradient" x1="0" y1="0" x2="0" y2="1">
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
                    tickFormatter={(value) => formatBps(value)}
                    width={70}
                  />
                  <Tooltip
                    contentStyle={{ background: 'rgb(var(--c-panel))', border: '1px solid rgb(var(--c-edge))', borderRadius: 8, fontSize: 12 }}
                    formatter={(value: number, name) => [formatBps(value), name === 'rx' ? 'RX' : 'TX']}
                  />
                  <Area type="monotone" dataKey="rx" stroke="rgb(var(--c-accent))" fill="url(#rxGradient)" strokeWidth={1.6} isAnimationActive={false} />
                  <Area type="monotone" dataKey="tx" stroke="rgb(var(--c-accent2))" fill="url(#txGradient)" strokeWidth={1.6} isAnimationActive={false} />
                </AreaChart>
              </ResponsiveContainer>
            )}
          </div>
        </Card>

        <Card className="p-4">
          <div className="mb-3 text-sm font-semibold">System</div>
          <div className="space-y-2.5 text-sm">
            <Row label="Switch engine" value={info.ovs.version ?? 'not available'} />
            <Row label="Bridge" value={`${info.bridge}${info.ovs.bridge_exists ? '' : ' (missing)'}`} />
            <Row label="FRRouting" value={info.frr.installed ? info.frr.version ?? 'installed' : 'not installed'} />
            <Row label="LLDP" value={info.lldp.installed ? (info.lldp.enabled ? 'enabled' : 'disabled') : 'not installed'} />
            <Row label="Total RX" value={formatBytes(traffic?.rx_bytes ?? 0)} />
            <Row label="Total TX" value={formatBytes(traffic?.tx_bytes ?? 0)} />
            <Row label="Python" value={info.python} />
          </div>
          <div className="mt-4 border-t border-edge pt-3">
            <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-muted">
              <Zap size={13} /> Feature support
            </div>
            <div className="flex flex-wrap gap-1.5">
              {Object.entries(info.features).map(([key, feature]) => (
                <Badge key={key} tone={feature.supported ? 'up' : 'warning'}>
                  {key}
                </Badge>
              ))}
            </div>
          </div>
        </Card>
      </div>

      <Card className="p-4">
        <div className="mb-3 flex items-center justify-between">
          <div className="text-sm font-semibold">Recent events</div>
          <Link to="/logs" className="text-xs text-accent hover:underline">
            View all
          </Link>
        </div>
        {info.events.length === 0 ? (
          <Empty title="No events recorded yet" />
        ) : (
          <div className="divide-y divide-edge">
            {info.events.map((event) => (
              <div key={event.id} className="flex items-center gap-3 py-2 text-sm">
                <Badge tone={event.severity}>{event.severity}</Badge>
                <span className="min-w-0 flex-1 truncate">{event.message}</span>
                <span className="shrink-0 text-xs text-muted">{timeAgo(event.ts)}</span>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-xs text-muted">{label}</span>
      <span className="mono truncate text-right text-[12px]">{value}</span>
    </div>
  )
}
