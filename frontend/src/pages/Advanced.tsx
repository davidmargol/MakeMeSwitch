import { useEffect, useState } from 'react'
import { AlertCircle, CheckCircle2, Cpu, Network, ServerCog, ShieldCheck } from 'lucide-react'
import { api, hasRole, type Feature, type Session } from '../api'
import { Badge, Card, Empty, PageHeader, Toggle, Toast, useAsyncAction } from '../components/ui'

interface MonitoringSettings {
  banner: { enabled: boolean; login: string; motd: string }
  snmp: {
    enabled: boolean
    bind: string
    port: number
    version: string
    contact: string
    location: string
    sys_object_id: string
    communities: { name: string; access: string; sources: string[] }[]
  }
  syslog: { enabled: boolean; destination: string; transport: string; port: number; severity: string; fmt: string }
}

interface SettingsData extends MonitoringSettings {
  stp: { enabled: boolean; mode: string; priority: number }
  lldp_enabled: boolean
  ip_routing: boolean
  mirrors: any[]
  port_channels: any[]
  svis: any[]
  routes: any[]
  features: Record<string, Feature>
  frr: { installed: boolean; running: boolean; version: string | null; detail?: string }
}

interface ServicesData {
  ovs: { installed: boolean; running: boolean; version: string | null; bridge_exists?: boolean; ports?: number }
  frr: { installed: boolean; running: boolean; version: string | null }
  lldp: { installed: boolean; enabled: boolean; detail: string; neighbors: any[] }
  bridge: string
  web: { version: string; uptime: number }
}

