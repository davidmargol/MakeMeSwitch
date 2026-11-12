import { useState } from 'react'
import { Network, Lock, User } from 'lucide-react'
import { api } from '../api'

export default function Login({ onLogin }: { onLogin: (data: { username: string; role: string; csrf: string }) => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const data = await api.post<{ username: string; role: string; csrf: string }>('/api/v1/auth/login', { username, password })
      onLogin(data)
    } catch (exc: any) {
      setError(exc?.message ?? 'Login failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center p-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex flex-col items-center gap-2 text-center">
          <div className="grid h-12 w-12 place-items-center rounded-xl border border-accent/40 bg-accent/10 text-accent">
            <Network size={24} />
          </div>
          <div>
            <h1 className="text-lg font-semibold tracking-tight">MakeMeSwitch Manager</h1>
            <p className="text-xs text-muted">Sign in to manage the switch</p>
          </div>
        </div>
        <form onSubmit={submit} className="card space-y-3 p-5">
          <div>
            <label className="label">Username</label>
            <div className="relative">
              <User size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
              <input className="input pl-9" value={username} onChange={(e) => setUsername(e.target.value)} autoFocus autoComplete="username" />
            </div>
          </div>
          <div>
            <label className="label">Password</label>
            <div className="relative">
              <Lock size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
              <input
                className="input pl-9"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
              />
            </div>
          </div>
          {error && <div className="rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-xs text-bad">{error}</div>}
          <button className="btn-primary w-full justify-center" disabled={busy || !username}>
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
          <p className="text-center text-[11px] leading-relaxed text-muted">
            The panel is reachable from the LAN. Use HTTPS on untrusted networks.
          </p>
        </form>
      </div>
    </div>
  )
}
