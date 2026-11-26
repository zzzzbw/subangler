import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
import type { Library } from '../types'
import { Badge, Card, Empty, ErrorBox, Spinner, useToasts } from '../components/ui'
import FolderPicker from '../components/FolderPicker'

type ToastPush = ReturnType<typeof useToasts>['push']
type FormState = {
  root_path: string
  name: string
  scan_cron: string
  scan_depth: number
  exclude_text: string
}

const emptyForm = (): FormState => ({
  root_path: '', name: '', scan_cron: '0 3 * * *', scan_depth: 3, exclude_text: '',
})

function parsePatterns(text: string): string[] {
  return text.split(/[\n,]/).map((s) => s.trim()).filter(Boolean)
}

export default function Libraries({ push }: { push: ToastPush }) {
  const [libs, setLibs] = useState<Library[] | null>(null)
  const [error, setError] = useState('')
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState<FormState>(emptyForm)
  const [scanning, setScanning] = useState<number | null>(null)
  const [editing, setEditing] = useState<number | null>(null)
  const [editForm, setEditForm] = useState<FormState>(emptyForm)
  const [saving, setSaving] = useState(false)
  const navigate = useNavigate()

  const load = useCallback(async () => {
    try { setLibs((await api.get<{ items: Library[] }>('/api/libraries')).items); setError('') }
    catch (e) { setError((e as Error).message) }
  }, [])
  useEffect(() => { load() }, [load])

  async function create(e: FormEvent) {
    e.preventDefault()
    try {
      await api.post('/api/libraries', {
        root_path: form.root_path, name: form.name, scan_cron: form.scan_cron,
        scan_depth: form.scan_depth, exclude_patterns: parsePatterns(form.exclude_text),
      })
      push('媒体库已添加'); setShowForm(false); setForm(emptyForm())
      load()
    } catch (err) { push((err as Error).message, 'error') }
  }

  function openEdit(lib: Library) {
    if (editing === lib.id) { setEditing(null); return }
    setEditing(lib.id)
    setEditForm({
      root_path: lib.root_path, name: lib.name, scan_cron: lib.scan_cron,
      scan_depth: lib.scan_depth, exclude_text: (lib.exclude_patterns || []).join('\n'),
    })
  }

  async function saveEdit(lib: Library) {
    setSaving(true)
    try {
      const r = await api.patch<Library>(`/api/libraries/${lib.id}`, {
        root_path: editForm.root_path, name: editForm.name, scan_cron: editForm.scan_cron,
        scan_depth: editForm.scan_depth, exclude_patterns: parsePatterns(editForm.exclude_text),
      })
      push(`${r.name || '媒体库'} 配置已保存`)
      setEditing(null)
      load()
    } catch (e) { push((e as Error).message, 'error') }
    finally { setSaving(false) }
  }

  async function toggleEnabled(lib: Library) {
    try {
      await api.patch(`/api/libraries/${lib.id}`, { enabled: !lib.enabled })
      push(`${lib.name || '媒体库'} 已${lib.enabled ? '禁用' : '启用'}`); load()
    } catch (e) { push((e as Error).message, 'error') }
  }

  async function scan(id: number) {
    setScanning(id)
    try {
      const r = await api.post<{ job_id: number }>(`/api/libraries/${id}/scan`)
      push(`扫描任务 #${r.job_id} 已创建`)
      navigate(`/jobs?job=${r.job_id}`)
    } catch (e) { push((e as Error).message, 'error') }
    finally { setScanning(null) }
  }

  async function remove(id: number) {
    if (!confirm('删除媒体库会同时移除其下所有视频记录（不会删除媒体文件），确定吗？')) return
    try { await api.delete(`/api/libraries/${id}`); push('已删除'); load() }
    catch (e) { push((e as Error).message, 'error') }
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>媒体库</h1>
          <p className="page-sub">管理要扫描的媒体目录；每库独立设置扫盘时间、扫描深度与排除规则</p>
        </div>
        <button className="btn btn-primary" onClick={() => setShowForm((v) => !v)}>
          {showForm ? '取消' : '+ 添加媒体库'}
        </button>
      </div>

      {showForm && (
        <Card title="添加媒体库">
          <form onSubmit={create} className="form-grid">
            <label className="field full">
              <span>根目录路径 <em>可手动输入，也可点「浏览」选择服务端目录</em></span>
              <FolderPicker value={form.root_path} onChange={(v) => setForm({ ...form, root_path: v })}
                placeholder="/media/tvshows 或 Y:/tv/欧美剧" />
            </label>
            <label className="field">
              <span>名称</span>
              <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="选填，如 欧美剧" />
            </label>
            <label className="field">
              <span>扫盘时间（cron）</span>
              <input value={form.scan_cron} onChange={(e) => setForm({ ...form, scan_cron: e.target.value })}
                placeholder="0 3 * * *" />
            </label>
            <label className="field">
              <span>扫描深度 <em>1 = 仅根目录</em></span>
              <input type="number" min={1} max={20} value={form.scan_depth}
                onChange={(e) => setForm({ ...form, scan_depth: Number(e.target.value) })} />
            </label>
            <label className="field full">
              <span>排除规则 <em>glob 风格，每行一条或逗号分隔，如 *sample*、*/@eaDir/*</em></span>
              <textarea rows={3} value={form.exclude_text}
                onChange={(e) => setForm({ ...form, exclude_text: e.target.value })}
                placeholder={"*sample*\n*/@eaDir/*\n*.nfo"} />
            </label>
            <div className="full form-actions">
              <button type="submit" className="btn btn-primary">添加</button>
            </div>
          </form>
        </Card>
      )}

      {!libs ? (error ? <ErrorBox message={error} onRetry={load} /> : <Spinner />) : libs.length === 0 ? (
        <Card><Empty text="还没有媒体库" hint="添加一个目录开始自动下载字幕" /></Card>
      ) : (
        <div className="lib-list">
          {libs.map((lib) => {
            const isEditing = editing === lib.id
            return (
              <Card key={lib.id} title={lib.name || '未命名媒体库'} subtitle={lib.root_path}
                actions={
                  <div className="row-gap">
                    <Badge tone={lib.enabled ? 'ok' : 'muted'}>{lib.enabled ? '已启用' : '已禁用'}</Badge>
                    <button className={`btn btn-sm ${isEditing ? 'btn-primary' : ''}`} onClick={() => openEdit(lib)}>
                      {isEditing ? '收起配置' : '配置'}
                    </button>
                    <button className="btn btn-sm btn-ghost" onClick={() => toggleEnabled(lib)}>
                      {lib.enabled ? '禁用' : '启用'}
                    </button>
                    <button className="btn btn-sm" onClick={() => scan(lib.id)} disabled={scanning === lib.id}>
                      {scanning === lib.id ? '扫描中…' : '⟳ 立即扫描'}
                    </button>
                    <button className="btn btn-sm btn-danger-ghost" onClick={() => remove(lib.id)}>删除</button>
                  </div>
                }>
                <div className="lib-meta">
                  <span><strong>定时：</strong>{lib.scan_cron}</span>
                  <span><strong>深度：</strong>{lib.scan_depth ?? '不限'}</span>
                  <span><strong>排除：</strong>{(lib.exclude_patterns || []).length ? lib.exclude_patterns.join(', ') : '无'}</span>
                  <span><strong>ID：</strong>{lib.id}</span>
                </div>
                {isEditing && (
                  <div className="lib-edit">
                    <div className="credential-hint">配置仅对当前媒体库生效，保存后下次扫描按新规则执行。</div>
                    <div className="form-grid">
                      <label className="field full">
                        <span>根目录路径 <em>可手动输入，也可点「浏览」选择服务端目录</em></span>
                        <FolderPicker value={editForm.root_path}
                          onChange={(v) => setEditForm({ ...editForm, root_path: v })}
                          placeholder="/media/tvshows 或 Y:/tv/欧美剧" />
                      </label>
                      <label className="field">
                        <span>名称</span>
                        <input value={editForm.name} onChange={(e) => setEditForm({ ...editForm, name: e.target.value })} />
                      </label>
                      <label className="field">
                        <span>扫盘时间（cron）</span>
                        <input value={editForm.scan_cron} onChange={(e) => setEditForm({ ...editForm, scan_cron: e.target.value })}
                          placeholder="0 3 * * *" />
                      </label>
                      <label className="field">
                        <span>扫描深度 <em>1 = 仅根目录</em></span>
                        <input type="number" min={1} max={20} value={editForm.scan_depth}
                          onChange={(e) => setEditForm({ ...editForm, scan_depth: Number(e.target.value) })} />
                      </label>
                      <label className="field full">
                        <span>排除规则 <em>glob 风格，每行一条或逗号分隔</em></span>
                        <textarea rows={3} value={editForm.exclude_text}
                          onChange={(e) => setEditForm({ ...editForm, exclude_text: e.target.value })}
                          placeholder={"*sample*\n*/@eaDir/*\n*.nfo"} />
                      </label>
                    </div>
                    <div className="credential-actions">
                      <button className="btn btn-sm btn-primary" onClick={() => saveEdit(lib)} disabled={saving}>
                        {saving ? '保存中…' : '保存配置'}
                      </button>
                      <button className="btn btn-sm btn-ghost" onClick={() => setEditing(null)}>取消</button>
                    </div>
                  </div>
                )}
              </Card>
            )
          })}
        </div>
      )}

      <p className="hint-block">
        说明：扫盘时间使用 5 段 cron（分 时 日 月 周），如 <code>0 3 * * *</code> 表示每天 03:00。
        扫描深度从媒体库根目录算起，1 表示只扫描根目录。排除规则为 glob 风格，支持 <code>*</code> / <code>?</code>，
        命中相对路径或文件名即跳过；隐藏目录与 sample 目录始终跳过。
      </p>
    </div>
  )
}
