import { expect, test } from 'vitest'
import { allowedVlanSummary, formatAllowedVlans, isAllVlans, parseAllowedVlans } from './vlanInput'

test('parses single IDs, ranges and mixed lists into a sorted unique list', () => {
  expect(parseAllowedVlans('10')).toEqual([10])
  expect(parseAllowedVlans('10, 20-22,10')).toEqual([10, 20, 21, 22])
})

test('treats all as the full 1-4094 coordinate representation', () => {
  const all = parseAllowedVlans('ALL')
  expect(all).toHaveLength(4094)
  expect(isAllVlans(all)).toBe(true)
  expect(formatAllowedVlans(all)).toBe('all')
  expect(allowedVlanSummary(all)).toBe('all')
})

test('rejects empty, zero, inverted and out-of-range input with a readable message', () => {
  for (const bad of ['', '0', '30-20', '4095', '10,x']) {
    expect(() => parseAllowedVlans(bad)).toThrow(/VLAN|Invalid/)
  }
})
