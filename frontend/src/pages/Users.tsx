import { useEffect, useState } from 'react'
import { KeyRound, Plus, Trash2 } from 'lucide-react'
import { api, timeAgo, type Session } from '../api'
import { Badge, Card, Empty, Modal, PageHeader, Toast } from '../components/ui'

interface UserRow {
  username: string
  role: string
  created_at: number
  disabled: number
  must_change_password: number
}

export default function Users({ session }: { session: Session }) {
  const [users, setUsers] = useState<UserRow[]>([])
  const [error, setError] = useState<string | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [passwordOpen, setPasswordOpen] = useState(false)
  const [form, setForm] = useState({ username: '', password: '', role: 'readonly' })
  const [own, setOwn] = useState({ current: '', next: '' })

  const load = async () => {
    try {
      const data = await api.get<{ users: UserRow[] }>('/api/v1/users')
      setUsers(data.users)
      setError(null)
    } catch (exc: any) {
      setError(exc.message)
    }
  }

  useEffect(() => {
    load()
  }, [])

  const create = async () => {
    try {
      await api.post('/api/v1/users', form)
      setCreateOpen(false)
      setForm({ username: '', password: '', role: 'readonly' })
      setToast('User created')
      load()
    } catch (exc: any) {
      setError(exc.message)
    }
  }

  const remove = async (username: string) => {
    try {
      await api.del(`/api/v1/users/${encodeURIComponent(username)}`)
      setToast('User deleted')
      load()
    } catch (exc: any) {
      setError(exc.message)
    }
  }

  const changePassword = async () => {
    try {
      await api.post('/api/v1/auth/password', { current_password: own.current, new_password: own.next })
      setPasswordOpen(false)
      setOwn({ current: '', next: '' })
      setToast('Password changed')
    } catch (exc: any) {
      setError(exc.message)
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Users & access"
        subtitle="Argon2id password hashing, role-based access and audit logging"
        actions={
          <>
            <button className="btn" onClick={() => setPasswordOpen(true)}>
              <KeyRound size={15} /> Change my password
            </button>
            <button className="btn-primary" onClick={() => setCreateOpen(true)}>
              <Plus size={15} /> New user
            </button>
          </>
        }
      />

      {error && <Card className="border-bad/40 p-4 text-sm text-bad">{error}</Card>}

      <Card className="overflow-hidden">
        <table className="w-full">
          <thead className="bg-panel2/60">
            <tr>
              <th className="th">Username</th>
              <th className="th">Role</th>
              <th className="th">Created</th>
              <th className="th">Status</th>
              <th className="th"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-edge">
            {users.map((user) => (
              <tr key={user.username} className="hover:bg-panel2/50">
                <td className="td font-medium">
                  {user.username}
                  {user.username === session.username && <span className="ml-2 text-[10px] text-muted">(you)</span>}
                </td>
                <td className="td">
                  <Badge tone={user.role}>{user.role}</Badge>
                </td>
                <td className="td text-xs text-muted">{timeAgo(user.created_at)}</td>
                <td className="td text-xs">{user.disabled ? <Badge tone="down">disabled</Badge> : <Badge tone="up">active</Badge>}</td>
                <td className="td text-right">
                  {user.username !== session.username && (
                    <button className="btn-danger !px-2 !py-1" onClick={() => remove(user.username)}>
                      <Trash2 size={13} />
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {users.length === 0 && <Empty title="No users" />}
      </Card>

      <Modal
        open={createOpen}
        title="Create user"
        onClose={() => setCreateOpen(false)}
        footer={
          <>
            <button className="btn" onClick={() => setCreateOpen(false)}>
              Cancel
            </button>
            <button className="btn-primary" onClick={create} disabled={!form.username || form.password.length < 10}>
              <Plus size={14} /> Create
            </button>
          </>
        }
      >
        <div className="space-y-3">
          <div>
            <label className="label">Username</label>
            <input className="input" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
          </div>
          <div>
            <label className="label">Password (min 10 characters)</label>
            <input className="input" type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
          </div>
          <div>
            <label className="label">Role</label>
            <select className="input" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>
              <option value="readonly">readonly — view only</option>
              <option value="operator">operator — configure switch</option>
              <option value="admin">admin — users, rollback, audit</option>
            </select>
          </div>
        </div>
      </Modal>

      <Modal
        open={passwordOpen}
        title="Change password"
        onClose={() => setPasswordOpen(false)}
        footer={
          <>
            <button className="btn" onClick={() => setPasswordOpen(false)}>
              Cancel
            </button>
            <button className="btn-primary" onClick={changePassword} disabled={own.next.length < 10 || !own.current}>
              Save
            </button>
          </>
        }
      >
        <div className="space-y-3">
          <div>
            <label className="label">Current password</label>
            <input className="input" type="password" value={own.current} onChange={(e) => setOwn({ ...own, current: e.target.value })} />
          </div>
          <div>
            <label className="label">New password (min 10 characters)</label>
            <input className="input" type="password" value={own.next} onChange={(e) => setOwn({ ...own, next: e.target.value })} />
          </div>
        </div>
      </Modal>

      <Toast message={toast} onDone={() => setToast(null)} />
    </div>
  )
}
