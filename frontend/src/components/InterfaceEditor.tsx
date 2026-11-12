import { useState } from 'react'
import { formatAllowedVlans, parseAllowedVlans } from '../vlanInput'
import { Cable, Power, Save, Unplug, Usb, Wrench } from 'lucide-react'
import { api, ConfirmError, formatBytes, type InterfaceRow, type Op, type Vlan } from '../api'
import { Badge, ConfirmDialog, Modal, Toggle, useAsyncAction } from './ui'

export default function InterfaceEditor({
  iface,
  vlans,
  canEdit,
  onClose,
  onSaved,
}: {
  iface: InterfaceRow
  vlans: Vlan[]
  canEdit: boolean
  onClose: () => void
  onSaved: (message: string) => void
}) {
  const [description, setDescription] = useState(iface.description)
  const [adminUp, setAdminUp] = useState(iface.admin_up)
  const [attached, setAttached] = useState(iface.attached)
  const [mode, setMode] = useState<'access' | 'trunk'>(iface.mode)
  const [accessVlan, setAccessVlan] = useState(iface.access_vlan)
  const [trunkAllowed, setTrunkAllowed] = useState(formatAllowedVlans(iface.trunk_allowed))
  const [nativeVlan, setNativeVlan] = useState(iface.native_vlan ?? '')
  const [mtu, setMtu] = useState(iface.mtu)
  const [confirmState, setConfirmState] = useState<{ message: string; ops: Op[]; payload: any } | null>(null)
  const { busy, error, setError, run } = useAsyncAction()

  const payload = () => {
    const body: Record<string, any> = {
      description,
      admin_up: adminUp,
      attached,
      mode,
      mtu,
      native_vlan: nativeVlan === '' ? null : Number(nativeVlan),
    }
    if (mode === 'access') body.access_vlan = accessVlan
    else body.trunk_allowed = parseAllowedVlans(trunkAllowed)
    return body
  }

  const save = async (confirm = false, draft = payload()) => {
    const body = { ...draft, confirm }
    const result = await run(() => api.patch(`/api/v1/interfaces/${iface.name}`, body), true)
    if (result) {
      onSaved(`${iface.name} updated (${(result as any).ops?.length ?? 0} OVS change(s))`)
    }
  }

  const submit = async () => {
    setError(null)
    try {
      await save(false)
    } catch (exc: any) {
      if (exc instanceof ConfirmError) {
        setConfirmState({
          message: exc.message,
          ops: exc.preview.ops,
          payload: payload(),
        })
      } else {
        setError(exc?.message ?? String(exc))
      }
    }
  }

  const detach = async () => {
    const result = await run(() => api.del(`/api/v1/interfaces/${iface.name}/attach`))
    if (result) onSaved(`${iface.name} detached from the switch`)
  }

  const stats = iface.stats ?? {}
  const link = iface.link ?? {}

  return (
    <>
      <Modal
        open
        wide
        title={`${iface.name} — ${iface.linux_if ?? 'unmapped'}`}
        onClose={onClose}
        footer={
          <>
            <button className="btn" onClick={onClose}>
              Close
            </button>
            {canEdit && iface.attached && (
              <button className="btn-danger" onClick={detach} disabled={busy}>
                <Unplug size={14} /> Detach
              </button>
            )}
            {canEdit && (
              <button className="btn-primary" onClick={submit} disabled={busy}>
                <Save size={14} /> {busy ? 'Applying…' : 'Apply changes'}
              </button>
            )}
          </>
        }
      >
        <div className="grid gap-5 md:grid-cols-2">
          <div className="space-y-3">
            <div className="rounded-lg border border-edge bg-panel2 p-3">
              <div className="mb-2 flex items-center justify-between">
                <div className="text-xs font-semibold uppercase tracking-wider text-muted">Physical adapter</div>
                <Badge tone={iface.status}>{iface.status}</Badge>
              </div>
              <dl className="space-y-1.5 text-sm">
                <Info label="Linux NIC" value={iface.linux_if ?? '—'} mono />
                <Info label="MAC address" value={iface.mac || '—'} mono />
                <Info label="Adapter identity" value={iface.identity ?? '—'} mono />
                <Info label="Connection" value={iface.kind === 'usb' ? `USB ${link.usb_path ?? ''}` : iface.kind === 'pci' ? `PCIe ${link.pci_slot ?? ''}` : iface.kind} />
                <Info label="MAC" value={link.mac ?? '—'} mono />
                <Info label="Driver" value={link.driver ?? '—'} />
                <Info label="Speed / duplex" value={`${link.speed ? `${link.speed} Mb/s` : 'auto'} / ${link.duplex ?? 'auto'}`} />
                <Info label="MTU (kernel)" value={String(link.mtu ?? '—')} />
                {iface.protected && <Info label="Protection" value="management interface — cannot be enslaved" />}
              </dl>
            </div>

            <div className="rounded-lg border border-edge bg-panel2 p-3">
              <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted">Counters</div>
              <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-sm">
                <Info label="RX bytes" value={formatBytes(stats.rx_bytes ?? 0)} />
                <Info label="TX bytes" value={formatBytes(stats.tx_bytes ?? 0)} />
                <Info label="RX packets" value={String(Math.round(stats.rx_packets ?? 0))} />
                <Info label="TX packets" value={String(Math.round(stats.tx_packets ?? 0))} />
                <Info label="RX errors" value={String(Math.round(stats.rx_errors ?? 0))} />
                <Info label="TX errors" value={String(Math.round(stats.tx_errors ?? 0))} />
              </div>
            </div>
          </div>

          <div className="space-y-3">
            <div>
              <label className="label">Description</label>
              <input className="input" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="uplink to core" disabled={!canEdit} />
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="flex items-center justify-between rounded-lg border border-edge bg-panel2 px-3 py-2">
                <span className="flex items-center gap-1.5 text-sm">
                  <Power size={14} /> Admin state
                </span>
                <Toggle checked={adminUp} onChange={setAdminUp} disabled={!canEdit} />
              </div>
              <div className="flex items-center justify-between rounded-lg border border-edge bg-panel2 px-3 py-2">
                <span className="flex items-center gap-1.5 text-sm">
                  <Cable size={14} /> In switch
                </span>
                <Toggle checked={attached} onChange={setAttached} disabled={!canEdit || iface.protected} />
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="label">Switchport mode</label>
                <select className="input" value={mode} onChange={(e) => setMode(e.target.value as 'access' | 'trunk')} disabled={!canEdit}>
                  <option value="access">access</option>
                  <option value="trunk">trunk</option>
                </select>
              </div>
              {mode === 'access' ? (
                <div>
                  <label className="label">Access VLAN</label>
                  <select className="input" value={accessVlan} onChange={(e) => setAccessVlan(Number(e.target.value))} disabled={!canEdit}>
                    {vlans.map((vlan) => (
                      <option key={vlan.id} value={vlan.id}>
                        {vlan.id} — {vlan.name}
                      </option>
                    ))}
                  </select>
                </div>
              ) : (
                <div>
                  <label className="label">Native VLAN (untagged)</label>
                  <select className="input" value={nativeVlan} onChange={(e) => setNativeVlan(e.target.value)} disabled={!canEdit}>
                    <option value="">none (all tagged)</option>
                    {vlans.map((vlan) => (
                      <option key={vlan.id} value={vlan.id}>
                        {vlan.id} — {vlan.name}
                      </option>
                    ))}
                  </select>
                </div>
              )}
            </div>

            {mode === 'trunk' && (
              <div>
                <label className="label">Allowed VLANs (tagged)</label>
                <input className="input" value={trunkAllowed} onChange={(e) => setTrunkAllowed(e.target.value)} placeholder="all or 10,20,30-32" disabled={!canEdit} />
                <p className="mt-1 text-xs text-muted">Use all for all VLANs, or IDs/ranges (1–4094).</p>
              </div>
            )}

            <div>
              <label className="label">MTU</label>
              <input className="input" type="number" min={576} max={9216} value={mtu} onChange={(e) => setMtu(Number(e.target.value))} disabled={!canEdit} />
            </div>

            <div className="rounded-lg border border-edge bg-panel2 px-3 py-2">
              <div className="mb-1 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-muted">
                <Wrench size={13} /> Effective VLAN membership
              </div>
              <div className="text-sm">
                {iface.attached ? (
                  <>
                    {iface.vlans.mode === 'access' ? (
                      <span>untagged VLAN {iface.vlans.untagged.join(', ') || '—'}</span>
                    ) : (
                      <span>
                        tagged {iface.vlans.tagged.join(', ') || '—'} · untagged {iface.vlans.untagged.join(', ') || '—'}
                      </span>
                    )}
                  </>
                ) : (
                  <span className="text-muted">not part of the switch</span>
                )}
              </div>
            </div>

            {error && <div className="rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-xs text-bad">{error}</div>}
            {!canEdit && <div className="text-xs text-warn">Your role is read-only; changes are disabled.</div>}
            {iface.kind === 'usb' && (
              <div className="flex items-start gap-1.5 text-[11px] text-muted">
                <Usb size={13} className="mt-0.5" /> USB Ethernet adapters may not support every offload; performance is limited by the USB bus.
              </div>
            )}
          </div>
        </div>
      </Modal>

      <ConfirmDialog
        open={!!confirmState}
        title="Confirm interface reassignment"
        message={confirmState?.message ?? ''}
        ops={confirmState?.ops}
        busy={busy}
        onCancel={() => setConfirmState(null)}
        onConfirm={async () => {
          if (!confirmState) return
          setConfirmState(null)
          try {
            await save(true, confirmState.payload)
          } catch (exc: any) {
            setError(exc?.message ?? String(exc))
          }
        }}
      />
    </>
  )
}

function Info({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className={`truncate text-right text-[12px] ${mono ? 'mono' : ''}`}>{value}</dd>
    </div>
  )
}
