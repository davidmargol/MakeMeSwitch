import { useState } from 'react'
import { useLiveRefresh } from '../useLiveRefresh'
import { allowedVlanSummary, isAllVlans } from '../vlanInput'
import { RefreshCw } from 'lucide-react'
import { api, formatBps, hasRole, type InterfaceRow, type Session, type Vlan } from '../api'
import { Badge, Card, Empty, PageHeader, Toast } from '../components/ui'
import { useStats } from '../stats'
import InterfaceEditor from '../components/InterfaceEditor'

export default function Ports({ session }: { session: Session }) {
  const [rows, setRows] = useState<InterfaceRow[]>([])
  const [vlans, setVlans] = useState<Vlan[]>([])
  const [selected, setSelected] = useState<InterfaceRow | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const { snapshot } = useStats()

  const load = useLiveRefresh(async () => {
    try {
      const data = await api.get<{ interfaces: InterfaceRow[] }>('/api/v1/interfaces')
      setRows(data.interfaces)
      const vlanData = await api.get<{ vlans: Vlan[] }>('/api/v1/vlans')
      setVlans(vlanData.vlans)
      setError(null)
      setSelected((prev) => (prev ? data.interfaces.find((row) => row.name === prev.name) ?? null : null))
    } catch (exc: any) {
      setError(exc.message)
    } finally {
      setLoading(false)
    }
  })

  const live = new Map((snapshot?.ports ?? []).map((port) => [port.name, port]))
  const active = rows.filter((row) => row.status === 'up').length
  const assigned = rows.filter((row) => row.attached).length

  return (
    <div className="space-y-5">
      <PageHeader
        title="Switch ports"
        subtitle={`${rows.length} ports detected · ${assigned} assigned · ${active} link up`}
        actions={
          <button className="btn" onClick={load}>
            <RefreshCw size={15} /> Refresh
          </button>
        }
      />

      {error && <Card className="border-bad/40 p-4 text-sm text-bad">{error}</Card>}

      <Card className="p-4 md:p-5">
        <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
          <div>
            <div className="text-sm font-semibold">Front panel</div>
            <div className="text-xs text-muted">Physical ports mapped to this switch</div>
          </div>
          <div className="flex flex-wrap items-center gap-3 text-[11px] text-muted">
            <Legend color="bg-ok" label="link up" />
            <Legend color="bg-warn" label="warning" />
            <Legend color="bg-bad" label="admin down" />
            <Legend color="bg-edge" label="no link" />
            <Legend color="bg-panel2 border border-edge" label="not assigned" />
          </div>
        </div>

        {loading ? (
          <Empty title="Scanning interfaces…" />
        ) : rows.length === 0 ? (
          <Empty
            title="No physical Ethernet ports detected"
            hint="Connect a USB/PCIe NIC and refresh. The management interface is intentionally excluded."
          />
        ) : (
          <div className="rounded-xl border border-edge bg-[rgb(var(--c-bg))]/60 p-3 md:p-4">
            <div className="mb-3 flex items-center justify-between text-[11px] uppercase tracking-wider text-muted">
              <span>{rows.length > 0 ? 'MakeMeSwitch' : ''}</span>
              <span className="mono">{rows.length} × Ethernet</span>
            </div>
            <div className="grid grid-cols-4 gap-2 sm:grid-cols-6 md:grid-cols-8 xl:grid-cols-12">
              {rows.map((row, index) => {
                const stats = live.get(row.name)?.stats ?? row.stats ?? {}
                const errors = (stats.rx_errors ?? 0) + (stats.tx_errors ?? 0)
                const isUp = row.status === 'up'
                const isAdminDown = !row.admin_up
                const dot = isAdminDown ? 'bg-bad' : isUp ? (errors > 0 ? 'bg-warn' : 'bg-ok') : row.attached ? 'bg-edge' : 'bg-panel2 border border-edge'
                const vlanLabel = !row.attached
                  ? 'unassigned'
                  : row.mode === 'access'
                    ? `vlan ${row.access_vlan}`
                    : isAllVlans(row.trunk_allowed) ? 'trunk all' : `trunk ${row.trunk_allowed.length}v`
                return (
                  <button
                    key={row.name}
                    onClick={() => setSelected(row)}
                    className={`group relative overflow-hidden rounded-lg border p-2 text-left transition hover:border-accent/60 ${
                      row.attached ? 'border-edge bg-panel2' : 'border-dashed border-edge bg-panel/40'
                    }`}
                    title={`${row.name} — ${row.linux_if ?? 'unmapped'}`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="text-[10px] font-semibold text-muted">{String(index + 1).padStart(2, '0')}</span>
                      <span className={`h-2 w-2 rounded-full ${dot} ${isUp && errors === 0 ? 'shadow-[0_0_8px_rgb(var(--c-ok))]' : ''}`} />
                    </div>
                    <div className="mt-1 truncate text-xs font-medium">{row.name.replace('Ethernet', 'Eth')}</div>
                    <div className="truncate text-[10px] text-muted">{vlanLabel}</div>
                    <div className="mt-1 h-1 overflow-hidden rounded bg-edge/60">
                      <div
                        className={`h-full ${isUp ? 'bg-ok/70' : 'bg-edge'}`}
                        style={{ width: `${Math.min(100, ((live.get(row.name)?.rates.rx_bps ?? 0) / 10_000_000) * 100)}%` }}
                      />
                    </div>
                    <div className="mt-0.5 truncate text-[9px] text-muted/80">{formatBps(live.get(row.name)?.rates.rx_bps ?? 0)}</div>
                  </button>
                )
              })}
            </div>
          </div>
        )}
      </Card>

      <Card className="overflow-hidden">
        <div className="border-b border-edge px-4 py-3 text-sm font-semibold">Port summary</div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px]">
            <thead className="bg-panel2/60">
              <tr>
                <th className="th">Port</th>
                <th className="th">NIC</th>
                <th className="th">Status</th>
                <th className="th">Mode</th>
                <th className="th">VLANs</th>
                <th className="th">Speed</th>
                <th className="th">RX</th>
                <th className="th">TX</th>
                <th className="th">Description</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-edge">
              {rows.map((row) => (
                <tr key={row.name} className="cursor-pointer transition hover:bg-panel2/60" onClick={() => setSelected(row)}>
                  <td className="td font-medium">{row.name}</td>
                  <td className="td mono text-muted">{row.linux_if ?? '—'}</td>
                  <td className="td">
                    <Badge tone={row.status}>{row.status}</Badge>
                  </td>
                  <td className="td">{row.attached ? <Badge tone={row.mode}>{row.mode}</Badge> : <span className="text-muted">—</span>}</td>
                  <td className="td mono text-xs">
                    {!row.attached
                      ? '—'
                      : row.mode === 'access'
                        ? row.access_vlan
                        : allowedVlanSummary(row.trunk_allowed)}
                  </td>
                  <td className="td">{row.link?.speed ? `${row.link.speed} Mb/s` : '—'}</td>
                  <td className="td mono text-xs">{formatBps(live.get(row.name)?.rates.rx_bps ?? 0)}</td>
                  <td className="td mono text-xs">{formatBps(live.get(row.name)?.rates.tx_bps ?? 0)}</td>
                  <td className="td max-w-[180px] truncate text-muted">{row.description || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {rows.length === 0 && <Empty title="No ports" />}
        </div>
      </Card>

      {selected && (
        <InterfaceEditor
          iface={selected}
          vlans={vlans}
          canEdit={hasRole(session, 'operator')}
          onClose={() => setSelected(null)}
          onSaved={(message) => {
            setToast(message)
            load()
          }}
        />
      )}
      <Toast message={toast} onDone={() => setToast(null)} />
    </div>
  )
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className={`h-2 w-2 rounded-full ${color}`} /> {label}
    </span>
  )
}
