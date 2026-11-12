import { useEffect, useState } from 'react'
import { BrowserRouter, NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import {
  Activity,
  Cable,
  LayoutDashboard,
  LogOut,
  Menu,
  Moon,
  Network,
  ScrollText,
  Settings2,
  Share2,
  SlidersHorizontal,
  Sun,
  Table2,
  Tags,
  TerminalSquare,
  Users,
  X,
} from 'lucide-react'
import { api, hasRole, setCsrf, type Role, type Session } from './api'
import { StatsProvider, useStats } from './stats'
import { Badge } from './components/ui'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import Ports from './pages/Ports'
import Interfaces from './pages/Interfaces'
import Vlans from './pages/Vlans'
import MacTable from './pages/MacTable'
import Topology from './pages/Topology'
import Monitoring from './pages/Monitoring'
import Configuration from './pages/Configuration'
import TerminalPage from './pages/Terminal'
import Advanced from './pages/Advanced'
import Logs from './pages/Logs'
import UsersPage from './pages/Users'

type NavEntry = { to: string; label: string; icon: any; minRole?: Role }

const NAV_GROUPS: { title: string; items: NavEntry[] }[] = [
  {
    title: 'Overview',
    items: [
      { to: '/', label: 'Dashboard', icon: LayoutDashboard },
      { to: '/ports', label: 'Switch ports', icon: Cable },
      { to: '/interfaces', label: 'Interfaces', icon: Network },
      { to: '/vlans', label: 'VLANs', icon: Tags },
      { to: '/mac', label: 'MAC table', icon: Table2 },
      { to: '/topology', label: 'Topology', icon: Share2 },
      { to: '/monitoring', label: 'Monitoring', icon: Activity },
    ],
  },
  {
    title: 'Operations',
    items: [
      { to: '/config', label: 'Configuration', icon: Settings2 },
      { to: '/terminal', label: 'Web terminal', icon: TerminalSquare },
      { to: '/advanced', label: 'Advanced', icon: SlidersHorizontal },
      { to: '/logs', label: 'Logs & events', icon: ScrollText },
      { to: '/users', label: 'Users & audit', icon: Users, minRole: 'admin' },
    ],
  },
]

function Sidebar({ session, onLogout, onNavigate }: { session: Session; onLogout: () => void; onNavigate?: () => void }) {
  const { snapshot, connected } = useStats()
  return (
    <div className="flex h-full flex-col border-r border-edge bg-panel/80 backdrop-blur">
      <div className="flex items-center gap-2.5 px-4 py-4">
        <div className="grid h-9 w-9 place-items-center rounded-lg border border-accent/40 bg-accent/10 text-accent">
          <Network size={18} />
        </div>
        <div className="leading-tight">
          <div className="text-sm font-semibold tracking-tight">MakeMeSwitch</div>
          <div className="text-[11px] uppercase tracking-wider text-muted">Manager</div>
        </div>
      </div>
      <nav className="flex-1 space-y-5 overflow-y-auto px-2 py-2">
        {NAV_GROUPS.map((group) => (
          <div key={group.title}>
            <div className="px-3 pb-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-muted/60">{group.title}</div>
            <div className="space-y-0.5">
              {group.items
                .filter((item) => !item.minRole || hasRole(session, item.minRole))
                .map((item) => (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    onClick={onNavigate}
                    className={({ isActive }) => `nav-item ${isActive ? 'nav-item-active' : ''}`}
                    end={item.to === '/'}
                  >
                    <item.icon size={16} />
                    {item.label}
                  </NavLink>
                ))}
            </div>
          </div>
        ))}
      </nav>
      <div className="space-y-2 border-t border-edge px-3 py-3">
        <div className="flex items-center justify-between text-[11px] text-muted">
          <span className="flex items-center gap-1.5">
            <span className={`h-1.5 w-1.5 rounded-full ${connected ? 'bg-ok' : 'bg-bad'}`} />
            {connected ? 'live' : 'reconnecting'}
          </span>
          <span className="mono">{snapshot?.ports.filter((p) => p.status === 'up').length ?? 0} up</span>
        </div>
        <div className="flex items-center justify-between rounded-lg border border-edge bg-panel2 px-2.5 py-2">
          <div className="min-w-0">
            <div className="truncate text-xs font-medium">{session.username}</div>
            <div className="text-[10px] uppercase tracking-wider text-muted">{session.role}</div>
          </div>
          <button className="text-muted transition hover:text-bad" onClick={onLogout} title="Sign out">
            <LogOut size={15} />
          </button>
        </div>
      </div>
    </div>
  )
}

function AppShell({ session, onLogout }: { session: Session; onLogout: () => void }) {
  const [mobileOpen, setMobileOpen] = useState(false)
  const [light, setLight] = useState(() => localStorage.getItem('mms-theme') === 'light')
  const location = useLocation()

  useEffect(() => {
    document.documentElement.classList.toggle('light', light)
    document.documentElement.classList.toggle('dark', !light)
    localStorage.setItem('mms-theme', light ? 'light' : 'dark')
  }, [light])

  return (
    <div className="flex h-full">
      <aside className="hidden w-60 shrink-0 md:block">
        <Sidebar session={session} onLogout={onLogout} />
      </aside>
      {mobileOpen && (
        <div className="fixed inset-0 z-40 md:hidden">
          <div className="absolute inset-0 bg-black/60" onClick={() => setMobileOpen(false)} />
          <div className="absolute inset-y-0 left-0 w-64">
            <Sidebar session={session} onLogout={onLogout} onNavigate={() => setMobileOpen(false)} />
          </div>
        </div>
      )}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-14 items-center justify-between gap-3 border-b border-edge bg-bg/80 px-4 backdrop-blur">
          <div className="flex items-center gap-2">
            <button className="btn !px-2 md:hidden" onClick={() => setMobileOpen(true)} aria-label="Open menu">
              <Menu size={16} />
            </button>
            <div className="text-sm font-medium">
              {NAV_GROUPS.flatMap((g) => g.items).find((i) => i.to === location.pathname)?.label ?? 'MakeMeSwitch'}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Badge tone={session.role}>{session.role}</Badge>
            <button className="btn !px-2" onClick={() => setLight((v) => !v)} title="Toggle theme">
              {light ? <Moon size={15} /> : <Sun size={15} />}
            </button>
          </div>
        </header>
        <main className="min-h-0 flex-1 overflow-y-auto p-4 md:p-6">
          <Routes>
            <Route path="/" element={<Dashboard session={session} />} />
            <Route path="/ports" element={<Ports session={session} />} />
            <Route path="/interfaces" element={<Interfaces session={session} />} />
            <Route path="/vlans" element={<Vlans session={session} />} />
            <Route path="/mac" element={<MacTable />} />
            <Route path="/topology" element={<Topology />} />
            <Route path="/monitoring" element={<Monitoring />} />
            <Route path="/config" element={<Configuration session={session} />} />
            <Route path="/terminal" element={<TerminalPage />} />
            <Route path="/advanced" element={<Advanced session={session} />} />
            <Route path="/logs" element={<Logs session={session} />} />
            {hasRole(session, 'admin') && <Route path="/users" element={<UsersPage session={session} />} />}
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  )
}

export default function App() {
  const [session, setSession] = useState<Session | null | undefined>(undefined)

  useEffect(() => {
    api
      .get<Session>('/api/v1/auth/session')
      .then((data) => {
        setCsrf(data.csrf)
        setSession(data)
      })
      .catch(() => setSession(null))
  }, [])

  useEffect(() => {
    const handler = () => setSession(null)
    window.addEventListener('osw:unauthorized', handler)
    return () => window.removeEventListener('osw:unauthorized', handler)
  }, [])

  const logout = async () => {
    try {
      await api.post('/api/v1/auth/logout')
    } catch {
      /* ignore */
    }
    setCsrf('')
    setSession(null)
  }

  if (session === undefined) {
    return (
      <div className="grid h-full place-items-center text-sm text-muted">
        <div className="flex items-center gap-2">
          <span className="h-2 w-2 animate-pulse rounded-full bg-accent" /> Connecting to MakeMeSwitch…
        </div>
      </div>
    )
  }

  if (session === null) {
    return (
      <Login
        onLogin={(data) => {
          setCsrf(data.csrf)
          setSession({ username: data.username, role: data.role as Role, csrf: data.csrf })
        }}
      />
    )
  }

  return (
    <StatsProvider>
      <BrowserRouter>
        <AppShell session={session} onLogout={logout} />
      </BrowserRouter>
    </StatsProvider>
  )
}
