/**
 * 学习报告 —— 回头看。
 *
 * ## 这一页和 Today 的分工
 *
 * Today 是**往前看**（今天该干什么），这一页是**回头看**（我这两周怎么样）。
 * 两者时间方向不同，所以独立成页，不塞进 Today ✓
 *
 * ## 判断都在 `reportState.ts` 里
 *
 * 状态映射、文案、兜底全在纯逻辑模块 —— 前端单测只收 `.ts`，
 * 把判断留在组件里就等于那些判断没有测试 ✗
 *
 * ## 两条不能破的规矩
 *
 * 1. **不显示任何原始掌握度数字**（`mastery` / `urgency` / 百分比）。
 *    表达"学得怎么样"只有一个方式：状态词（还没碰过 / 在学 / 还不太稳 / 差不多了）。
 * 2. **不显示全库口径的知识点总数** —— 后端 `mastery_overview` 的
 *    `knowledge_point_total` 没按 learner 限定，对单个用户是错的。
 *    `overviewLines()` 里已经把它排除了 ✓
 *
 * ## 模型不可用也不会空白
 *
 * 那段人话由 `narrativeText()` 保证一定有：有模型写的用它，
 * 没有就用真实计数拼一段确定的话 ✓
 */

import { useCallback, useEffect, useState } from 'react'

import { DEFAULT_REPORT_DAYS, getLearningReport, type LearningReport } from '../../api/reports'
import { messageOf } from '../../api/http'
import { useLearning } from '../../app/LearningProvider'
import { Reveal } from '../../motion/primitives'
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  IconAlert,
  IconBookmark,
  IconInbox,
  IconRefresh,
  SectionTitle,
  Skeleton,
  cn,
  useToast,
} from '../../ui'
import {
  narrativeText,
  overviewLines,
  rankedErrorTypes,
  reportState,
  sessionsLabel,
  toRow,
  trajectoryTrend,
} from './reportState'

