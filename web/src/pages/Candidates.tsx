import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api'
import type { Candidate, Video } from '../types'
import { Badge, Card, Empty, ErrorBox, Spinner, langLabel, useToasts } from '../components/ui'

type ToastPush = ReturnType<typeof useToasts>['push']

const PAGE_SIZES = [10, 20, 50, 100]

export default function Candidates({ push }: { push: ToastPush }) {
  const [params, setParams] = useSearchParams()
  const videoFilter = params.get('video') || ''
  const langFilter = params.get('lang') || ''
  const qParam = params.get('q') || ''
  const page = Math.max(1, Number(params.get('page')) || 1)
  const pageSize = PAGE_SIZES.includes(Number(params.get('size'))) ? Number(params.get('size')) : 20

  const [candidates, setCandidates] = useState<Candidate[] | null>(null)
  const [videos, setVideos] = useState<Video[]>([])
  const [total, setTotal] = useState(0)
  const [groups, setGroups] = useState(0)
  const [pages, setPages] = useState(0)
  const [error, setError] = useState('')
  const [busyId, setBusyId] = useState<number | null>(null)
  const uploadRef = useRef<HTMLInputElement>(null)
  const [uploadVideo, setUploadVideo] = useState<number | null>(null)
  const [uploadLang, setUploadLang] = useState('zh')

  // 搜索框本地值 + 防抖写回 URL。
  const [searchInput, setSearchInput] = useState(qParam)
  const paramsRef = useRef(params)
  paramsRef.current = params
  const qParamRef = useRef(qParam)
  qParamRef.current = qParam
  useEffect(() => { setSearchInput(qParam) }, [qParam])
  useEffect(() => {
    if (searchInput === qParamRef.current) return
    const timer = setTimeout(() => {
      const p = new URLSearchParams(paramsRef.current)
      const value = searchInput.trim()
      if (value) p.set('q', value); else p.delete('q')
      p.delete('page')
      setParams(p)
    }, 300)
    return () => clearTimeout(timer)
  }, [searchInput, setParams])
  // 分组数变化后页码越界则回到最后一页。
  useEffect(() => {
    if (pages > 0 && page > pages) {
      const p = new URLSearchParams(params)
      p.set('page', String(pages))
      setParams(p)
    }
  }, [pages, page, params, setParams])

  const load = useCallback(async () => {
    try {
      const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
      if (videoFilter) query.set('video_id', videoFilter)
      if (langFilter) query.set('language', langFilter)
      if (qParam) query.set('q', qParam)
      const [cRes, vRes] = await Promise.all([
        api.get<{ items: Candidate[]; total: number; groups: number; pages: number }>(`/api/candidates?${query}`),
        api.get<{ items: Video[] }>('/api/videos?limit=1000'),
      ])
      setCandidates(cRes.items)
      setTotal(cRes.total); setGroups(cRes.groups); setPages(cRes.pages)
      setVideos(vRes.items); setError('')
    } catch (e) { setError((e as Error).message) }
  }, [videoFilter, langFilter, qParam, page, pageSize])
  useEffect(() => { load() }, [load])

  function updateParam(key: string, value: string) {
    const p = new URLSearchParams(params)
    if (value) p.set(key, value); else p.delete(key)
    p.delete('page')
    setParams(p)
  }

  function goPage(next: number) {
    const p = new URLSearchParams(params)
    p.set('page', String(Math.max(1, Math.min(next, pages || 1))))
    setParams(p)
  }

  async function install(c: Candidate) {
    setBusyId(c.id)
    try {
      await api.post(`/api/candidates/${c.id}/install`, { video_id: c.video_id, language: c.language })
      push(`已安装 ${c.provider} 字幕`); load()
    } catch (e) { push((e as Error).message, 'error') }
    finally { setBusyId(null) }
  }

  async function ignore(c: Candidate) {
    try { await api.post(`/api/candidates/${c.id}/ignore`); push('候选已忽略'); load() }
    catch (e) { push((e as Error).message, 'error') }
  }

  async function onUploadFile(file: File) {
    if (!uploadVideo) { push('请先选择剧集', 'error'); return }
    const form = new FormData()
    form.append('video_id', String(uploadVideo))
    form.append('language', uploadLang)
    form.append('file', file)
    try {
      const r = await api.upload<{ path: string }>('/api/candidates/upload-install', form)
      push(`本地字幕已安装：${r.path.split(/[\\/]/).pop()}`); load()
    } catch (e) { push((e as Error).message, 'error') }
  }

  // 按视频+语言分组，并排展示候选。
  const groupList: { key: string; videoId: number; lang: string; items: Candidate[] }[] = []
  for (const c of candidates || []) {
    const key = `${c.video_id}:${c.language}`
    let g = groupList.find((x) => x.key === key)
    if (!g) { g = { key, videoId: c.video_id, lang: c.language, items: [] }; groupList.push(g) }
    g.items.push(c)
  }
  for (const g of groupList) g.items.sort((a, b) => b.score - a.score)

  const videoMap = new Map(videos.map((v) => [v.id, v]))

  if (candidates === null) return error ? <ErrorBox message={error} onRetry={load} /> : <Spinner />

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>候选挑选</h1>
          <p className="page-sub">{groups} 组待选 · {total} 个候选，对比后人工救回</p>
        </div>
        <div className="head-actions">
          <select className="select" value={uploadVideo || ''} onChange={(e) => setUploadVideo(Number(e.target.value) || null)}>
            <option value="">选择剧集以上传</option>
            {videos.map((v) => <option key={v.id} value={v.id}>{v.title || v.path.split(/[\\/]/).pop()} {v.episode ? `E${v.episode}` : ''}</option>)}
          </select>
          <select className="select" value={uploadLang} onChange={(e) => setUploadLang(e.target.value)}>
            <option value="zh">简体中文</option><option value="en">英文</option>
          </select>
          <input ref={uploadRef} type="file" accept=".srt,.ass,.ssa,.vtt,.sub" hidden
            onChange={(e) => { const f = e.target.files?.[0]; if (f) onUploadFile(f); e.target.value = '' }} />
          <button className="btn" onClick={() => {
            if (!uploadVideo) { push('请先选择剧集', 'error'); return }
            uploadRef.current?.click()
          }}>↑ 上传本地字幕</button>
        </div>
      </div>

      <Card>
        <div className="filter-bar">
          <select className="select" value={videoFilter} onChange={(e) => updateParam('video', e.target.value)}>
            <option value="">全部剧集</option>
            {videos.map((v) => <option key={v.id} value={v.id}>{v.title || v.path.split(/[\\/]/).pop()} {v.episode ? `E${v.episode}` : ''}</option>)}
          </select>
          <select className="select" value={langFilter} onChange={(e) => updateParam('lang', e.target.value)}>
            <option value="">全部语言</option><option value="zh">简体中文</option><option value="en">英文</option>
          </select>
          <input className="search-input" type="search" placeholder="搜索剧名、发布名或来源…" value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)} />
        </div>

        {error && <ErrorBox message={error} onRetry={load} />}

        {groupList.length === 0 ? <Empty text="没有符合条件的候选" hint="试试调整搜索词或筛选条件" /> : (
          <div className="candidate-groups">
            {groupList.map((g) => {
              const v = videoMap.get(g.videoId)
              return (
                <div key={g.key} className="candidate-group">
                  <div className="candidate-group-head">
                    <strong>{v?.title || (v ? v.path.split(/[\\/]/).pop() : `视频 #${g.videoId}`)} {v?.episode ? `· E${v.episode}` : ''}</strong>
                    <Badge tone="accent">{langLabel(g.lang)}</Badge>
                    <span className="muted">{g.items.length} 个候选</span>
                  </div>
                  <div className="candidate-list">
                    {g.items.map((c) => (
                      <div key={c.id} className={`candidate-card ${busyId === c.id ? 'busy' : ''}`}>
                        <div className="candidate-top">
                          <span className="candidate-score">{c.score}</span>
                          <div className="candidate-ident">
                            <div className="candidate-release" title={c.release_name}>{c.release_name || c.external_id}</div>
                            <div className="candidate-meta">
                              <Badge tone="muted">{c.provider}</Badge>
                              {c.fps && <span>{c.fps} fps</span>}
                              {c.hearing_impaired && <Badge tone="warn">听障</Badge>}
                              {c.lang_desc && <span>{c.lang_desc}</span>}
                            </div>
                          </div>
                          <div className="candidate-actions">
                            <button className="btn btn-sm btn-primary" onClick={() => install(c)} disabled={busyId === c.id}>
                              {busyId === c.id ? '安装中…' : '安装'}
                            </button>
                            <button className="btn btn-sm btn-ghost" onClick={() => ignore(c)}>忽略</button>
                          </div>
                        </div>
                        {Object.keys(c.score_breakdown || {}).length > 0 && (
                          <div className="score-breakdown">
                            {Object.entries(c.score_breakdown).map(([k, val]) => (
                              <span key={k} className="score-chip" title={`${k}: ${val}`}>
                                {scoreLabel(k)} <b>{val > 0 ? '+' : ''}{val}</b>
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )
            })}
          </div>
        )}

        <div className="pager">
          <span className="pager-info">第 {page} / {Math.max(pages, 1)} 页 · 共 {groups} 组 / {total} 个候选</span>
          <div className="pager-controls">
            <button className="btn btn-sm" disabled={page <= 1} onClick={() => goPage(page - 1)}>上一页</button>
            <button className="btn btn-sm" disabled={page >= Math.max(pages, 1)} onClick={() => goPage(page + 1)}>下一页</button>
            <select className="select" value={pageSize} onChange={(e) => updateParam('size', e.target.value)}>
              {PAGE_SIZES.map((n) => <option key={n} value={n}>{n} 组/页</option>)}
            </select>
          </div>
        </div>
      </Card>
    </div>
  )
}

function scoreLabel(k: string): string {
  const map: Record<string, string> = {
    season: '季', episode: '集', title: '标题', year: '年份', fps: '帧率',
    release: '发行名', hearing_impaired: '听障', language: '语言', source: '来源',
  }
  return map[k] || k
}
