import { useEffect, useMemo, useState } from 'react'
import { RefreshCw, Search } from 'lucide-react'
import { api, type MacEntry, type Vlan } from '../api'
import { Badge, Card, Empty, PageHeader, Toggle } from '../components/ui'

export default function MacTable() {
  const [entries, setEntries] = useState<MacEntry[]>([])
  const [vlans, setVlans] = useState<Vlan[]>([])
  const [query, setQuery] = useState('')
  const [vlanFilter, setVlanFilter] = useState<string>('')
  const [portFilter, setPortFilter] = useState<string>('')
  const [auto, setAuto] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [updated, setUpdated] = useState<number>(0)

  const load = async () => {
    try {
      const data = await api.get<{ entries: MacEntry[] }>('/api/v1/mac-table')
      setEntries(data.entries)
      setUpdated(Date.now())
      setError(null)
    } catch (exc: any) {
      setError(exc.message)
    }
  }

  useEffect(() => {
    api.get<{ vlans: Vlan[] }>('/api/v1/vlans').then((data) => setVlans(data.vlans)).catch(() => undefined)
    load()
  }, [])

  useEffect(() => {
    if (!auto) return
    const timer = setInterval(load, 5000)
    return () => clearInterval(timer)
  }, [auto])

  const ports = useMemo(() => Array.from(new Set(entries.map((e) => e.port))).sort(), [entries])
  const filtered = useMemo(
    () =>
      entries.filter((entry) => {
        if (vlanFilter && String(entry.vlan) !== vlanFilter) return false
        if (portFilter && entry.port !== portFilter) return false
        if (query) {
          const needle = query.toLowerCase()
          if (!entry.mac.toLowerCase().includes(needle) && !entry.port.toLowerCase().includes(needle)) return false
        }
        return true
      }),
    [entries, vlanFilter, portFilter, query],
  )

  return (
    <div className="space-y-5">
      <PageHeader
        title="MAC address table"
        subtitle="Forwarding database learned by Open vSwitch"
        actions={
          <>
            <div className="flex items-center gap-2 text-xs text-muted">
              Auto refresh <Toggle checked={auto} onChange={setAuto} />
            </div>
            <button className="btn" onClick={load}>
              <RefreshCw size={15} /> Refresh
            </button>
          </>
        }
      />

      {error && <Card className="border-bad/40 p-4 text-sm text-bad">{error}</Card>}

      <Card className="p-4">
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-[220px] flex-1">
            <Search size={14} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-muted" />
            <input className="input pl-8" placeholder="Search MAC or port" value={query} onChange={(e) => setQuery(e.target.value)} />
          </div>
          <select className="input w-40" value={vlanFilter} onChange={(e) => setVlanFilter(e.target.value)}>
            <option value="">All VLANs</option>
            {vlans.map((vlan) => (
              <option key={vlan.id} value={vlan.id}>
                {vlan.id} — {vlan.name}
              </option>
            ))}
          </select>
          <select className="input w-44" value={portFilter} onChange={(e) => setPortFilter(e.target.value)}>
            <option value="">All ports</option>
            {ports.map((port) => (
              <option key={port} value={port}>
                {port}
              </option>
            ))}
          </select>
          <div className="ml-auto text-xs text-muted">
            {filtered.length} entries{updated ? ` · ${new Date(updated).toLocaleTimeString()}` : ''}
          </div>
        </div>
      </Card>

      <Card className="overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[680px]">
            <thead className="bg-panel2/60">
              <tr>
                <th className="th">MAC address</th>
                <th className="th">VLAN</th>
                <th className="th">Learned port</th>
                <th className="th">Type</th>
                <th className="th text-right">Age</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-edge">
              {filtered.map((entry, index) => (
                <tr key={`${entry.mac}-${entry.vlan}-${index}`} className="hover:bg-panel2/50">
                  <td className="td mono">{entry.mac}</td>
                  <td className="td">
                    <Badge tone="access">
                      {entry.vlan} {entry.vlan_name ? `· ${entry.vlan_name}` : ''}
                    </Badge>
                  </td>
                  <td className="td">
                    <div className="font-medium">{entry.port}</div>
                    <div className="mono text-[10px] text-muted">{entry.linux_if}</div>
                  </td>
                  <td className="td text-xs text-muted">{entry.type}</td>
                  <td className="td mono text-right text-xs">{Math.round(entry.age)}s</td>
                </tr>
              ))}
            </tbody>
          </table>
          {filtered.length === 0 && (
            <Empty title="No MAC entries" hint="Traffic on assigned ports populates the forwarding table. Filters may be hiding entries." />
          )}
        </div>
      </Card>
    </div>
  )
}
