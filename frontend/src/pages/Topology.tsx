import { useEffect, useState } from 'react'
import { ArrowLeftRight, Info, Network, Unplug } from 'lucide-react'
import { api } from '../api'
import { Badge, Card, Empty, PageHeader } from '../components/ui'
import { useStats } from '../stats'

interface TopologyPort {
  name: string
  linux_if: string | null
  status: string
  speed: number | null
  neighbor: any | null
  lldp_identified: boolean
}

interface TopologyData {
  local: { hostname: string; bridge: string }
  lldp: { installed: boolean; enabled: boolean; neighbors: any[]; detail: string }
  ports: TopologyPort[]
}

export default function Topology() {
  const [data, setData] = useState<TopologyData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const { snapshot } = useStats()

  useEffect(() => {
    const load = () =>
      api
        .get<TopologyData>('/api/v1/topology')
        .then(setData)
        .catch((exc) => setError(exc.message))
    load()
    const timer = setInterval(load, 15000)
    return () => clearInterval(timer)
  }, [])

  if (error) return <Card className="border-bad/40 p-4 text-sm text-bad">{error}</Card>
  if (!data) return <Empty title="Loading topology…" />

  const live = new Map((snapshot?.ports ?? []).map((port) => [port.name, port]))
  const identified = data.ports.filter((port) => port.lldp_identified).length

  return (
    <div className="space-y-5">
      <PageHeader
        title="Network topology"
        subtitle="Neighbours are reported only from LLDP; unidentified links are marked as such"
        actions={<Badge tone={data.lldp.installed ? (data.lldp.enabled ? 'up' : 'warning') : 'down'}>{data.lldp.installed ? (data.lldp.enabled ? 'LLDP enabled' : 'LLDP disabled') : 'lldpd not installed'}</Badge>}
      />

      {!data.lldp.installed && (
        <Card className="flex items-start gap-3 border-warn/30 p-4 text-sm">
          <Info size={16} className="mt-0.5 shrink-0 text-warn" />
          <div>
            <div className="font-medium">LLDP neighbour discovery is unavailable</div>
            <div className="mt-0.5 text-xs text-muted">
              Install <span className="mono">lldpd</span> on the host and enable LLDP in configuration. No topology data is invented — port links below come
              from carrier state only.
            </div>
          </div>
        </Card>
      )}

      <Card className="p-5">
        <div className="mb-6 flex items-center justify-between text-xs text-muted">
          <span className="mono">{data.local.hostname}</span>
          <span className="mono">bridge {data.local.bridge}</span>
        </div>

        <div className="relative flex flex-col items-center gap-6">
          <div className="z-10 w-full max-w-xs rounded-xl border border-accent/40 bg-accent/5 p-4 text-center shadow-[0_0_40px_-20px_rgb(var(--c-accent))]">
            <div className="mx-auto mb-2 grid h-10 w-10 place-items-center rounded-lg border border-accent/40 bg-accent/10 text-accent">
              <Network size={20} />
            </div>
            <div className="text-sm font-semibold">{data.local.hostname}</div>
            <div className="mt-0.5 text-[11px] text-muted">MakeMeSwitch · {data.ports.length} ports</div>
          </div>

          <div className="grid w-full gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {data.ports.map((port) => {
              const stats = live.get(port.name)
              const up = port.status === 'up'
              return (
                <div
                  key={port.name}
                  className={`rounded-lg border p-3 transition ${up ? 'border-ok/30 bg-ok/5' : 'border-edge bg-panel2/60'}`}
                >
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <ArrowLeftRight size={14} className={up ? 'text-ok' : 'text-muted'} />
                      <span className="text-sm font-medium">{port.name}</span>
                    </div>
                    <Badge tone={port.status}>{port.status}</Badge>
                  </div>
                  <div className="mt-2 space-y-1 text-xs">
                    <div className="flex justify-between text-muted">
                      <span>NIC</span>
                      <span className="mono">{port.linux_if ?? '—'}</span>
                    </div>
                    <div className="flex justify-between text-muted">
                      <span>Speed</span>
                      <span>{port.speed ? `${port.speed} Mb/s` : '—'}</span>
                    </div>
                    <div className="flex justify-between text-muted">
                      <span>Traffic</span>
                      <span className="mono">{stats ? `${Math.round((stats.rates.rx_bps + stats.rates.tx_bps) / 1000)} kbps` : '—'}</span>
                    </div>
                  </div>
                  <div className="mt-2 border-t border-edge pt-2 text-[11px]">
                    {port.neighbor ? (
                      <div className="flex items-center gap-1.5 text-accent">
                        <Network size={12} />
                        <span className="truncate">{port.neighbor.chassis ?? 'neighbour'}</span>
                        <span className="text-muted">· {port.neighbor.port ?? 'port n/a'}</span>
                      </div>
                    ) : (
                      <span className="flex items-center gap-1.5 text-muted">
                        <Unplug size={12} /> neighbour unidentified
                      </span>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
          {data.ports.length === 0 && <Empty title="No ports to draw" hint="Assign at least one physical NIC to the switch." />}
        </div>

        <div className="mt-5 text-center text-[11px] text-muted">
          {identified} of {data.ports.length} links identified via LLDP · local port carrier state updates live
        </div>
      </Card>
    </div>
  )
}
