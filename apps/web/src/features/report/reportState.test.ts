/**
 * 学习报告纯逻辑测试（Phase 5E）。
 *
 * ## 这组测试守的两条硬规矩
 *
 * 1. **原始 mastery 数字不许出现在界面上** —— 这一页能渲染出来的每一段文字，
 *    都不该出现 `0.31` 或 `31%` 这类东西。项目规矩：用户可见文字里不出现
 *    置信度 / 相似度 / 数据库字段（见 `features/learn/voice.ts`）。
 * 2. **LLM 不可用也要有话说** —— 只用真实计数拼一段确定的话，**不编造经历**。
 *
 * 运行：`npm run test:unit`
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  errorTypeLabel,
  fallbackNarrative,
  hasAnyLearning,
  narrativeText,
  overviewLines,
  rankedErrorTypes,
  reportState,
  sessionsLabel,
  statusLabel,
  toRow,
  trajectoryTrend,
} from './reportState.ts'
import type { LearningReport, ReportPoint } from '../../api/reports'

// --------------------------------------------------------------------------- //
// 造数据
// --------------------------------------------------------------------------- //
function report(over: Partial<LearningReport> = {}): LearningReport {
  return {
    generated_at: '2026-09-23T20:00:00+00:00',
    window_days: 30,
    window_since: '2026-08-24T20:00:00+00:00',
    error_types_are_all_time: true,
    overview: {
      // ⚠️ 这两个是全库口径，故意给一个夸张值 —— 界面/文案都不该用它
      knowledge_point_total: 378,
      untouched: 375,
      tracked: 3,
      new: 0,
      learning: 1,
      weak: 1,
      mastered: 1,
    },
    weak_points: [],
    due_reviews: [],
    error_types: {},
    sessions: { count: 2, turns: 9 },
    trajectory: [],
    narrative: { available: false, text: '' },
    ...over,
  }
}

function point(over: Partial<ReportPoint> = {}): ReportPoint {
  return {
    knowledge_point_id: 1,
    title: 'Java 线程',
    mastery: 0.31,
    status: 'weak',
    attempt_count: 4,
    consecutive_wrong: 2,
    importance: 3,
    last_error_type: 'concept_confusion',
    next_review_at: null,
    urgency: 0.7,
    due: false,
    ...over,
  }
}

// --------------------------------------------------------------------------- //
// 一、状态 → 人话（这一页唯一的"掌握度"表达）
// --------------------------------------------------------------------------- //
test('四种知识点状态都映射成人话', () => {
  assert.equal(statusLabel('new'), '还没碰过')
  assert.equal(statusLabel('learning'), '在学')
  assert.equal(statusLabel('weak'), '还不太稳')
  assert.equal(statusLabel('mastered'), '差不多了')
})

test('认不出来的状态给中性说法，不崩也不猜', () => {
  assert.equal(statusLabel('something-new'), '在学')
  assert.equal(statusLabel(''), '在学')
})

test('错因按真实 ErrorType 映射', () => {
  assert.equal(errorTypeLabel('concept_confusion'), '概念混了')
  assert.equal(errorTypeLabel('memory_gap'), '没记住')
  assert.equal(errorTypeLabel('reasoning_break'), '推理断了')
  assert.equal(errorTypeLabel('misread'), '看错了题')
})

test('未知错因键原样带出，不猜标签', () => {
  assert.equal(errorTypeLabel('brand_new'), 'brand_new')
})

test('错因按次数降序', () => {
  const ranked = rankedErrorTypes({ misread: 1, concept_confusion: 5, memory_gap: 3 })
  assert.deepEqual(
    ranked.map((r) => r.key),
    ['concept_confusion', 'memory_gap', 'misread'],
  )
  assert.equal(ranked[0].label, '概念混了')
  assert.equal(ranked[0].count, 5)
})

// --------------------------------------------------------------------------- //
// 二、⚠️ 绝不出现在界面上的东西
// --------------------------------------------------------------------------- //
test('总体状态里不含全库口径的总数', () => {
  const lines = overviewLines(report().overview)
  const joined = lines.map((l) => `${l.label}${l.value}`).join('|')

  assert.ok(!joined.includes('378'), '全库知识点总数漏进了界面')
  assert.ok(!joined.includes('375'), '"还没碰过"（全库口径）漏进了界面')
  assert.ok(joined.includes('3 个'), '按 learner 限定的数字应当保留')
})

test('知识点行里不出现原始 mastery 或 urgency', () => {
  const row = toRow(point({ mastery: 0.31, urgency: 0.7 }))
  const text = Object.values(row).join('|')

  assert.ok(!text.includes('0.31'), 'mastery 漏进了界面')
  assert.ok(!text.includes('0.7'), 'urgency 漏进了界面')
  assert.ok(!text.includes('31%'), '百分比漏进了界面')
  assert.equal(row.status, '还不太稳', '应当用状态词表达')
  assert.ok(text.includes('作答 4 次'), '次数是可以说的')
})

test('轨迹只说方向，不给数字', () => {
  const trend = trajectoryTrend([{ mastery: 0.1 }, { mastery: 0.9 }])
  assert.equal(trend, '在往上走')
  assert.ok(!String(trend).includes('0.'), '轨迹描述里不该有原始数字')
})

test('整份报告渲染出的文字里不含原始 mastery', () => {
  // 把这一页会用到的所有文案拼起来，逐条检查
  const full = report({
    weak_points: [point({ mastery: 0.31 })],
    due_reviews: [point({ mastery: 0.42, due: true })],
    trajectory: [
      { knowledge_point_id: 1, title: 'Java 线程', points: [{ at: 'x', mastery: 0.31 }] },
    ],
  })
  const rendered = [
    ...overviewLines(full.overview).map((l) => `${l.label}：${l.value}`),
    ...full.weak_points.map((p) => JSON.stringify(toRow(p))),
    ...full.due_reviews.map((p) => JSON.stringify(toRow(p))),
    full.trajectory.map((t) => trajectoryTrend(t.points) ?? '').join(' '),
    narrativeText(full),
    sessionsLabel(full.sessions.count, full.sessions.turns) ?? '',
  ].join('\n')

  for (const raw of ['0.31', '0.42', '31%', '42%', 'mastery', 'urgency']) {
    assert.ok(!rendered.includes(raw), `${raw} 不该出现在界面上`)
  }
})

// --------------------------------------------------------------------------- //
// 三、空状态
// --------------------------------------------------------------------------- //
test('没有任何学习记录时算空', () => {
  const empty = report({
    overview: { knowledge_point_total: 378, untouched: 378, tracked: 0, new: 0, learning: 0, weak: 0, mastered: 0 },
    sessions: { count: 0, turns: 0 },
  })
  assert.equal(hasAnyLearning(empty), false)
  assert.equal(reportState({ loading: false, error: null, report: empty }), 'empty')
})

test('空数据的兜底文案如实说空，不硬凑鼓励', () => {
  const empty = report({
    overview: { knowledge_point_total: 378, untouched: 378, tracked: 0, new: 0, learning: 0, weak: 0, mastered: 0 },
    sessions: { count: 0, turns: 0 },
  })
  const text = fallbackNarrative(empty)
  assert.ok(text.includes('还没有开始'))
  assert.ok(!text.includes('你已经学过'), '没学过就不能说"你已经学过"')
})

test('空数据时没有学习次数可显示', () => {
  assert.equal(sessionsLabel(0, 0), null)
})

// --------------------------------------------------------------------------- //
// 四、有数据
// --------------------------------------------------------------------------- //
test('有学习记录时算正常', () => {
  assert.equal(hasAnyLearning(report()), true)
  assert.equal(reportState({ loading: false, error: null, report: report() }), 'ready')
})

test('兜底文案只用真实计数', () => {
  const full = report({
    overview: { knowledge_point_total: 378, untouched: 375, tracked: 3, new: 0, learning: 1, weak: 1, mastered: 1 },
    due_reviews: [point({ due: true })],
  })
  const text = fallbackNarrative(full)

  assert.ok(text.includes('3 个知识点'), '要说出真实的学过数量')
  assert.ok(text.includes('1 个还不太稳'))
  assert.ok(text.includes('1 个到了该复习的时候'))
  // ⚠️ 不许编造
  assert.ok(!text.includes('小时'), '没给学习时长就不许提时长')
  assert.ok(!text.includes('进步'), '没给进步幅度就不许说进步')
})

test('没有待复习但有薄弱点 → 建议从最需要复习的开始', () => {
  const text = fallbackNarrative(report())
  assert.ok(text.includes('最需要复习'))
})

test('没有欠账 → 说可以往前学', () => {
  const clean = report({
    overview: { knowledge_point_total: 378, untouched: 376, tracked: 2, new: 0, learning: 0, weak: 0, mastered: 2 },
  })
  assert.ok(fallbackNarrative(clean).includes('往前学新的'))
})

test('学习次数文案', () => {
  assert.equal(sessionsLabel(2, 9), '2 次学习 · 9 轮问答')
})

// --------------------------------------------------------------------------- //
// 五、LLM 可用 / 不可用
// --------------------------------------------------------------------------- //
test('模型可用时用模型写的那段', () => {
  const withText = report({ narrative: { available: true, text: '你学过 3 个知识点。' } })
  assert.equal(narrativeText(withText), '你学过 3 个知识点。')
})

test('⚠️ 模型不可用时退回确定性文案 —— 页面不会没话说', () => {
  const degraded = report({ narrative: { available: false, text: '' } })
  const text = narrativeText(degraded)
  assert.ok(text.length > 0, '必须有一段话，不能是空串')
  assert.equal(text, fallbackNarrative(degraded))
})

test('narrative 标了可用但正文是空的 → 也退回兜底', () => {
  for (const text of ['', '   ']) {
    const weird = report({ narrative: { available: true, text } })
    assert.equal(narrativeText(weird), fallbackNarrative(weird))
  }
})

// --------------------------------------------------------------------------- //
// 六、页面状态
// --------------------------------------------------------------------------- //
test('页面四态', () => {
  assert.equal(reportState({ loading: true, error: null, report: null }), 'loading')
  assert.equal(reportState({ loading: false, error: '炸了', report: null }), 'error')
  assert.equal(reportState({ loading: false, error: null, report: null }), 'empty')
  assert.equal(reportState({ loading: false, error: null, report: report() }), 'ready')
})

test('⚠️ 加载中优先于空 —— 别把加载中显示成"还没有记录"', () => {
  assert.equal(reportState({ loading: true, error: null, report: null }), 'loading')
})

test('出错时即使有旧数据也算出错', () => {
  assert.equal(reportState({ loading: false, error: '连不上', report: report() }), 'error')
})

// --------------------------------------------------------------------------- //
// 七、轨迹
// --------------------------------------------------------------------------- //
test('轨迹不足两点时不给趋势', () => {
  assert.equal(trajectoryTrend([]), null)
  assert.equal(trajectoryTrend([{ mastery: 0.5 }]), null)
})

test('轨迹趋势：上升 / 回落 / 持平', () => {
  assert.equal(trajectoryTrend([{ mastery: 0.1 }, { mastery: 0.9 }]), '在往上走')
  assert.equal(trajectoryTrend([{ mastery: 0.9 }, { mastery: 0.1 }]), '有点回落')
  assert.equal(trajectoryTrend([{ mastery: 0.31 }, { mastery: 0.35 }]), '基本持平')
})
