import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
import type { Library } from '../types'
import { Badge, Card, Empty, ErrorBox, Spinner, useToasts } from '../components/ui'

type ToastPush = ReturnType<typeof useToasts>['push']

export default function Libraries({ push }: { push: ToastPush }) {
  const [libs, setLibs] = useState<Library[] | null>(null)
  const [error, setError] = useState('')
  const [showForm, setShowForm] = useState(false)
  const [scanning, setScanning] = useState<number | null>(null)
  const [form, setForm] = useState({ root_path: '', name: '', scan_cron: '0 3 * * *' })
  const navigate = useNavigate()

  const load = useCallback(async () => {
    try { setLibs((await api.get<{ items: Library[] }>('/api/libraries')).items); setError('') }
    catch (e) { setError((e as Error).message) }
  }, [])
  useEffect(() => { load() }, [load])

  async function create(e: FormEvent) {
    e.preventDefault()
    try {
      await api.post('/api/libraries', form)
      push('媒体库已添加'); setShowForm(false); setForm({ root_path: '', name: '', scan_cron: '0 3 * * *' })
      load()
    } catch (err) { push((err as Error).message, 'error') }
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
          <p className="page-sub">管理要扫描的媒体目录，触发扫描后发现新剧集</p>
        </div>
        <button className="btn btn-primary" onClick={() => setShowForm((v) => !v)}>
          {showForm ? '取消' : '+ 添加媒体库'}
        </button>
      </div>

      {showForm && (
        <Card title="添加媒体库">
          <form onSubmit={create} className="form-grid">
            <label className="field full">
              <span>根目录路径 <em>容器内路径，如 /media/tvshows</em></span>
              <input value={form.root_path} onChange={(e) => setForm({ ...form, root_path: e.target.value })}
                placeholder="/media/tvshows 或 Y:/tv/欧美剧" required />
            </label>
            <label className="field">
              <span>名称</span>
              <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="选填，如 欧美剧" />
            </label>
            <label className="field">
              <span>定时扫描（cron）</span>
              <input value={form.scan_cron} onChange={(e) => setForm({ ...form, scan_cron: e.target.value })} />
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
          {libs.map((lib) => (
            <Card key={lib.id} title={lib.name || '未命名媒体库'} subtitle={lib.root_path}
              actions={
                <div className="row-gap">
                  <Badge tone={lib.enabled ? 'ok' : 'muted'}>{lib.enabled ? '已启用' : '已禁用'}</Badge>
                  <button className="btn btn-sm" onClick={() => scan(lib.id)} disabled={scanning === lib.id}>
                    {scanning === lib.id ? '扫描中…' : '⟳ 立即扫描'}
                  </button>
                  <button className="btn btn-sm btn-danger-ghost" onClick={() => remove(lib.id)}>删除</button>
                </div>
              }>
              <div className="lib-meta">
                <span><strong>定时：</strong>{lib.scan_cron}</span>
                <span><strong>ID：</strong>{lib.id}</span>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}
