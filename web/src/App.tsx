import { useEffect, useState } from 'react'
import { NavLink, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { api, ApiError } from './api'
import type { UserInfo } from './types'
import { Spinner, ToastStack, useToasts } from './components/ui'
import Dashboard from './pages/Dashboard'
import Libraries from './pages/Libraries'
import Videos from './pages/Videos'
import Jobs from './pages/Jobs'
import Candidates from './pages/Candidates'
import Providers from './pages/Providers'
import Settings from './pages/Settings'
import Login from './pages/Login'

const NAV = [
  { to: '/', label: '总览', icon: '▦', end: true },
  { to: '/libraries', label: '媒体库', icon: '▤' },
  { to: '/videos', label: '剧集', icon: '▶' },
  { to: '/jobs', label: '任务中心', icon: '⟳' },
  { to: '/candidates', label: '候选挑选', icon: '☷' },
  { to: '/providers', label: '字幕源', icon: '⚙' },
  { to: '/settings', label: '设置', icon: '⚙︎' },
]

export default function App() {
  const [user, setUser] = useState<UserInfo | null | undefined>(undefined)
  const [healthOk, setHealthOk] = useState<boolean | null>(null)
  const { toasts, push } = useToasts()
  const navigate = useNavigate()
  const location = useLocation()

  useEffect(() => {
    api.get<UserInfo>('/api/auth/me')
      .then(setUser)
      .catch((e) => { if (e instanceof ApiError && e.status === 401) setUser(null); else setUser(null) })
    api.get<{ ok: boolean }>('/api/health').then((r) => setHealthOk(r.ok)).catch(() => setHealthOk(false))
  }, [])

  if (user === undefined) return <div className="app-loading"><Spinner /></div>

  if (!user) {
    return (
      <>
        <Routes>
          <Route path="*" element={<Login onLoggedIn={(u) => { setUser(u); navigate('/') }} />} />
        </Routes>
        <ToastStack toasts={toasts} />
      </>
    )
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">SA</div>
          <div>
            <div className="brand-name">SubAngler</div>
            <div className="brand-tag">字幕自动化中心</div>
          </div>
        </div>
        <nav className="nav">
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.end}
              className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}
            >
              <span className="nav-icon">{n.icon}</span>
              <span>{n.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className="health-chip" title="系统健康状态">
            <span className={`dot dot-${healthOk === null ? 'muted' : healthOk ? 'ok' : 'error'}`} />
            系统{healthOk === null ? '检查中' : healthOk ? '正常' : '异常'}
          </div>
          <button className="user-chip" onClick={async () => {
            await api.post('/api/auth/logout')
            setUser(null)
            navigate('/login')
          }} title="点击退出登录">
            <span className="avatar">{user.username.slice(0, 1).toUpperCase()}</span>
            <span className="user-name">{user.username}</span>
            <span className="logout-hint">退出</span>
          </button>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div className="crumb">{NAV.find((n) => n.to === location.pathname)?.label || 'SubAngler'}</div>
          <div className="topbar-right">
            <span className="version">v0.1.0 · M3</span>
          </div>
        </header>
        <div className="content">
          <Routes>
            <Route path="/" element={<Dashboard push={push} />} />
            <Route path="/libraries" element={<Libraries push={push} />} />
            <Route path="/videos" element={<Videos push={push} />} />
            <Route path="/jobs" element={<Jobs push={push} />} />
            <Route path="/candidates" element={<Candidates push={push} />} />
            <Route path="/providers" element={<Providers push={push} />} />
            <Route path="/settings" element={<Settings push={push} />} />
          </Routes>
        </div>
      </main>
      <ToastStack toasts={toasts} />
    </div>
  )
}
