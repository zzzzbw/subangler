import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import type { ConfigItem, ConfigAuditItem, HealthReport } from '../types'
import { Badge, Card, Dot, Empty, ErrorBox, Spinner, useToasts } from '../components/ui'

type ToastPush = ReturnType<typeof useToasts>['push']

type Group = { key: string; label: string; icon: string; match: (k: string) => boolean }
const GROUPS: Group[] = [
  { key: 'general', label: '通用', icon: '⚙', match: (k) => k.startsWith('general.') },
  { key: 'library', label: '媒体库', icon: '▤', match: (k) => k.startsWith('library.') },
  { key: 'providers', label: '字幕源', icon: '☷', match: (k) => k.startsWith('providers.') },
  { key: 'credentials', label: '凭证', icon: '🔑', match: (k) => k.startsWith('credentials.') },
  { key: 'output', label: '落盘', icon: '▣', match: (k) => k.startsWith('output.') },
  { key: 'security', label: '安全', icon: '🛡', match: (k) => k.startsWith('security.') },
  { key: 'notify', label: '通知', icon: '🔔', match: (k) => k.startsWith('notify.') },
]

export default function Settings({ push }: { push: ToastPush }) {
  const [items, setItems] = useState<ConfigItem[] | null>(null)
  const [version, setVersion] = useState(0)
  const [error, setError] = useState('')
  const [activeGroup, setActiveGroup] = useState('general')
  const [draft, setDraft] = useState<Record<string, unknown>>({})
  const [revealed, setRevealed] = useState<Set<string>>(new Set())
  const [saving, setSaving] = useState(false)
  const [audit, setAudit] = useState<ConfigAuditItem[]>([])
  const [health, setHealth] = useState<HealthReport | null>(null)
  const [testState, setTestState] = useState<Record<string, string>>({})

  const load = useCallback(async () => {
    try {
      const r = await api.get<{ version: number; items: ConfigItem[] }>('/api/config')
      setItems(r.items); setVersion(r.version)
      const d: Record<string, unknown> = {}
      r.items.forEach((i) => { d[i.key] = i.value })
      setDraft(d); setError('')
    } catch (e) { setError((e as Error).message) }
  }, [])
  useEffect(() => { load() }, [load])

  const loadAux = useCallback(async () => {
    api.get<{ items: ConfigAuditItem[] }>('/api/config/audit?limit=20').then((r) => setAudit(r.items)).catch(() => {})
    api.get<HealthReport>('/api/health/full').then(setHealth).catch(() => {})
  }, [])
  useEffect(() => { loadAux() }, [loadAux])

  const dirty = useMemo(() => {
    if (!items) return false
    return items.some((i) => JSON.stringify(draft[i.key]) !== JSON.stringify(i.value))
  }, [items, draft])

  function setField(key: string, value: unknown) { setDraft((d) => ({ ...d, [key]: value })) }

  async function save() {
    if (!dirty) return
    setSaving(true)
    const changed = items!.filter((i) => JSON.stringify(draft[i.key]) !== JSON.stringify(i.value))
    const values: Record<string, unknown> = {}
    changed.forEach((i) => { values[i.key] = draft[i.key] })
    try {
      await api.patch('/api/config', { values })
      push(`${changed.length} 项配置已保存，即时生效，无需重启`)
      await load(); await loadAux()
    } catch (e) { push((e as Error).message, 'error') }
    finally { setSaving(false) }
  }

  async function testCredential(provider: string, key: string) {
    const value = String(draft[key] || '')
    if (!value || value === '****') { push('请先填写凭证', 'error'); return }
    setTestState((s) => ({ ...s, [key]: '测试中…' }))
    const body: Record<string, string> = { provider }
    if (provider === 'assrt') body.token = value; else body.api_key = value
    try {
      const r = await api.post<{ ok: boolean; detail: string }>('/api/config/test-connection', body)
      setTestState((s) => ({ ...s, [key]: r.ok ? '✓ 连接成功' : `✗ ${r.detail}` }))
      push(r.ok ? '连接成功' : `连接失败: ${r.detail}`, r.ok ? 'ok' : 'error')
    } catch (e) {
      setTestState((s) => ({ ...s, [key]: `✗ ${(e as Error).message}` }))
    }
  }

  async function testFlare(key: string) {
    const url = String(draft[key] || '')
    setTestState((s) => ({ ...s, [key]: '检测中…' }))
    try {
      const r = await api.post<{ ok: boolean; detail: string }>('/api/config/test-flaresolverr', { url })
      setTestState((s) => ({ ...s, [key]: r.ok ? `✓ ${r.detail}` : `✗ ${r.detail}` }))
      push(r.ok ? `FlareSolverr 可用：${r.detail}` : `FlareSolverr 不可用：${r.detail}`, r.ok ? 'ok' : 'error')
    } catch (e) {
      setTestState((s) => ({ ...s, [key]: `✗ ${(e as Error).message}` }))
    }
  }

  async function exportConfig() {
    try {
      const data = await api.get<Record<string, unknown>>('/api/config/export')
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      a.download = `subangler-config-${new Date().toISOString().slice(0, 10)}.json`
      a.click(); URL.revokeObjectURL(a.href)
      push('配置已导出')
    } catch (e) { push((e as Error).message, 'error') }
  }

  if (items === null) return error ? <ErrorBox message={error} onRetry={load} /> : <Spinner />

  const groupItems = items.filter((i) => GROUPS.find((g) => g.key === activeGroup)?.match(i.key))
  const credentialTestMap: Record<string, string> = {
    'credentials.opensubtitles_api_key': 'opensubtitles_com',
    'credentials.assrt_token': 'assrt',
    'credentials.subdl_api_key': 'subdl',
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>设置</h1>
          <p className="page-sub">所有配置均在此维护，敏感项加密存储 · 版本 #{version}</p>
        </div>
        <div className="head-actions">
          <button className="btn" onClick={exportConfig}>↓ 导出配置</button>
          <button className="btn btn-primary" onClick={save} disabled={!dirty || saving}>
            {saving ? '保存中…' : dirty ? `● 保存 ${items.filter((i) => JSON.stringify(draft[i.key]) !== JSON.stringify(i.value)).length} 项更改` : '✓ 已保存'}
          </button>
        </div>
      </div>

      <div className="settings-layout">
        <nav className="settings-nav">
          {GROUPS.map((g) => {
            const count = items.filter((i) => g.match(i.key)).length
            return count > 0 && (
              <button key={g.key} className={activeGroup === g.key ? 'active' : ''} onClick={() => setActiveGroup(g.key)}>
                <span>{g.icon}</span>{g.label}<span className="nav-count">{count}</span>
              </button>
            )
          })}
        </nav>

        <div className="settings-content">
          <Card title={GROUPS.find((g) => g.key === activeGroup)?.label}
            subtitle="改动点击右上角保存后立即生效">
            {groupItems.length === 0 ? <Empty text="该分组暂无配置项" /> : (
              <div className="config-list">
                {groupItems.map((item) => (
                  <ConfigField key={item.key} item={item} value={draft[item.key]}
                    revealed={revealed.has(item.key)}
                    onReveal={() => setRevealed((s) => { const n = new Set(s); n.has(item.key) ? n.delete(item.key) : n.add(item.key); return n })}
                    onChange={(v) => setField(item.key, v)}
                    testProvider={credentialTestMap[item.key]}
                    testState={testState[item.key]}
                    onTest={() => testCredential(credentialTestMap[item.key], item.key)}
                    onTestFlare={item.key === 'providers.flaresolverr_url' ? () => testFlare(item.key) : undefined} />
                ))}
              </div>
            )}
          </Card>

          <Card title="系统自检" subtitle="依赖、权限、网络与凭证连通性">
            {!health ? <Spinner /> : (
              <div className="check-list">
                {health.checks.map((c) => (
                  <div key={c.name} className="check-row">
                    <Dot tone={c.status} />
                    <span className="check-name">{c.name}</span>
                    <span className="check-detail">{c.detail || '—'}</span>
                    {c.fix && <span className="check-fix">建议: {c.fix}</span>}
                  </div>
                ))}
              </div>
            )}
          </Card>

          <Card title="修改密码">
            <ChangePassword push={push} />
          </Card>

          <Card title="最近配置变更" subtitle="敏感项只记录变更动作，不记录明文">
            {audit.length === 0 ? <Empty text="暂无变更记录" /> : (
              <div className="audit-list">
                {audit.map((a) => (
                  <div key={a.id} className="audit-row">
                    <span className="audit-key" title={a.key}>{configKeyLabel(a.key)}</span>
                    <span className="audit-change" title={JSON.stringify({ 旧: a.old_value, 新: a.new_value })}>
                      {a.summary || (a.new_value === '****'
                        ? <Badge tone="warn">敏感值已更新</Badge>
                        : <>{formatAuditValue(a.old_value)} → {formatAuditValue(a.new_value)}</>)}
                    </span>
                    <span className="audit-op">{a.operator}</span>
                    <span className="audit-time">{new Date(a.changed_at).toLocaleString('zh-CN')}</span>
                  </div>
                ))}
              </div>
            )}
          </Card>
        </div>
      </div>
    </div>
  )
}

