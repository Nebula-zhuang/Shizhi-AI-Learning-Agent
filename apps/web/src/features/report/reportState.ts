/**
 * 学习报告的纯逻辑 —— 状态映射、文案、兜底。**不碰 React、不碰网络。**
 *
 * ## 为什么单独一个文件
 *
 * 前端单测只收 `.ts`（`node --experimental-strip-types --test "src/**\/*.test.ts"`），
 * `.tsx` 组件测不到。而这一页最要紧的两条规矩恰恰都是**纯逻辑**：
 *
 * 1. **原始 mastery 数字绝不能出现在界面上** —— 只用状态映射的人话
 * 2. **LLM 不可用时也要有一段确定的话** —— 不编造数据，只用真实计数
 *
 * 放在组件里，这两条就等于没有测试。
 *
 * ## 关于 `knowledge_point_total` / `untouched`
 *
 * 后端 `mastery_overview` 的这两个字段是**全库**口径（那条查询没按 learner 限定），
 * 对单个用户来说是错的。所以这里**根本不读它们** ——
 * 类型里留着只是为了如实接收响应，界面上一律不用 ✗
 */

import type { LearningReport, MasteryOverview, ReportPoint } from '../../api/reports'

/** 知识点状态 → 人话。**这是本页唯一的"掌握度"表达方式。** */
const STATUS_LABEL: Record<string, string> = {
  new: '还没碰过',
  learning: '在学',
  weak: '还不太稳',
  mastered: '差不多了',
}

/** 认不出来的状态给个中性的说法，不猜。 */
export function statusLabel(status: string): string {
  return STATUS_LABEL[status] ?? '在学'
}

/**
 * 错因 → 人话。**键必须与后端 `ErrorType` 一致**
 * （`app/models/answer_evaluation.py`：concept_confusion / memory_gap /
 * reasoning_break / misread / none）。
 */
const ERROR_LABEL: Record<string, string> = {
  concept_confusion: '概念混了',
  memory_gap: '没记住',
  reasoning_break: '推理断了',
  misread: '看错了题',
  none: '说不上来哪里错',
}

export function errorTypeLabel(key: string): string {
  return ERROR_LABEL[key] ?? key
}

/** 按次数从多到少，方便直接渲染。 */
export function rankedErrorTypes(errorTypes: Record<string, number>): Array<{
  key: string
  label: string
  count: number
}> {
  return Object.entries(errorTypes)
    .map(([key, count]) => ({ key, label: errorTypeLabel(key), count }))
    .sort((a, b) => b.count - a.count || a.key.localeCompare(b.key))
}

/** 知识点在报告里的一行显示。 */
export interface PointRow {
  id: number
  title: string
  /** 人话，例如「还不太稳」 */
  status: string
  /** 人话，例如「作答 4 次 · 连错 2 次」 */
  detail: string
  due: boolean
  /** 到了该复习的时候 */
  when: string
}

export function toRow(point: ReportPoint): PointRow {
  const bits: string[] = []
  if (point.attempt_count > 0) bits.push(`作答 ${point.attempt_count} 次`)
  if (point.consecutive_wrong > 0) bits.push(`连错 ${point.consecutive_wrong} 次`)
  return {
    id: point.knowledge_point_id,
    title: point.title,
    status: statusLabel(point.status),
    detail: bits.join(' · ') || '刚开始',
    due: point.due,
    when: point.due ? '该复习了' : point.next_review_at ? '还没到点' : '',
  }
}

/** 总体状态里**可以显示**的那几条（不含全库口径的那两个 ✗）。 */
export interface OverviewLine {
  label: string
  value: string
}

export function overviewLines(overview: MasteryOverview): OverviewLine[] {
  const lines: OverviewLine[] = [
    { label: '学过的知识点', value: `${overview.tracked} 个` },
    { label: '差不多了', value: `${overview.mastered} 个` },
    { label: '还不太稳', value: `${overview.weak} 个` },
    { label: '在学中', value: `${overview.learning} 个` },
  ]
  // 「还没碰过」要用全库口径才说得出来，而那个数字是错的 → 不显示 ✗
  return lines
}

/** 这份报告里有没有"真的学过东西"。决定空状态。 */
export function hasAnyLearning(report: LearningReport): boolean {
  return (
    report.overview.tracked > 0 ||
    report.sessions.count > 0 ||
    report.weak_points.length > 0 ||
    report.due_reviews.length > 0
  )
}

/**
 * LLM 不可用时的**确定性文案**。
 *
 * ⚠️ **只用真实计数，不编造任何经历**：
 * 不说学了多久、不说进步多少、不提没出现过的知识点。
 * 数据为空时如实说空 —— 不硬凑一句鼓励的话。
 */
export function fallbackNarrative(report: LearningReport): string {
  const { tracked, weak } = report.overview
  const due = report.due_reviews.length

  if (!hasAnyLearning(report)) {
    return '还没有开始的记录。随便挑一个知识点开始，我会先讲清楚，再问你几个问题看看是不是真懂了。'
  }

  const parts: string[] = []
  parts.push(`你已经学过 ${tracked} 个知识点`)
  if (weak > 0) parts.push(`其中 ${weak} 个还不太稳`)

  const first = parts.join('，') + '。'
  if (due > 0) {
    return `${first}有 ${due} 个到了该复习的时候，今天可以先从这些开始。`
  }
  if (weak > 0) {
    return `${first}今天可以先从最需要复习的内容开始。`
  }
  return `${first}目前没有欠账，可以往前学新的。`
}

/** 页面要显示的那段话：有模型写的就用它，没有就用确定性兜底。 */
export function narrativeText(report: LearningReport): string {
  const text = report.narrative?.text?.trim()
  return report.narrative?.available && text ? text : fallbackNarrative(report)
}

/** 页面的整体状态。**顺序有意为之**：先加载、再出错，别把加载中当空。 */
export type ReportState = 'loading' | 'error' | 'empty' | 'ready'

export function reportState(input: {
  loading: boolean
  error: string | null
  report: LearningReport | null
}): ReportState {
  if (input.loading) return 'loading'
  if (input.error) return 'error'
  if (!input.report || !hasAnyLearning(input.report)) return 'empty'
  return 'ready'
}

/** 学习次数怎么说。 */
export function sessionsLabel(count: number, turns: number): string | null {
  if (count <= 0) return null
  return `${count} 次学习 · ${turns} 轮问答`
}

/**
 * 轨迹的"从…到…"描述。**只说方向，不给数字** ✓
 *
 * 用状态词描述首尾，避免把 `mastery` 暴露到界面上。
 */
export function trajectoryTrend(points: Array<{ mastery: number }>): string | null {
  if (points.length < 2) return null
  const first = points[0].mastery
  const last = points[points.length - 1].mastery
  const band = (value: number) => (value >= 0.8 ? 3 : value >= 0.5 ? 2 : value > 0 ? 1 : 0)
  const delta = band(last) - band(first)
  if (delta > 0) return '在往上走'
  if (delta < 0) return '有点回落'
  return '基本持平'
}