export default function Advanced({ session }: { session: Session }) {
  const [settings, setSettings] = useState<SettingsData | null>(null)
  const [services, setServices] = useState<ServicesData | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const { busy, run } = useAsyncAction()
  const canEdit = hasRole(session, 'operator')

  const load = async () => {
    try {
      const [settingsData, servicesData] = await Promise.all([api.get<SettingsData>('/api/v1/settings'), api.get<ServicesData>('/api/v1/services')])
      setSettings(settingsData)
      setServices(servicesData)
      setError(null)
    } catch (exc: any) {
      setError(exc.message)
    }
  }

  useEffect(() => {
    load()
    const timer = setInterval(load, 15000)
    return () => clearInterval(timer)
  }, [])

  const patch = (payload: Record<string, any>, message: string) =>
    run(async () => {
      await api.patch('/api/v1/settings', payload)
      setToast(message)
      await load()
    })

  if (error) return <Card className="border-bad/40 p-4 text-sm text-bad">{error}</Card>
  if (!settings) return <Empty title="Loading capabilities…" />

  return (
    <div className="space-y-5">
      <PageHeader
        title="Advanced features"
        subtitle="Only capabilities actually provided by the installed backend are enabled"
        actions={<Badge tone={services?.ovs.running ? 'up' : 'bad'}>{services?.ovs.running ? 'OVS running' : 'OVS unavailable'}</Badge>}
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card className="p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
            <ShieldCheck size={16} /> Spanning tree
          </div>
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <div>
                <div className="text-sm">RSTP / STP</div>
                <div className="text-[11px] text-muted">Loop prevention for redundant links (access ports are RSTP edge)</div>
              </div>
              <Toggle checked={settings.stp.enabled && settings.stp.mode !== 'none'} onChange={(value) => patch({ stp: { enabled: value } }, `Spanning-tree ${value ? 'enabled' : 'disabled'}`)} disabled={!canEdit || busy} />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="label">Mode</label>
                <select
                  className="input"
                  value={settings.stp.mode}
                  onChange={(e) => patch({ stp: { mode: e.target.value } }, `Spanning-tree mode ${e.target.value}`)}
                  disabled={!canEdit}
                >
                  <option value="rstp">rstp</option>
                  <option value="stp">stp</option>
                  <option value="none">none</option>
                </select>
              </div>
              <div>
                <label className="label">Priority</label>
                <select
                  className="input"
                  value={settings.stp.priority}
                  onChange={(e) => patch({ stp: { priority: Number(e.target.value) } }, `Spanning-tree priority ${e.target.value}`)}
                  disabled={!canEdit}
                >
                  {[0, 4096, 8192, 16384, 24576, 32768, 40960, 49152, 61440].map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          </div>

          <div className="mt-5 space-y-3 border-t border-edge pt-4">
            <div className="flex items-center justify-between">
              <div>
                <div className="text-sm">LLDP</div>
                <div className="text-[11px] text-muted">{settings.features.lldp.detail}</div>
              </div>
              <Toggle checked={settings.lldp_enabled} onChange={(value) => patch({ lldp_enabled: value }, `LLDP ${value ? 'enabled' : 'disabled'}`)} disabled={!canEdit || !settings.features.lldp.supported} />
            </div>
            <div className="flex items-center justify-between">
              <div>
                <div className="text-sm">IP routing (SVIs + static routes)</div>
                <div className="text-[11px] text-muted">Requires Linux IPv4 forwarding; dynamic protocols need FRR</div>
              </div>
              <Toggle checked={settings.ip_routing} onChange={(value) => patch({ ip_routing: value }, `IP routing ${value ? 'enabled' : 'disabled'}`)} disabled={!canEdit} />
            </div>
          </div>
        </Card>

        <Card className="p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
            <ServerCog size={16} /> Services
          </div>
          <div className="space-y-2 text-sm">
            <ServiceRow label="Open vSwitch" ok={!!services?.ovs.running} detail={services?.ovs.version ?? 'unavailable'} />
            <ServiceRow label="Web panel" ok detail={`v${services?.web.version ?? ''}`} />
            <ServiceRow label="FRRouting" ok={!!services?.frr.installed} detail={services?.frr.version ?? 'not installed (optional)'} />
            <ServiceRow label="LLDP daemon" ok={!!services?.lldp.installed} detail={services?.lldp.installed ? 'lldpd present' : 'not installed (optional)'} />
          </div>
          <div className="mt-4 border-t border-edge pt-3">
            <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-muted">
              <Cpu size={13} /> Backend capability matrix
            </div>
            <div className="space-y-1.5">
              {Object.entries(settings.features).map(([name, feature]) => (
                <div key={name} className="flex items-start justify-between gap-3 text-xs">
                  <span className="font-medium capitalize">{name.replace('_', ' ')}</span>
                  <span className="flex max-w-[60%] items-start gap-1.5 text-right text-muted">
                    {feature.supported ? <CheckCircle2 size={13} className="mt-0.5 shrink-0 text-ok" /> : <AlertCircle size={13} className="mt-0.5 shrink-0 text-warn" />}
                    <span>{feature.detail}</span>
                  </span>
                </div>
              ))}
            </div>
          </div>
        </Card>

        <MonitoringCard settings={settings} canEdit={canEdit} busy={busy} patch={patch} />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
            <Network size={15} /> Port-channels / LACP
          </div>
          {settings.port_channels.length === 0 ? (
            <div className="text-xs text-muted">No port-channels. Configure members from the CLI: <span className="mono">channel-group 1 mode active</span>.</div>
          ) : (
            <div className="space-y-2 text-sm">
              {settings.port_channels.map((pc) => (
                <div key={pc.id} className="rounded-lg border border-edge bg-panel2 p-2">
                  <div className="flex items-center justify-between">
                    <span className="font-medium">Port-Channel{pc.id}</span>
                    <Badge tone="trunk">{pc.mode}</Badge>
                  </div>
                  <div className="mono mt-1 text-[11px] text-muted">{pc.members.join(', ')}</div>
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card className="p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
            <Network size={15} /> Port mirroring (SPAN)
          </div>
          {settings.mirrors.length === 0 ? (
            <div className="text-xs text-muted">No mirror sessions. Use <span className="mono">monitor session 1 source interface Ethernet1/1</span> in the CLI.</div>
          ) : (
            <div className="space-y-2 text-sm">
              {settings.mirrors.map((mirror) => (
                <div key={mirror.id} className="rounded-lg border border-edge bg-panel2 p-2">
                  <div className="flex items-center justify-between">
                    <span className="font-medium">Session {mirror.id}</span>
                    <Badge tone="info">{mirror.direction}</Badge>
                  </div>
                  <div className="text-[11px] text-muted">source: {mirror.source.join(', ')}</div>
                  <div className="text-[11px] text-muted">destination: {mirror.destination ?? 'unset'}</div>
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card className="p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
            <Network size={15} /> Layer 3
          </div>
          {settings.svis.length === 0 && settings.routes.length === 0 ? (
            <div className="text-xs text-muted">
              No SVIs or static routes. Enable IP routing, then add an <span className="mono">interface vlan 10</span> with <span className="mono">ip address</span> in the CLI.
            </div>
          ) : (
            <div className="space-y-2 text-sm">
              {settings.svis.map((svi) => (
                <div key={svi.vlan} className="flex items-center justify-between rounded-lg border border-edge bg-panel2 p-2">
                  <span className="font-medium">Vlan{svi.vlan}</span>
                  <span className="mono text-[11px]">{svi.ip ?? 'no address'}</span>
                </div>
              ))}
              {settings.routes.map((route) => (
                <div key={route.prefix} className="mono flex items-center justify-between text-[11px] text-muted">
                  <span>{route.prefix}</span>
                  <span>via {route.nexthop ?? route.interface}</span>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>

      <Toast message={toast} onDone={() => setToast(null)} />
    </div>
  )
}

function ServiceRow({ label, ok, detail }: { label: string; ok: boolean; detail: string }) {
  return (
    <div className="flex items-center justify-between rounded-lg border border-edge bg-panel2 px-3 py-2">
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 rounded-full ${ok ? 'bg-ok' : 'bg-bad'}`} />
        <span className="text-sm">{label}</span>
      </div>
      <span className="mono text-[11px] text-muted">{detail}</span>
    </div>
  )
}

function MonitoringCard({
  settings,
  canEdit,
  busy,
  patch,
}: {
  settings: SettingsData
  canEdit: boolean
  busy: boolean
  patch: (payload: Record<string, any>, message: string) => void
}) {
  const [login, setLogin] = useState(settings.banner.login)
  const [motd, setMotd] = useState(settings.banner.motd)
  const [contact, setContact] = useState(settings.snmp.contact)
  const [location, setLocation] = useState(settings.snmp.location)
  const [snmpBind, setSnmpBind] = useState(settings.snmp.bind)
  const [snmpPort, setSnmpPort] = useState(String(settings.snmp.port))
  const [sysObjectId, setSysObjectId] = useState(settings.snmp.sys_object_id)
  const [communities, setCommunities] = useState<{ name: string; sources: string }[]>(
    settings.snmp.communities.map((c) => ({ name: c.name, sources: c.sources.join(', ') })),
  )
  const [newCommunity, setNewCommunity] = useState('')
  const [destination, setDestination] = useState(settings.syslog.destination)
  const [syslogPort, setSyslogPort] = useState(String(settings.syslog.port))

  useEffect(() => {
    setLogin(settings.banner.login)
    setMotd(settings.banner.motd)
    setContact(settings.snmp.contact)
    setLocation(settings.snmp.location)
    setSnmpBind(settings.snmp.bind)
    setSnmpPort(String(settings.snmp.port))
    setSysObjectId(settings.snmp.sys_object_id)
    setCommunities(settings.snmp.communities.map((c) => ({ name: c.name, sources: c.sources.join(', ') })))
    setDestination(settings.syslog.destination)
    setSyslogPort(String(settings.syslog.port))
  }, [settings])

  return (
    <Card className="p-4">
      <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
        <ServerCog size={16} /> Monitoring and banner
      </div>

      <div className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-sm">SNMP v2c agent</div>
            <div className="text-[11px] text-muted">
              Read-only, MIB-II + IF-MIB + BRIDGE-MIB for LibreNMS / Infoblox
            </div>
          </div>
          <Toggle
            checked={settings.snmp.enabled}
            onChange={(value) => patch({ snmp: { enabled: value } }, `SNMP ${value ? 'enabled' : 'disabled'}`)}
            disabled={!canEdit || busy}
          />
        </div>
        <div className="grid grid-cols-3 gap-2">
          <div>
            <label className="label">Version</label>
            <select
              className="input"
              value={settings.snmp.version}
              onChange={(e) => {
                if (e.target.value === 'v3') return
                patch({ snmp: { version: e.target.value } }, `SNMP version ${e.target.value}`)
              }}
              disabled={!canEdit || busy}
            >
              <option value="v2c">v2c</option>
              <option value="v1">v1</option>
              <option value="v3" disabled>
                v3 (not implemented)
              </option>
            </select>
          </div>
          <div>
            <label className="label">UDP port</label>
            <input className="input" value={snmpPort} onChange={(e) => setSnmpPort(e.target.value)} disabled={!canEdit} />
          </div>
          <div>
            <label className="label">Bind</label>
            <input className="input" value={snmpBind} onChange={(e) => setSnmpBind(e.target.value)} disabled={!canEdit} />
          </div>
        </div>

        <div>
          <label className="label">Communities (read-only)</label>
          <div className="space-y-1">
            {communities.length === 0 && <div className="text-[11px] text-muted">none — the agent serves nothing</div>}
            {communities.map((c, index) => (
              <div key={index} className="grid grid-cols-[1fr_1.4fr_auto] items-center gap-2">
                <input
                  className="input mono"
                  value={c.name}
                  placeholder="public"
                  onChange={(e) => {
                    const next = [...communities]
                    next[index] = { ...next[index], name: e.target.value }
                    setCommunities(next)
                  }}
                  disabled={!canEdit}
                />
                <input
                  className="input mono"
                  value={c.sources}
                  placeholder="any source — or 10.0.0.0/21, 192.168.0.0/16"
                  onChange={(e) => {
                    const next = [...communities]
                    next[index] = { ...next[index], sources: e.target.value }
                    setCommunities(next)
                  }}
                  disabled={!canEdit}
                />
                <button
                  className="btn btn-sm"
                  onClick={() => setCommunities(communities.filter((_, i) => i !== index))}
                  disabled={!canEdit}
                >
                  Remove
                </button>
              </div>
            ))}
          </div>
          <div className="mt-2 flex gap-2">
            <input
              className="input mono flex-1"
              placeholder="new community name (e.g. monitor)"
              value={newCommunity}
              onChange={(e) => setNewCommunity(e.target.value)}
              disabled={!canEdit}
            />
            <button
              className="btn btn-sm"
              disabled={!canEdit || !newCommunity.trim()}
              onClick={() => {
                setCommunities([...communities, { name: newCommunity.trim(), sources: '' }])
                setNewCommunity('')
              }}
            >
              Add
            </button>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="label">Contact</label>
            <input className="input" value={contact} onChange={(e) => setContact(e.target.value)} disabled={!canEdit} />
          </div>
          <div>
            <label className="label">Location</label>
            <input className="input" value={location} onChange={(e) => setLocation(e.target.value)} disabled={!canEdit} />
          </div>
        </div>
        <div>
          <label className="label">sysObjectID (optional)</label>
          <input
            className="input mono"
            placeholder="1.3.6.1.4.1.<your-PEN>.1"
            value={sysObjectId}
            onChange={(e) => setSysObjectId(e.target.value)}
            disabled={!canEdit}
          />
        </div>
        <button
          className="btn btn-sm"
          disabled={!canEdit || busy}
          onClick={() =>
            patch(
              {
                snmp: {
                  contact,
                  location,
                  bind: snmpBind,
                  port: Number(snmpPort),
                  sys_object_id: sysObjectId,
                  communities: communities
                    .filter((c) => c.name.trim())
                    .map((c) => ({
                      name: c.name.trim(),
                      access: 'ro',
                      sources: c.sources
                        .split(/[\s,]+/)
                        .map((v) => v.trim())
                        .filter(Boolean),
                    })),
                },
              },
              'SNMP settings saved',
            )
          }
        >
          Save SNMP settings
        </button>

        <div className="space-y-2 border-t border-edge pt-3">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-sm">Remote syslog</div>
              <div className="text-[11px] text-muted">Switch events to a collector (Infoblox, LibreNMS, SIEM)</div>
            </div>
            <Toggle
              checked={settings.syslog.enabled}
              onChange={(value) => patch({ syslog: { enabled: value } }, `Remote syslog ${value ? 'enabled' : 'disabled'}`)}
              disabled={!canEdit || busy || (!destination && !settings.syslog.destination)}
            />
          </div>
          <div className="grid grid-cols-3 gap-2">
            <div className="col-span-2">
              <label className="label">Collector</label>
              <input
                className="input"
                placeholder="10.0.0.10"
                value={destination}
                onChange={(e) => setDestination(e.target.value)}
                disabled={!canEdit}
              />
            </div>
            <div>
              <label className="label">Port</label>
              <input
                className="input"
                value={syslogPort}
                onChange={(e) => setSyslogPort(e.target.value)}
                disabled={!canEdit}
              />
            </div>
          </div>
          <div className="grid grid-cols-3 gap-2">
            <select
              className="input"
              value={settings.syslog.transport}
              onChange={(e) => patch({ syslog: { transport: e.target.value } }, `Syslog transport ${e.target.value}`)}
              disabled={!canEdit}
            >
              <option value="udp">udp</option>
              <option value="tcp">tcp</option>
            </select>
            <select
              className="input"
              value={settings.syslog.severity}
              onChange={(e) => patch({ syslog: { severity: e.target.value } }, `Syslog severity ${e.target.value}`)}
              disabled={!canEdit}
            >
              {['emergency', 'alert', 'critical', 'error', 'warning', 'notice', 'info', 'debug'].map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
            <select
              className="input"
              value={settings.syslog.fmt}
              onChange={(e) => patch({ syslog: { fmt: e.target.value } }, `Syslog format ${e.target.value}`)}
              disabled={!canEdit}
            >
              <option value="rfc5424">rfc5424</option>
              <option value="rfc3164">rfc3164</option>
            </select>
          </div>
          <button
            className="btn btn-sm"
            disabled={!canEdit || busy || !destination}
            onClick={() => patch({ syslog: { destination, port: Number(syslogPort) } }, 'Syslog collector saved')}
          >
            Save collector
          </button>
        </div>

        <div className="space-y-2 border-t border-edge pt-3">
          <div className="text-sm">Banner</div>
          <textarea
            className="input mono h-16"
            placeholder="login banner (shown before login)"
            value={login}
            onChange={(e) => setLogin(e.target.value)}
            disabled={!canEdit}
          />
          <textarea
            className="input mono h-16"
            placeholder="MOTD (shown after login)"
            value={motd}
            onChange={(e) => setMotd(e.target.value)}
            disabled={!canEdit}
          />
          <button
            className="btn btn-sm"
            disabled={!canEdit || busy}
            onClick={() => patch({ banner: { enabled: Boolean(login || motd), login, motd } }, 'Banner saved')}
          >
            Save banner
          </button>
        </div>
      </div>
    </Card>
  )
}