function ConfigField({ item, value, revealed, onReveal, onChange, testProvider, testState, onTest, onTestFlare }: {
  item: ConfigItem; value: unknown; revealed: boolean; onReveal: () => void
  onChange: (v: unknown) => void; testProvider?: string; testState?: string; onTest: () => void
  onTestFlare?: () => void
}) {
  const hideValue = item.sensitive && !revealed && item.masked
  let control: React.ReactNode

  if (item.value_type === 'bool') {
    control = (
      <label className="switch">
        <input type="checkbox" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} />
        <span className="switch-track" />
      </label>
    )
  } else if (item.value_type === 'int') {
    control = <input className="config-input" type="number" value={Number(value ?? 0)} onChange={(e) => onChange(Number(e.target.value))} />
  } else if (item.value_type === 'json') {
    control = <JsonConfigField itemKey={item.key} value={value} onChange={onChange} />
  } else if (item.sensitive) {
    control = (
      <div className="row-gap">
        <input className="config-input" type={hideValue ? 'password' : 'text'}
          value={hideValue ? '****' : String(value ?? '')}
          placeholder="未设置"
          onChange={(e) => onChange(e.target.value)} />
        <button className="btn btn-sm btn-ghost" onClick={onReveal}>{revealed ? '隐藏' : '显示'}</button>
        {testProvider && <button className="btn btn-sm" onClick={onTest}>测试连接</button>}
        {testState && <span className={`test-result ${testState.startsWith('✓') ? 'ok' : 'error'}`}>{testState}</span>}
      </div>
    )
  } else if (onTestFlare) {
    // FlareSolverr 地址：检测按钮仅提示可用性，不拦截保存。
    control = (
      <div className="row-gap">
        <input className="config-input" value={String(value ?? '')} onChange={(e) => onChange(e.target.value)} placeholder="http://192.168.2.15:8191" />
        <button className="btn btn-sm" onClick={onTestFlare} disabled={testState === '检测中…'}>检测</button>
        {testState && <span className={`test-result ${testState.startsWith('✓') ? 'ok' : testState === '检测中…' ? '' : 'error'}`}>{testState}</span>}
      </div>
    )
  } else {
    control = <input className="config-input" value={String(value ?? '')} onChange={(e) => onChange(e.target.value)} />
  }

  return (
    <div className="config-field">
      <div className="config-label">
        <span className="config-key">{item.key}</span>
        <span className="config-desc">{item.description}</span>
      </div>
      <div className="config-control">{control}</div>
    </div>
  )
}

