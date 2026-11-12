import { useMemo, useState } from 'react'
import { useLiveRefresh } from '../useLiveRefresh'
import { isAllVlans } from '../vlanInput'
import { RefreshCw, Search, ShieldAlert } from 'lucide-react'
import { api, hasRole, type InterfaceRow, type Session, type Vlan } from '../api'
import { Badge, Card, Empty, PageHeader, Toast } from '../components/ui'
import InterfaceEditor from '../components/InterfaceEditor'

export default function Interfaces({ session }: { session: Session }) {
  const [rows, setRows] = useState<InterfaceRow[]>([])
  const [vlans, setVlans] = useState<Vlan[]>([])
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<InterfaceRow | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useLiveRefresh(async () => {
    try {
      const data = await api.get<{ interfaces: InterfaceRow[] }>('/api/v1/interfaces')
      setRows(data.interfaces)
      setSelected((prev) => prev ? data.interfaces.find((row) => row.name === prev.name) ?? null : null)
      const vlanData = await api.get<{ vlans: Vlan[] }>('/api/v1/vlans')
      setVlans(vlanData.vlans)
      setError(null)
    } catch (exc: any) {
      setError(exc.message)
    }
  })

  const scan = async () => {
    setBusy(true)
    try {
      await api.post('/api/v1/interfaces/scan')
      await load()
      setToast('Interface scan complete')
    } catch (exc: any) {
      setError(exc.message)
    } finally {
      setBusy(false)
    }
  }

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return rows
    return rows.filter((row) =>
      [row.name, row.linux_if, row.mac, row.identity, row.description, row.kind].some((value) => (value ?? '').toLowerCase().includes(needle)),
    )
  }, [rows, query])

  return (
    <div className="space-y-5">
      <PageHeader
        title="Interfaces"
        subtitle="Physical NIC discovery, stable mapping and port properties"
        actions={
          <>
            <div className="relative">
              <Search size={14} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-muted" />
              <input className="input !py-1.5 w-52 pl-8" placeholder="Search port / NIC / MAC" value={query} onChange={(e) => setQuery(e.target.value)} />
            </div>
            <button className="btn" onClick={scan} disabled={busy}>
              <RefreshCw size={15} className={busy ? 'animate-spin' : ''} /> Scan NICs
            </button>
          </>
        }
      />

      {error && <Card className="border-bad/40 p-4 text-sm text-bad">{error}</Card>}

      <Card className="overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[900px]">
            <thead className="bg-panel2/60">
              <tr>
                <th className="th">Port</th>
                <th className="th">Linux NIC</th>
                <th className="th">MAC address</th>
                <th className="th">Adapter</th>
                <th className="th">Status</th>
                <th className="th">Mode</th>
                <th className="th">VLAN</th>
                <th className="th">MTU</th>
                <th className="th">Description</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-edge">
              {filtered.map((row) => (
                <tr key={row.name} className="cursor-pointer transition hover:bg-panel2/60" onClick={() => setSelected(row)}>
                  <td className="td font-medium">{row.name}</td>
                  <td className="td mono text-xs">{row.linux_if ?? '—'}</td>
                  <td className="td mono text-xs">{row.mac || '—'}</td>
                  <td className="td text-xs">
                    <div className="flex items-center gap-1.5">
                      <Badge tone={row.kind === 'usb' ? 'info' : 'readonly'}>{row.kind}</Badge>
                      {row.protected && (
                        <span title="Management interface — protected">
                          <ShieldAlert size={14} className="text-warn" />
                        </span>
                      )}
                    </div>
                  </td>
                  <td className="td">
                    <Badge tone={row.status}>{row.status}</Badge>
                  </td>
                  <td className="td text-xs">{row.attached ? <Badge tone={row.mode}>{row.mode}</Badge> : <span className="text-muted">unassigned</span>}</td>
                  <td className="td mono text-xs">
                    {row.attached ? (row.mode === 'access' ? row.access_vlan : isAllVlans(row.trunk_allowed) ? 'all allowed' : `${row.trunk_allowed.length} allowed`) : '—'}
                  </td>
                  <td className="td mono text-xs">{row.mtu}</td>
                  <td className="td max-w-[200px] truncate text-muted">{row.description || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {filtered.length === 0 && <Empty title="No matching interfaces" hint="Physical NICs appear here after detection. The host management NIC is excluded." />}
        </div>
      </Card>

      <p className="px-1 text-[11px] text-muted">
        Interface mapping is persisted by stable PCI/USB topology. A NIC is never silently reassigned to a different switch port; a missing device is
        shown as “not present” until it returns.
      </p>

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