export function ReportView() {
  const toast = useToast()
  const { startLearning } = useLearning()

  const [report, setReport] = useState<LearningReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setReport(await getLearningReport(DEFAULT_REPORT_DAYS))
    } catch (err) {
      setError(messageOf(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const state = reportState({ loading, error, report })

  /** 从报告里直接开一段教学 —— 与 Today/辅导走同一个入口 ✓ */
  const goLearn = useCallback(
    async (kpId: number, title: string) => {
      try {
        await startLearning({ kpId, title, documentId: null })
      } catch (err) {
        toast.error('没能开始', messageOf(err))
      }
    },
    [startLearning, toast],
  )

  return (
    <div className="space-y-8">
      <header>
        <Reveal>
          <p className="meta tracking-[0.14em]">学习报告</p>
          <h1 className="display mt-2.5">你学得怎么样</h1>
          <p className="mt-3 text-sm text-ink-3">
            近 {DEFAULT_REPORT_DAYS} 天
            {report ? ` · ${sessionsLabel(report.sessions.count, report.sessions.turns) ?? '还没有学习记录'}` : ''}
          </p>
        </Reveal>
      </header>

      {state === 'loading' && (
        <div className="space-y-3">
          <Skeleton className="h-24 w-full rounded-lg" />
          <Skeleton className="h-32 w-full rounded-lg" />
          <Skeleton className="h-20 w-full rounded-lg" />
        </div>
      )}

      {state === 'error' && (
        <ErrorState
          title="没能读到你的学习报告"
          message={error ?? undefined}
          onRetry={() => void load()}
        />
      )}

      {state === 'empty' && (
        <EmptyState
          icon={<IconInbox size={22} />}
          title="还没有开始的记录"
          description="随便挑一个知识点开始，我会先讲清楚，再问你几个问题看看是不是真懂了。学完之后这里就会有记录了。"
          action={
            <Button variant="quiet" size="sm" onClick={() => void load()}>
              刷新看看
            </Button>
          }
        />
      )}

      {state === 'ready' && report && (
        <>
          {/* ── 学习回顾（模型写的那段；不可用时是确定性兜底） */}
          <Reveal>
            <Card className="p-4">
              <SectionTitle eyebrow="学习回顾" title="这两周，你怎么样" />
              <p className="mt-3 whitespace-pre-line text-sm leading-relaxed text-ink-2">
                {narrativeText(report)}
              </p>
              {!report.narrative.available && (
                <p className="mt-2 text-2xs text-ink-4">
                  这段是根据你的学习数据自动生成的。
                </p>
              )}
            </Card>
          </Reveal>

          {/* ── 总体学习状态 */}
          <Reveal>
            <Card className="p-4">
              <SectionTitle eyebrow="总体状态" title="你学到哪儿了" />
              {/*
                ⚠️ 这里**不用进度环**。环在视觉上就是一个百分比，
                而这一页的硬规矩是"不显示任何掌握度数字或百分比" ✗
                （`Ring` 的 value 是 0~1 比例，画出来等于把比例摊在脸上）。
                用状态计数说话就够了。
              */}
              <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4">
                {overviewLines(report.overview).map((line) => (
                  <div key={line.label}>
                    <dt className="text-2xs text-ink-4">{line.label}</dt>
                    <dd className="mt-0.5 text-sm text-ink-1">{line.value}</dd>
                  </div>
                ))}
              </dl>
            </Card>
          </Reveal>

          {/* ── 待复习 */}
          <Reveal>
            <Card className="p-4">
              <SectionTitle
                eyebrow="待复习"
                title={report.due_reviews.length ? `${report.due_reviews.length} 个该回头看了` : '没有欠账'}
              />
              {report.due_reviews.length ? (
                <ul className="mt-3 space-y-1.5">
                  {report.due_reviews.map((point) => {
                    const row = toRow(point)
                    return (
                      <li key={row.id}>
                        <button
                          type="button"
                          onClick={() => void goLearn(row.id, row.title)}
                          className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left transition-colors hover:bg-surface-2"
                        >
                          <span className="min-w-0 flex-1 truncate text-sm text-ink-1">
                            {row.title}
                          </span>
                          <Badge tone="brick">{row.status}</Badge>
                          <span className="text-2xs text-ink-4">{row.detail}</span>
                        </button>
                      </li>
                    )
                  })}
                </ul>
              ) : (
                <p className="mt-2 text-sm text-ink-3">暂时没有需要回头补的地方。</p>
              )}
            </Card>
          </Reveal>

          {/* ── 薄弱点 + 错因 */}
          <div className="grid gap-4 lg:grid-cols-2">
            <Reveal>
              <Card className="p-4">
                <SectionTitle eyebrow="薄弱点" title="卡过的地方" />
                {report.weak_points.length ? (
                  <ul className="mt-3 space-y-1.5">
                    {report.weak_points.map((point) => {
                      const row = toRow(point)
                      return (
                        <li
                          key={row.id}
                          className="flex items-center gap-2 rounded-lg px-2 py-1.5"
                        >
                          <span className="min-w-0 flex-1 truncate text-sm text-ink-1">
                            {row.title}
                          </span>
                          <Badge tone="neutral">{row.detail}</Badge>
                        </li>
                      )
                    })}
                  </ul>
                ) : (
                  <p className="mt-2 text-sm text-ink-3">还没发现明显卡住的地方。</p>
                )}
              </Card>
            </Reveal>

            <Reveal>
              <Card className="p-4">
                <SectionTitle eyebrow="错因" title="错在哪儿" />
                {rankedErrorTypes(report.error_types).length ? (
                  <>
                    <ul className="mt-3 space-y-2">
                      {rankedErrorTypes(report.error_types).map((item) => (
                        <li key={item.key} className="flex items-center gap-3">
                          <span className="w-24 shrink-0 text-sm text-ink-1">{item.label}</span>
                          <span className="flex-1">
                            <span
                              className="block h-1.5 rounded-full bg-moss/70"
                              style={{ width: `${Math.min(100, item.count * 20)}%` }}
                            />
                          </span>
                          <span className="text-2xs text-ink-4">{item.count} 次</span>
                        </li>
                      ))}
                    </ul>
                    {report.error_types_are_all_time && (
                      <p className="mt-3 text-2xs text-ink-4">
                        这几项统计的是**全部**答错记录，不受上面时间范围限制。
                      </p>
                    )}
                  </>
                ) : (
                  <p className="mt-2 text-sm text-ink-3">还没有答错的记录。</p>
                )}
              </Card>
            </Reveal>
          </div>

          {/* ── 学习轨迹 */}
          <Reveal>
            <Card className="p-4">
              <SectionTitle eyebrow="变化" title="这段时间的走向" />
              {report.trajectory.length ? (
                <ul className="mt-3 space-y-2">
                  {report.trajectory.map((line) => (
                    <li key={line.knowledge_point_id} className="flex items-center gap-3">
                      <span className="min-w-0 flex-1 truncate text-sm text-ink-1">
                        {line.title}
                      </span>
                      <span className="text-2xs text-ink-4">{line.points.length} 次记录</span>
                      <Badge tone="moss">{trajectoryTrend(line.points) ?? '记录中'}</Badge>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-2 text-sm text-ink-3">
                  这段时间还没有留下变化记录。多跟助教过几轮就会有了。
                </p>
              )}
            </Card>
          </Reveal>

          {/* ── 下一步建议 */}
          <Reveal>
            <Card className={cn('p-4', 'border-moss-line bg-moss-soft/40')}>
              <SectionTitle eyebrow="下一步" title="今天从哪儿开始" />
              <p className="mt-2 text-sm text-ink-2">
                {report.due_reviews.length
                  ? '先看上面对是那几个该复习的；也可以直接挑一个继续。'
                  : '没有欠账，挑一个你还没碰过的知识点开始就行。'}
              </p>
              <div className="mt-3 flex flex-wrap gap-2">
                <Button
                  variant="primary"
                  size="sm"
                  icon={<IconBookmark size={14} />}
                  onClick={() => {
                    const first = report.due_reviews[0] ?? report.weak_points[0]
                    if (!first) {
                      toast.info('还没有可以接着学的点', '去「资料」里看看，或者直接在自由学习里问点什么。')
                      return
                    }
                    void goLearn(first.knowledge_point_id, first.title)
                  }}
                >
                  接着学
                </Button>
                <Button variant="quiet" size="sm" icon={<IconRefresh size={14} />} onClick={() => void load()}>
                  刷新
                </Button>
              </div>
              <p className="mt-3 flex items-center gap-1.5 text-2xs text-ink-4">
                <IconAlert size={12} />
                报告只读，不会改动你的学习状态。
              </p>
            </Card>
          </Reveal>
        </>
      )}
    </div>
  )
}
