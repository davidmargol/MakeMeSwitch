// @vitest-environment jsdom
import React from 'react'
import { afterEach, expect, test, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { api, ConfirmError, type InterfaceRow } from './api'
import InterfaceEditor from './components/InterfaceEditor'
import Vlans from './pages/Vlans'

const session = { username: 'admin', role: 'admin' as const, csrf: '' }
const vlan = { id: 1, name: 'default', description: '', tagged: [], untagged: [] }
const iface = { name: 'Ethernet1/1', description: '', admin_up: true, attached: false, mode: 'access', access_vlan: 1, trunk_allowed: [1], native_vlan: null, mtu: 1500, stats: {}, link: {}, vlans: { mode: 'access', tagged: [], untagged: [], native: null } } as unknown as InterfaceRow

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })

test('attach confirmation reaches the dialog and retries only after explicit confirmation', async () => {
  const patch = vi.spyOn(api, 'patch').mockRejectedValueOnce(new ConfirmError('Attach may disconnect this NIC', { preview: { ops: [] } })).mockResolvedValue({ ops: [] })
  const saved = vi.fn()
  render(<InterfaceEditor iface={iface} vlans={[vlan]} canEdit onClose={() => {}} onSaved={saved} />)
  fireEvent.click(screen.getByText('Apply changes'))
  await screen.findByText('Confirm interface reassignment')
  expect(patch).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
  await waitFor(() => expect(saved).toHaveBeenCalledTimes(1))
  expect(patch.mock.calls[1][1]).toMatchObject({ confirm: true })
})

test('creating a VLAN refreshes the list even when its callback returns void', async () => {
  let created = false
  vi.spyOn(api, 'get').mockImplementation(async () => ({ vlans: created ? [vlan, { ...vlan, id: 200, name: 'NEW' }] : [vlan], interfaces: [] }) as any)
  vi.spyOn(api, 'post').mockImplementation(async () => { created = true; return {} as any })
  render(<Vlans session={session} />)
  await screen.findByText('default')
  fireEvent.click(screen.getByText('New VLAN'))
  fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '200' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create' }))
  await screen.findByText('NEW')
})

test('external CLI changes refresh on focus without overwriting an unsaved trunk draft', async () => {
  let changed = false
  vi.spyOn(api, 'get').mockImplementation(async () => ({ vlans: [{ ...vlan, name: changed ? 'CLI' : 'default' }], interfaces: [{ ...iface }] }) as any)
  render(<Vlans session={session} />)
  await screen.findByText('default')
  fireEvent.change(screen.getAllByRole('combobox')[0], { target: { value: 'trunk' } })
  changed = true
  fireEvent(window, new Event('focus'))
  await screen.findByText('CLI')
  expect((screen.getAllByRole('combobox')[0] as HTMLSelectElement).value).toBe('trunk')
})

test('trunk all is transmitted as an explicit all policy, not NaN/null', async () => {
  vi.spyOn(api, 'get').mockResolvedValue({ vlans: [vlan], interfaces: [iface] } as any)
  const patch = vi.spyOn(api, 'patch').mockResolvedValue({})
  render(<Vlans session={session} />)
  await screen.findByText('default')
  fireEvent.change(screen.getAllByRole('combobox')[0], { target: { value: 'trunk' } })
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'all' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save' }))
  await waitFor(() => expect(patch).toHaveBeenCalled())
  const sent = patch.mock.calls[0][1] as any
  expect(sent.mode).toBe('trunk')
  // Existing API contract carries an explicit VLAN list; full range == trunk all.
  expect(sent.trunk_allowed).toHaveLength(4094)
  expect([sent.trunk_allowed[0], sent.trunk_allowed.at(-1)]).toEqual([1, 4094])
})
