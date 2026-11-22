import { useState, type FormEvent } from 'react'
import { api } from '../api'
import type { UserInfo } from '../types'

export default function Login({ onLoggedIn }: { onLoggedIn: (u: UserInfo) => void }) {
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true); setError('')
    try {
      const user = await api.post<UserInfo>('/api/auth/login', { username, password })
      onLoggedIn(user)
    } catch (err) {
      setError((err as Error).message || '登录失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <div className="login-brand">
          <div className="brand-mark large">SA</div>
          <h1>SubAngler</h1>
          <p>字幕自动化中心</p>
        </div>
        <form onSubmit={submit} className="login-form">
          <label>
            <span>用户名</span>
            <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required />
          </label>
          <label>
            <span>密码</span>
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password" placeholder="首次部署默认见 .env.example" required />
          </label>
          {error && <div className="error-box">{error}</div>}
          <button type="submit" className="btn btn-primary btn-block" disabled={busy}>
            {busy ? '登录中…' : '登录'}
          </button>
        </form>
        <p className="login-foot">凭证加密存储 · 会话 HttpOnly Cookie 保护</p>
      </div>
    </div>
  )
}
