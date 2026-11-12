export type Role = 'admin' | 'operator' | 'readonly'

export interface Session {
  username: string
  role: Role
  csrf: string
  expires_at?: number
}

export interface PortLink {
  name: string
  mac: string
  mtu: number
  operstate: string
  carrier: boolean
  link_up: boolean
  speed: number | null
  duplex: string | null
  driver: string | null
  identity: string | null
  kind: string
  pci_slot: string | null
  usb_path: string | null
  description: string
}

export interface IfaceVlans {
  mode: string
  untagged: number[]
  tagged: number[]
  native: number | null
}

export interface InterfaceRow {
  name: string
  linux_if: string | null
  mac: string | null
  identity: string | null
  kind: string
  present: boolean
  protected: boolean
  description: string
  attached: boolean
  admin_up: boolean
  mode: 'access' | 'trunk'
  access_vlan: number
  trunk_allowed: number[]
  native_vlan: number | null
  mtu: number
  channel_group: number | null
  link: Partial<PortLink>
  status: string
  stats: Record<string, number>
  vlans: IfaceVlans
}

export interface Vlan {
  id: number
  name: string
  description: string
  untagged: string[]
  tagged: string[]
}

export interface MacEntry {
  mac: string
  vlan: number
  vlan_name: string
  port: string
  linux_if: string
  age: number
  type: string
}

export interface EventRow {
  id: number
  ts: number
  severity: string
  source: string
  message: string
}

export interface AuditRow {
  id: number
  ts: number
  username: string
  ip: string | null
  action: string
  target: string | null
  result: string
  detail: string | null
}

export interface Feature {
  supported: boolean
  detail: string
  version?: string
}

export interface SystemInfo {
  hostname: string
  config_hostname: string
  makemeswitch_version: string
  project: string
  platform: string
  kernel: string
  python: string
  uptime_seconds: number
  cpu_count: number
  load: number[]
  cpu_percent: number
  memory_total: number
  memory_available: number
  memory_percent: number
  temperature_c: number | null
  bridge: string
  ovs: { installed: boolean; running: boolean; version: string | null; bridge_exists?: boolean; ports?: number; detail?: string }
  frr: { installed: boolean; running: boolean; version: string | null; detail?: string }
  lldp: { installed: boolean; enabled: boolean; neighbors: any[]; detail: string }
  ports_total: number
  ports_active: number
  vlans: number
  unsaved_changes: boolean
  startup_saved?: boolean
  events: EventRow[]
  summary: { hostname: string; vlans: number; ports: number; ports_assigned: number; trunks: number }
  features: Record<string, Feature>
  user: { username: string; role: Role }
}

export interface StatsPort {
  name: string
  linux_if: string
  status: string
  present: boolean
  attached: boolean
  admin_up: boolean
  mode: string
  vlans: IfaceVlans
  stats: Record<string, number>
  rates: { rx_bps: number; tx_bps: number; rx_pps: number; tx_pps: number }
}

export interface StatsSnapshot {
  ts: number
  ports: StatsPort[]
  totals: { rx_bytes: number; tx_bytes: number; rx_bps: number; tx_bps: number }
  system: {
    cpu_percent: number
    memory_percent: number
    memory_total: number
    memory_available: number
    temperature_c: number | null
    uptime_seconds: number
    ports_active: number
    ports_total: number
    vlans: number
    unsaved_changes: boolean
  }
}

export interface ConfigBundle {
  running: Record<string, any>
  startup: Record<string, any> | null
  running_text: string
  startup_text: string
  unsaved_changes: boolean
  drift: { severity: string; port?: string; message: string }[]
  history: { id: number; ts: number; action: string; user: string }[]
}

export interface Op {
  action: string
  target: string
  detail?: string
  interfaces?: string[]
}

export interface ApplyResult {
  changed: boolean
  ops: Op[]
  config: Record<string, any>
  unsaved_changes: boolean
}

let csrfToken = ''

export function setCsrf(token: string) {
  csrfToken = token
}

export class ApiError extends Error {
  status: number
  data: any
  constructor(message: string, status: number, data?: any) {
    super(message)
    this.status = status
    this.data = data
  }
}

export class ConfirmError extends ApiError {
  preview: { ops: Op[]; newly_attached?: string[] }
  constructor(message: string, data: any) {
    super(message, 409, data)
    this.preview = data?.preview ?? { ops: [] }
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (options.body !== undefined && !(options.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const method = (options.method ?? 'GET').toUpperCase()
  if (csrfToken && method !== 'GET') headers.set('X-CSRF-Token', csrfToken)
  const response = await fetch(path, { ...options, headers, credentials: 'same-origin' })
  const text = await response.text()
  let data: any = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (response.status === 401 && !path.includes('/auth/login')) {
    window.dispatchEvent(new Event('osw:unauthorized'))
  }
  if (!response.ok) {
    if (response.status === 409 && data?.requires_confirmation) {
      throw new ConfirmError(data.detail ?? 'confirmation required', data)
    }
    throw new ApiError(data?.detail ?? response.statusText, response.status, data)
  }
  if (method !== 'GET' && !path.includes('/auth/')) {
    window.dispatchEvent(new Event('osw:config-changed'))
  }
  return data as T
}

export const api = {
  get: <T,>(path: string) => request<T>(path),
  post: <T,>(path: string, body?: any) => request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  patch: <T,>(path: string, body: any) => request<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
  del: <T,>(path: string, body?: any) => request<T>(path, { method: 'DELETE', body: body === undefined ? undefined : JSON.stringify(body) }),
}

export function hasRole(session: Session | null, role: Role): boolean {
  const levels: Record<Role, number> = { readonly: 0, operator: 1, admin: 2 }
  if (!session) return false
  return levels[session.role] >= levels[role]
}

export function formatBytes(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let size = Number(value)
  let idx = 0
  while (size >= 1024 && idx < units.length - 1) {
    size /= 1024
    idx++
  }
  return `${idx === 0 ? size.toFixed(0) : size.toFixed(digits)} ${units[idx]}`
}

export function formatBps(value: number | null | undefined): string {
  if (!value) return '0 bps'
  const units = ['bps', 'Kbps', 'Mbps', 'Gbps']
  let size = Number(value)
  let idx = 0
  while (size >= 1000 && idx < units.length - 1) {
    size /= 1000
    idx++
  }
  return `${size.toFixed(idx === 0 ? 0 : 1)} ${units[idx]}`
}

export function formatUptime(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds))
  const days = Math.floor(total / 86400)
  const hours = Math.floor((total % 86400) / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  if (days > 0) return `${days}d ${hours}h ${minutes}m`
  if (hours > 0) return `${hours}h ${minutes}m`
  return `${minutes}m`
}

export function timeAgo(tsSeconds: number): string {
  const diff = Math.max(0, Date.now() / 1000 - tsSeconds)
  if (diff < 60) return `${Math.floor(diff)}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}
