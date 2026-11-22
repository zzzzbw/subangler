import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type { Candidate, Video } from '../types'
import { Badge, Card, Empty, ErrorBox, Spinner, langLabel, useToasts } from '../components/ui'

type ToastPush = ReturnType<typeof useToasts>['push']

export default function Candidates({ push }: { push: ToastPush }) {
  const [candidates, setCandidates] = useState<Candidate[] | null>(null)
  const [videos, setVideos] = useState<Video[]>([])
  const [error, setError] = useState('')
  const [videoFilter, setVideoFilter] = useState('')
  const [langFilter, setLangFilter] = useState('')
  const [busyId, setBusyId] = useState<number | null>(null)
  const uploadRef = useRef<HTMLInputElement>(null)
  const [uploadVideo, setUploadVideo] = useState<number | null>(null)
  const [uploadLang, setUploadLang] = useState('zh')

  const load = useCallback(async () => {
    try {
      const q = new URLSearchParams({ status: 'pending', limit: '300' })
      if (videoFilter) q.set('video_id', videoFilter)
      if (langFilter) q.set('language', langFilter)
      const [cRes, vRes] = await Promise.all([
        api.get<{ items: Candidate[] }>(`/api/candidates?${q}`),
        api.get<{ items: Video[] }>('/api/videos?limit=1000'),
      ])
      setCandidates(cRes.items); setVideos(vRes.items); setError('')
    } catch (e) { setError((e as Error).message) }
  }, [videoFilter, langFilter])
  useEffect(() => { load() }, [load])

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
  const groups: { key: string; videoId: number; lang: string; items: Candidate[] }[] = []
  for (const c of candidates || []) {
    const key = `${c.video_id}:${c.language}`
    let g = groups.find((x) => x.key === key)
    if (!g) { g = { key, videoId: c.video_id, lang: c.language, items: [] }; groups.push(g) }
    g.items.push(c)
  }
  for (const g of groups) g.items.sort((a, b) => b.score - a.score)

  const videoMap = new Map(videos.map((v) => [v.id, v]))

  if (candidates === null) return error ? <ErrorBox message={error} onRetry={load} /> : <Spinner />

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>候选挑选</h1>
          <p className="page-sub">自动评分未达标时，在这里对比候选并人工救回</p>
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
          <select className="select" value={videoFilter} onChange={(e) => setVideoFilter(e.target.value)}>
            <option value="">全部剧集</option>
            {videos.map((v) => <option key={v.id} value={v.id}>{v.title || v.path.split(/[\\/]/).pop()} {v.episode ? `E${v.episode}` : ''}</option>)}
          </select>
          <select className="select" value={langFilter} onChange={(e) => setLangFilter(e.target.value)}>
            <option value="">全部语言</option><option value="zh">简体中文</option><option value="en">英文</option>
          </select>
        </div>

        {groups.length === 0 ? <Empty text="没有待挑选的候选" hint="下载任务中评分不达标的候选会自动出现在这里" /> : (
          <div className="candidate-groups">
            {groups.map((g) => {
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
