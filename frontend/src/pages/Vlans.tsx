import { useEffect, useState } from 'react'
import { useLiveRefresh } from '../useLiveRefresh'
import { formatAllowedVlans, parseAllowedVlans } from '../vlanInput'
import { Check, Plus, Save, Trash2 } from 'lucide-react'
import { api, ConfirmError, hasRole, type InterfaceRow, type Op, type Session, type Vlan } from '../api'
import { Badge, Card, ConfirmDialog, Empty, Modal, PageHeader, Toast, useAsyncAction } from '../components/ui'

export default function Vlans({ session }: { session: Session }) {
  const [vlans, setVlans] = useState<Vlan[]>([])
  const [rows, setRows] = useState<InterfaceRow[]>([])
  const [toast, setToast] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [newId, setNewId] = useState('')
  const [newName, setNewName] = useState('')
  const [editing, setEditing] = useState<Vlan | null>(null)
  const [confirmState, setConfirmState] = useState<{ message: string; ops: Op[]; retry: () => Promise<void> } | null>(null)
  const { busy, run } = useAsyncAction()
  const canEdit = hasRole(session, 'operator')

  const load = useLiveRefresh(async () => {
    try {
      const data = await api.get<{ vlans: Vlan[]; interfaces: InterfaceRow[] }>('/api/v1/vlans')
      setVlans(data.vlans)
      setRows(data.interfaces)
      setError(null)
    } catch (exc: any) {
      setError(exc.message)
    }
  })

  const handle = async (action: (confirm: boolean) => Promise<any>, success: string) => {
    try {
      await run(() => action(false), true)
      setToast(success)
      await load()
    } catch (exc: any) {
      if (exc instanceof ConfirmError) {
        setConfirmState({
          message: exc.message,
          ops: exc.preview.ops,
          retry: async () => {
            await run(() => action(true), true)
            setToast(success)
            await load()
          },
        })
      } else {
        setError(exc?.message ?? String(exc))
      }
    }
  }

  const createVlan = () =>
    handle(async () => {
      await api.post('/api/v1/vlans', { id: Number(newId), name: newName || undefined })
      setCreateOpen(false)
      setNewId('')
      setNewName('')
    }, `VLAN ${newId} created`)

  const deleteVlan = (vlan: Vlan) =>
    handle((confirm) => api.del(`/api/v1/vlans/${vlan.id}`, { confirm }), `VLAN ${vlan.id} deleted`)

  const renameVlan = (vlan: Vlan, name: string) =>
    handle((confirm) => api.patch(`/api/v1/vlans/${vlan.id}`, { name, confirm }), `VLAN ${vlan.id} renamed`)


  return (
    <div className="space-y-5">
      <PageHeader
        title="VLANs"
        subtitle="802.1Q segmentation applied directly to Open vSwitch"
        actions={
          canEdit && (
            <button className="btn-primary" onClick={() => setCreateOpen(true)}>
              <Plus size={15} /> New VLAN
            </button>
          )
        }
      />

      {error && <Card className="border-bad/40 p-4 text-sm text-bad">{error}</Card>}

      <div className="grid gap-4 xl:grid-cols-5">
        <Card className="overflow-hidden xl:col-span-2">
          <div className="border-b border-edge px-4 py-3 text-sm font-semibold">VLAN database</div>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="bg-panel2/60">
                <tr>
                  <th className="th">ID</th>
                  <th className="th">Name</th>
                  <th className="th">Ports</th>
                  <th className="th"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-edge">
                {vlans.map((vlan) => (
                  <tr key={vlan.id} className="hover:bg-panel2/50">
                    <td className="td mono font-medium">{vlan.id}</td>
                    <td className="td">
                      <div className="font-medium">{vlan.name}</div>
                      {vlan.description && <div className="text-[11px] text-muted">{vlan.description}</div>}
                    </td>
                    <td className="td text-xs text-muted">
                      {vlan.untagged.length + vlan.tagged.length > 0 ? `${vlan.untagged.length}U / ${vlan.tagged.length}T` : '—'}
                    </td>
                    <td className="td text-right">
                      {canEdit && (
                        <div className="flex justify-end gap-1">
                          <button className="btn !px-2 !py-1" title="Rename" onClick={() => setEditing(vlan)}>
                            <Save size={13} />
                          </button>
                          {vlan.id !== 1 && (
                            <button className="btn-danger !px-2 !py-1" title="Delete" onClick={() => deleteVlan(vlan)} disabled={busy}>
                              <Trash2 size={13} />
                            </button>
                          )}
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {vlans.length === 0 && <Empty title="No VLANs" />}
          </div>
        </Card>

        <Card className="overflow-hidden xl:col-span-3">
          <div className="flex items-center justify-between border-b border-edge px-4 py-3">
            <div>
              <div className="text-sm font-semibold">Membership matrix</div>
              <div className="text-[11px] text-muted">U = untagged (access) · T = tagged (trunk)</div>
            </div>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="bg-panel2/60">
                <tr>
                  <th className="th sticky left-0 bg-panel2/90">Port</th>
                  {vlans.map((vlan) => (
                    <th key={vlan.id} className="th text-center" title={vlan.name}>
                      {vlan.id}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-edge">
                {rows.map((row) => (
                  <tr key={row.name} className="hover:bg-panel2/50">
                    <td className="td sticky left-0 bg-panel/95 font-medium">{row.name}</td>
                    {vlans.map((vlan) => {
                      const untagged = row.vlans.untagged.includes(vlan.id)
                      const tagged = row.vlans.tagged.includes(vlan.id)
                      return (
                        <td key={vlan.id} className="td text-center">
                          {untagged ? (
                            <span className="font-semibold text-accent">U</span>
                          ) : tagged ? (
                            <span className="font-semibold text-accent2">T</span>
                          ) : (
                            <span className="text-muted/40">–</span>
                          )}
                        </td>
                      )
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
            {rows.length === 0 && <Empty title="No switch ports" hint="Assign physical NICs first on the Interfaces page." />}
          </div>
        </Card>
      </div>

      <Card className="overflow-hidden">
        <div className="border-b border-edge px-4 py-3">
          <div className="text-sm font-semibold">Port VLAN assignment</div>
          <div className="text-[11px] text-muted">Changes go through the shared configuration engine and update OVS immediately.</div>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px]">
            <thead className="bg-panel2/60">
              <tr>
                <th className="th">Port</th>
                <th className="th">State</th>
                <th className="th">Mode</th>
                <th className="th">Access VLAN</th>
                <th className="th">Allowed (trunk)</th>
                <th className="th">Native</th>
                <th className="th"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-edge">
              {rows.map((row) => (
                <PortRow key={row.name} row={row} vlans={vlans} canEdit={canEdit} onSave={load} onToast={setToast} onConfirm={setConfirmState} />
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Modal
        open={createOpen}
        title="Create VLAN"
        onClose={() => setCreateOpen(false)}
        footer={
          <>
            <button className="btn" onClick={() => setCreateOpen(false)}>
              Cancel
            </button>
            <button className="btn-primary" onClick={createVlan} disabled={!newId || busy}>
              <Check size={14} /> Create
            </button>
          </>
        }
      >
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className="label">VLAN ID (1–4094)</label>
            <input className="input" type="number" min={2} max={4094} value={newId} onChange={(e) => setNewId(e.target.value)} />
          </div>
          <div>
            <label className="label">Name</label>
            <input className="input" value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="SERVERS" />
          </div>
        </div>
      </Modal>

      <Modal
        open={!!editing}
        title={`Rename VLAN ${editing?.id ?? ''}`}
        onClose={() => setEditing(null)}
        footer={
          <>
            <button className="btn" onClick={() => setEditing(null)}>
              Cancel
            </button>
            <button
              className="btn-primary"
              onClick={async () => {
                if (editing) {
                  await renameVlan(editing, editing.name)
                  setEditing(null)
                }
              }}
            >
              <Check size={14} /> Save
            </button>
          </>
        }
      >
        <label className="label">Name</label>
        <input className="input" value={editing?.name ?? ''} onChange={(e) => editing && setEditing({ ...editing, name: e.target.value })} />
      </Modal>

      <ConfirmDialog
        open={!!confirmState}
        title="Confirm disruptive change"
        message={confirmState?.message ?? ''}
        ops={confirmState?.ops}
        onCancel={() => setConfirmState(null)}
        onConfirm={async () => {
          if (!confirmState) return
          const retry = confirmState.retry
          setConfirmState(null)
          try {
            await retry()
          } catch (exc: any) {
            setError(exc?.message ?? String(exc))
          }
        }}
      />
      <Toast message={toast} onDone={() => setToast(null)} />
    </div>
  )
}

function PortRow({
  row,
  vlans,
  canEdit,
  onSave,
  onToast,
  onConfirm,
}: {
  row: InterfaceRow
  vlans: Vlan[]
  canEdit: boolean
  onSave: () => void
  onToast: (message: string) => void
  onConfirm: (state: { message: string; ops: Op[]; retry: () => Promise<void> } | null) => void
}) {
  const [mode, setMode] = useState<'access' | 'trunk'>(row.mode)
  const [access, setAccess] = useState(row.access_vlan)
  const [allowed, setAllowed] = useState(formatAllowedVlans(row.trunk_allowed))
  const [native, setNative] = useState(row.native_vlan ?? '')
  const [busy, setBusy] = useState(false)
  const [dirty, setDirty] = useState(false)

  useEffect(() => {
    if (dirty) return
    setMode(row.mode)
    setAccess(row.access_vlan)
    setAllowed(formatAllowedVlans(row.trunk_allowed))
    setNative(row.native_vlan ?? '')
  }, [row, dirty])

  const save = async (confirm = false) => {
    setBusy(true)
    try {
      const body: Record<string, any> = {
        mode,
        native_vlan: native === '' ? null : Number(native),
        confirm,
      }
      if (mode === 'access') body.access_vlan = access
      else body.trunk_allowed = parseAllowedVlans(allowed)
      await api.patch(`/api/v1/interfaces/${row.name}`, body)
      setDirty(false)
      onToast(`${row.name} VLAN assignment saved`)
      onSave()
    } catch (exc: any) {
      if (exc instanceof ConfirmError) {
        onConfirm({
          message: exc.message,
          ops: exc.preview.ops,
          retry: async () => {
            await save(true)
          },
        })
      } else {
        onToast(exc?.message ?? String(exc))
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <tr className="hover:bg-panel2/40" onChange={() => setDirty(true)}>
      <td className="td font-medium">{row.name}</td>
      <td className="td text-xs text-muted">{row.attached ? row.status : 'unassigned'}</td>
      <td className="td">
        <select className="input !py-1 !text-xs w-24" value={mode} onChange={(e) => setMode(e.target.value as 'access' | 'trunk')} disabled={!canEdit}>
          <option value="access">access</option>
          <option value="trunk">trunk</option>
        </select>
      </td>
      <td className="td">
        <select className="input !py-1 !text-xs w-28" value={access} onChange={(e) => setAccess(Number(e.target.value))} disabled={!canEdit || mode !== 'access'}>
          {vlans.map((vlan) => (
            <option key={vlan.id} value={vlan.id}>
              {vlan.id}
            </option>
          ))}
        </select>
      </td>
      <td className="td">
        <input className="input !py-1 !text-xs w-40" value={allowed} onChange={(e) => setAllowed(e.target.value)} placeholder="all or 10,20-30" title="VLAN IDs 1–4094, ranges, or all" disabled={!canEdit || mode !== 'trunk'} />
      </td>
      <td className="td">
        <select className="input !py-1 !text-xs w-24" value={native} onChange={(e) => setNative(e.target.value)} disabled={!canEdit || mode !== 'trunk'}>
          <option value="">none</option>
          {vlans.map((vlan) => (
            <option key={vlan.id} value={vlan.id}>
              {vlan.id}
            </option>
          ))}
        </select>
      </td>
      <td className="td text-right">
        {canEdit && (
          <button className="btn !px-2 !py-1" onClick={() => save(false)} disabled={busy}>
            <Save size={13} /> Save
          </button>
        )}
      </td>
    </tr>
  )
}
