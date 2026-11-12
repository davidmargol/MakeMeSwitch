// Wire format (unchanged API contract): trunk_allowed is an explicit VLAN ID list.
// "all" is the full range 1..4094, which the backend reports as `trunk_all`.
export const VLAN_MIN = 1
export const VLAN_MAX = 4094

export function isAllVlans(value: number[]): boolean {
  return value.length === VLAN_MAX - VLAN_MIN + 1 && value.every((v, i) => v === VLAN_MIN + i)
}

export function formatAllowedVlans(value: number[]): string {
  return isAllVlans(value) ? 'all' : value.join(',')
}

export function allowedVlanSummary(value: number[], max = 4): string {
  if (isAllVlans(value)) return 'all'
  return `${value.slice(0, max).join(',')}${value.length > max ? '…' : ''}`
}

export function parseAllowedVlans(text: string): number[] {
  const input = text.trim().toLowerCase()
  if (input === 'all') return Array.from({ length: VLAN_MAX - VLAN_MIN + 1 }, (_, i) => VLAN_MIN + i)
  if (!input) throw new Error('Specify allowed VLANs (1–4094), ranges, or all')
  const result = new Set<number>()
  for (const token of input.split(',')) {
    const match = /^(\d+)(?:\s*-\s*(\d+))?$/.exec(token.trim())
    if (!match) throw new Error(`Invalid VLAN list item: ${token}`)
    const first = Number(match[1])
    const last = Number(match[2] ?? match[1])
    if (first < VLAN_MIN || last > VLAN_MAX || first > last) throw new Error('VLAN IDs must be 1–4094 and ranges must ascend')
    for (let vlan = first; vlan <= last; vlan++) result.add(vlan)
  }
  return [...result].sort((a, b) => a - b)
}
