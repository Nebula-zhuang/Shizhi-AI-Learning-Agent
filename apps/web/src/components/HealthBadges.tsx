import { useCallback, useEffect, useState } from 'react'

import { fetchHealth, type HealthResult } from '../api/client'

/** 组件中文名映射 */
const LABEL: Record<string, string> = {
  llm: 'LLM 网关',
  mysql: 'MySQL',
  chroma: 'Chroma',
}

/**
 * 顶部依赖状态条。
 * 直接消费后端 /api/health，把「哪个组件没通」暴露在界面上 —— 
 * P0 阶段这比看日志更直观。
 */
export function HealthBadges() {
  const [health, setHealth] = useState<HealthResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setHealth(await fetchHealth())
    } catch (err) {
      setHealth(null)
      setError((err as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  if (loading && !health) {
    return <span className="text-xs text-ink-4">正在检查后端依赖…</span>
  }

  if (error) {
    return (
      <div className="flex items-center gap-2">
        <span className="inline-flex items-center gap-1.5 rounded-md bg-brick-soft px-2 py-1 text-xs text-brick-ink ring-1 ring-brick-line">
          <span className="h-1.5 w-1.5 rounded-full bg-brick" />
          后端未连接
        </span>
        <button
          type="button"
          onClick={() => void load()}
          className="text-xs text-ink-3 underline-offset-2 hover:underline"
        >
          重试
        </button>
      </div>
    )
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      {health?.components.map((c) => (
        <span
          key={c.name}
          title={JSON.stringify(c.detail, null, 2)}
          className={[
            'inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs ring-1',
            c.ok
              ? 'bg-moss-soft text-moss-ink ring-moss-line'
              : 'bg-sienna-soft text-sienna-ink ring-sienna-line',
          ].join(' ')}
        >
          <span
            className={[
              'h-1.5 w-1.5 rounded-full',
              c.ok ? 'bg-moss' : 'bg-sienna',
            ].join(' ')}
          />
          {LABEL[c.name] ?? c.name}
          {c.name === 'llm' && typeof c.detail.mode === 'string' && (
            <span className="text-ink-3">· {c.detail.mode}</span>
          )}
        </span>
      ))}
      <button
        type="button"
        onClick={() => void load()}
        className="text-xs text-ink-3 underline-offset-2 hover:underline"
      >
        刷新
      </button>
    </div>
  )
}
