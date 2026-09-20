import { useEffect, useRef, useState } from 'react'

import { useLearning } from '../app/LearningProvider'
import { actionCue } from '../motion/variants'
import { HealthBadges } from './HealthBadges'

/**
 * 开发者面板。
 *
 * P0–P5 期间，后端依赖状态、接口清单、状态机轨迹、策略命中规则这些东西
 * 一直摆在主界面上 —— 对开发者有用，对一个学习伙伴是噪音。
 *
 * 做法不是删掉，而是**全部搬到这里**：主界面一个技术词都不出现，
 * 排障与演示需要的信息一件不少。按 `\` 或点右上角"开发者"打开。
 *
 * 这里显示的东西**允许**是技术语言：命中规则、模型提案、状态机、工具耗时 ——
 * 因为看它的人就是开发者。
 */
export function DevPanel({
  devMode,
  onDevMode,
}: {
  devMode: boolean
  onDevMode: (value: boolean) => void
}) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const { turn, timings, stage } = useLearning()

  // 点击面板外面就收起
  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: PointerEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) {
        setOpen(false)
      }
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const totalMs = timings.reduce((sum, item) => sum + item.ms, 0)

  return (
    <div ref={root} className="relative">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="meta inline-flex items-center gap-1.5 rounded-md px-2 py-1 transition-colors hover:bg-paper-sunken hover:text-ink-2"
      >
        开发者
        <svg
          width="10"
          height="10"
          viewBox="0 0 10 10"
          fill="none"
          aria-hidden="true"
          className={`transition-transform duration-200 ${open ? 'rotate-180' : ''}`}
        >
          <path
            d="M2 4l3 3 3-3"
            stroke="currentColor"
            strokeWidth="1.4"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>

      {open && (
        <div className="paper absolute right-0 z-30 mt-2 max-h-[70vh] w-[460px] overflow-y-auto p-4 shadow-float">
          <p className="meta mb-2 font-medium">后端依赖</p>
          <HealthBadges />

          <label className="mt-4 flex cursor-pointer items-center gap-2 border-t border-line pt-3 text-xs text-ink-2">
            <input
              type="checkbox"
              checked={devMode}
              onChange={(event) => onDevMode(event.target.checked)}
              className="h-3.5 w-3.5 accent-[#1b6b54]"
            />
            开发者模式（显示「接口调试」标签页）
          </label>

          {/* ---------------------------------------------------- 上一轮运行细节 */}
          <div className="mt-4 border-t border-line pt-3">
            <p className="meta mb-2 font-medium">上一轮运行细节</p>
            {!turn ? (
              <p className="text-2xs text-ink-4">还没有跑过一轮教学。</p>
            ) : (
              <div className="space-y-2 text-2xs">
                {stage && <p className="text-sienna">当前阶段：{stage}</p>}
                <dl className="space-y-1">
                  <Row label="action">
                    <code className="font-mono">
                      {turn.action}（{actionCue(turn.action).label}）
                    </code>
                  </Row>
                  <Row label="决策规则">
                    <code className="font-mono">{turn.decision.rule}</code>
                  </Row>
                  <Row label="是否强制">
                    {turn.decision.forced ? '是（阈值锁定）' : '否（自由区自选）'}
                  </Row>
                  <Row label="允许集合">
                    <code className="font-mono">{turn.decision.allowed.join(', ')}</code>
                  </Row>
                  {turn.proposal && (
                    <Row label="模型提案">
                      {turn.proposal.ok ? (
                        <span>
                          通过 <code className="font-mono">{turn.proposal.action}</code> conf=
                          {turn.proposal.confidence.toFixed(2)}
                        </span>
                      ) : (
                        <span className="text-brick">
                          被拦 reject={turn.proposal.reject_reason}
                        </span>
                      )}
                    </Row>
                  )}
                  <Row label="mastery">
                    <span className="font-mono">
                      {turn.state_before.mastery.toFixed(3)} → {turn.state_after.mastery.toFixed(3)}
                    </span>
                  </Row>
                  <Row label="trace">
                    <code className="break-all font-mono">{turn.trace.join(' → ')}</code>
                  </Row>
                  <Row label="sources">{turn.sources.length} 条</Row>
                  {turn.notes.length > 0 && (
                    <Row label="notes">
                      <span className="text-sienna">{turn.notes.join('；')}</span>
                    </Row>
                  )}
                </dl>

                {/* 阶段耗时：回答"这一轮慢在哪" */}
                {timings.length > 0 && (
                  <div className="rounded bg-paper-sunken px-2 py-1.5">
                    <p className="mb-1 text-ink-3">
                      阶段耗时 · 合计 {(totalMs / 1000).toFixed(1)}s
                    </p>
                    <ul className="space-y-0.5">
                      {timings.map((item) => (
                        <li key={item.name} className="flex justify-between font-mono">
                          <span className="text-ink-3">{item.name}</span>
                          <span className="text-ink-2">
                            {item.ms} ms
                            <span className="ml-1.5 text-ink-4">
                              {totalMs > 0 ? `${Math.round((item.ms / totalMs) * 100)}%` : ''}
                            </span>
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="mt-4 border-t border-line pt-3">
            <p className="meta mb-2 font-medium">接口</p>
            <div className="space-y-1 font-mono text-2xs text-ink-3">
              <div>POST /api/tutor/start/stream · 开课（SSE）</div>
              <div>POST /api/tutor/answer/stream · 作答（SSE）</div>
              <div>POST /api/tutor/start · 开课（同步）</div>
              <div>POST /api/tutor/answer · 作答（同步）</div>
              <div>GET&nbsp; /api/tutor/dashboard · 学习看板</div>
            </div>
            <a
              href="/docs"
              target="_blank"
              rel="noreferrer"
              className="meta mt-2 inline-block text-moss hover:underline"
            >
              打开接口文档 →
            </a>
          </div>
        </div>
      )}
    </div>
  )
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-3">
      <dt className="w-[4.5rem] shrink-0 text-ink-4">{label}</dt>
      <dd className="min-w-0 flex-1 text-ink-2">{children}</dd>
    </div>
  )
}