// 审计展示：配置 key 翻译成中文名，值做截断，避免整条数组刷屏。
const AUDIT_KEY_LABELS: Record<string, string> = {
  'library.root_paths': '媒体库根目录',
  'library.scan_cron': '默认扫描周期',
  'providers.enabled': '启用的字幕源',
  'providers.priority': '字幕源优先级',
  'providers.language_order': '语言路由顺序',
  'general.languages': '字幕语言',
  'general.log_level': '日志级别',
  'security.admin_username': '管理员用户名',
  'security.admin_password': '管理员密码',
  'security.session_hours': '会话时长',
}

function configKeyLabel(key: string): string {
  return AUDIT_KEY_LABELS[key] || key
}

function formatAuditValue(v: unknown): string {
  if (v === null || v === undefined || v === '') return '空'
  if (Array.isArray(v)) return v.length > 6 ? `[${v.length} 项]` : JSON.stringify(v)
  if (typeof v === 'object') {
    const s = JSON.stringify(v)
    return s.length > 60 ? s.slice(0, 57) + '…' : s
  }
  const s = String(v)
  return s.length > 60 ? s.slice(0, 57) + '…' : s
}

// JSON 配置字段：解析失败给出明确提示；对 Windows 路径反斜杠做容错。
function JsonConfigField({ itemKey, value, onChange }: {
  itemKey: string; value: unknown; onChange: (v: unknown) => void
}) {
  const serialized = useMemo(() => JSON.stringify(value ?? [], null, 2), [value])
  const [text, setText] = useState(serialized)
  const [error, setError] = useState('')
  useEffect(() => { setText(serialized); setError('') }, [serialized])

  function commit(raw: string, showError: boolean) {
    setText(raw)
    // 路径类配置常见 ["Y:\tv"] 写法：\t 是合法 tab 转义、\m 非法，直接 parse 会失败。
    // 先把「未成对的单个反斜杠」转义为 \\，再尝试解析（保留已是 \\ 的情况）。
    const candidates = [raw, raw.replace(/\\(?!\\)/g, '\\\\')]
    for (const candidate of candidates) {
      try {
        const parsed = JSON.parse(candidate)
        onChange(parsed)
        setError('')
        if (candidate !== raw) setText(JSON.stringify(parsed, null, 2))
        return
      } catch { /* 尝试下一个候选 */ }
    }
    if (showError) setError(`不是合法 JSON。路径请用正斜杠（如 "Y:/tv"）或双反斜杠（如 "Y:\\\\tv"）。`)
  }

  return (
    <div className="json-field">
      <textarea className={`config-input config-json ${error ? 'invalid' : ''}`}
        value={text} rows={Math.min(8, Math.max(2, text.split('\n').length))}
        spellCheck={false}
        onChange={(e) => commit(e.target.value, false)}
        onBlur={(e) => commit(e.target.value, true)} />
      {error && <div className="field-error">{error}</div>}
      {itemKey === 'library.root_paths' && !error && (
        <div className="field-hint">提示：Windows 路径可写 "Y:/tv" 或 "Y:\\tv"，保存时自动规范化。</div>
      )}
    </div>
  )
}

function ChangePassword({ push }: { push: ToastPush }) {
  const [oldPassword, setOld] = useState('')
  const [newPassword, setNew] = useState('')
  const [busy, setBusy] = useState(false)
  async function submit(e: React.FormEvent) {
    e.preventDefault(); setBusy(true)
    try {
      await api.post('/api/auth/change-password', { old_password: oldPassword, new_password: newPassword })
      push('密码已修改'); setOld(''); setNew('')
    } catch (err) { push((err as Error).message, 'error') }
    finally { setBusy(false) }
  }
  return (
    <form onSubmit={submit} className="form-grid compact">
      <label className="field"><span>当前密码</span><input type="password" value={oldPassword} onChange={(e) => setOld(e.target.value)} required /></label>
      <label className="field"><span>新密码（至少 8 位）</span><input type="password" value={newPassword} onChange={(e) => setNew(e.target.value)} minLength={8} required /></label>
      <div className="full"><button className="btn btn-primary" disabled={busy}>{busy ? '修改中…' : '修改密码'}</button></div>
    </form>
  )
}
