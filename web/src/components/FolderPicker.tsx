// 文件夹选择：手动可编辑输入框 + 浏览弹窗（服务端列目录，浏览器安全限制拿不到绝对路径）。
import { useCallback, useState, type KeyboardEvent } from 'react'
import { api } from '../api'

interface BrowseResult {
  current: string
  parent: string | null
  dirs: { name: string; path: string }[]
  root: boolean
}

export default function FolderPicker({ value, onChange, placeholder }: {
  value: string
  onChange: (path: string) => void
  placeholder?: string
}) {
  const [open, setOpen] = useState(false)
  const [input, setInput] = useState(value)
  const [browsePath, setBrowsePath] = useState('')
  const [result, setResult] = useState<BrowseResult | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const navigate = useCallback(async (path: string) => {
    setLoading(true); setError('')
    try {
      const r = await api.get<BrowseResult>(`/api/libraries/browse?path=${encodeURIComponent(path)}`)
      setResult(r)
      setBrowsePath(r.current)
      setInput(r.current)
    } catch (e) { setError((e as Error).message) }
    finally { setLoading(false) }
  }, [])

  function openPicker() {
    setOpen(true)
    setError('')
    setBrowsePath(value || '')
    setInput(value || '')
    navigate(value || '')
  }

  function close() {
    setOpen(false)
    setResult(null)
    setError('')
  }

  function confirm() {
    const chosen = input.trim() || browsePath.trim()
    if (!chosen) { setError('请先选择或输入目录路径'); return }
    onChange(chosen)
    close()
  }

  function onInputKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter') { e.preventDefault(); navigate(input.trim()) }
  }

  return (
    <div className="folder-picker">
      <div className="folder-picker-row">
        <input className="config-input" value={value} placeholder={placeholder || '输入或选择目录路径'}
          onChange={(e) => onChange(e.target.value)} />
        <button type="button" className="btn btn-sm" onClick={openPicker}>📁 浏览…</button>
      </div>

      {open && (
        <div className="modal-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) close() }}>
          <div className="modal folder-modal" role="dialog" aria-modal="true" aria-label="选择文件夹">
            <div className="modal-head">
              <h2>选择文件夹</h2>
              <button type="button" className="btn btn-sm btn-ghost" onClick={close} aria-label="关闭">✕</button>
            </div>
            <div className="modal-body">
              <div className="folder-path-row">
                <input className="config-input" value={input}
                  placeholder="输入路径后按回车，如 Y:/tv 或 /media/tvshows"
                  onChange={(e) => setInput(e.target.value)} onKeyDown={onInputKey} />
                <button type="button" className="btn btn-sm" onClick={() => navigate(input.trim())} disabled={loading}>
                  {loading ? '加载中…' : '前往'}
                </button>
                <button type="button" className="btn btn-sm btn-ghost" onClick={() => navigate('')} title="返回根目录">⌂</button>
                <button type="button" className="btn btn-sm btn-ghost" onClick={() => result?.parent !== null && result && navigate(result.parent!)}
                  disabled={!result || result.parent === null} title="上一级">↑</button>
              </div>
              {error && <div className="folder-error">{error}</div>}
              <div className="folder-tree">
                {loading && <div className="folder-loading">加载中…</div>}
                {!loading && result && result.dirs.length === 0 && <div className="folder-loading">（此目录下没有子目录）</div>}
                {!loading && result?.dirs.map((d) => (
                  <button type="button" key={d.path} className="folder-item" onClick={() => navigate(d.path)}>
                    <span className="folder-icon">▤</span><span>{d.name}</span><span className="folder-arrow">›</span>
                  </button>
                ))}
              </div>
              <div className="folder-current">当前目录：<code>{result?.current || browsePath || '（未浏览）'}</code></div>
            </div>
            <div className="modal-foot">
              <button type="button" className="btn btn-sm btn-ghost" onClick={close}>取消</button>
              <button type="button" className="btn btn-sm btn-primary" onClick={confirm}>选择当前目录</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
