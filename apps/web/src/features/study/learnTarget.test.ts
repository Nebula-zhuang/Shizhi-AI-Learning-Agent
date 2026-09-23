/**
 * 「开始学习」纯逻辑测试（Phase 5C-2）。
 *
 * ## 这组测试守的核心
 *
 * **拿不到可信的 kpId 就不许去开教学。**
 *
 * `startLearning(focus)` 会立刻发起一轮真实教学。所以这里测的不是"匹配算法"
 * （那在后端，用 `tests/api/test_study_learn_target.py` 测），
 * 而是**前端有没有把不可信的结果当成可信的**：
 * `matched` 与 `kp_id` 只要有一个不成立，就必须降级。
 *
 * 运行：`npm run test:unit`
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { PICKER_LABEL, STAY_LABEL, learnOutcome, stayHint } from './learnTarget.ts'
import type { LearnTargetResponse } from '../../api/study'

function ok(overrides: Partial<LearnTargetResponse> = {}): LearnTargetResponse {
  return {
    matched: true,
    kp_id: 42,
    title: 'Java 线程',
    document_id: 7,
    reason: 'exact',
    ...overrides,
  }
}

function miss(overrides: Partial<LearnTargetResponse> = {}): LearnTargetResponse {
  return {
    matched: false,
    kp_id: null,
    title: '',
    document_id: null,
    reason: 'none',
    ...overrides,
  }
}

// --------------------------------------------------------------------------- //
// 一、匹配成功 → 去辅导（focus 三个字段都要对）
// --------------------------------------------------------------------------- //
test('匹配成功时给出完整的 LearningFocus', () => {
  const outcome = learnOutcome(ok())
  assert.equal(outcome.kind, 'go')
  assert.ok(outcome.kind === 'go')
  assert.deepEqual(outcome.focus, { kpId: 42, title: 'Java 线程', documentId: 7 })
})

test('三种匹配级别都同样能去辅导', () => {
  for (const reason of ['exact', 'normalized', 'contained']) {
    const outcome = learnOutcome(ok({ reason }))
    assert.equal(outcome.kind, 'go', `${reason} 应当可以开始学习`)
  }
})

test('documentId 缺失时给 null，而不是编一个', () => {
  const outcome = learnOutcome(ok({ document_id: null }))
  assert.ok(outcome.kind === 'go')
  assert.equal(outcome.focus.documentId, null)
})

test('标题两边的空格被清掉', () => {
  const outcome = learnOutcome(ok({ title: '  Java 线程  ' }))
  assert.ok(outcome.kind === 'go')
  assert.equal(outcome.focus.title, 'Java 线程')
})

// --------------------------------------------------------------------------- //
// 二、**不许把不可信的结果当可信**（防伪 kpId）
// --------------------------------------------------------------------------- //
test('没有匹配时不产生 kpId，也不去辅导', () => {
  const outcome = learnOutcome(miss())
  assert.equal(outcome.kind, 'stay')
  assert.ok(!('focus' in outcome), '降级结果里不该有任何 focus')
})

test('matched 说是真、但 kp_id 是空 → 仍然降级', () => {
  // 脏数据：字段缺失却标记为真。**只信 matched 就会踩这个坑。**
  for (const bad of [null, undefined, 0, -1, 1.5, NaN]) {
    const outcome = learnOutcome(ok({ kp_id: bad as number | null }))
    assert.equal(outcome.kind, 'stay', `kp_id=${String(bad)} 不该被当成可信 id`)
  }
})

test('kp_id 有值但 title 是空的 → 仍然降级', () => {
  for (const title of ['', '   ', null as unknown as string]) {
    const outcome = learnOutcome(ok({ title }))
    assert.equal(outcome.kind, 'stay', `title=${JSON.stringify(title)} 不该被当成可信结果`)
  }
})

test('matched 为假但 kp_id 有值 → 仍然降级（以 matched 为准）', () => {
  const outcome = learnOutcome(ok({ matched: false }))
  assert.equal(outcome.kind, 'stay')
})

// --------------------------------------------------------------------------- //
// 三、降级文案：歧义不替用户猜
// --------------------------------------------------------------------------- //
test('歧义时明确说"不替你挑"', () => {
  const hint = stayHint('ambiguous')
  assert.ok(hint.includes('辅导'), '要指出去哪能自己挑')
  assert.ok(hint.includes('不替你挑') || hint.includes('好几个'), '要说明为什么没直接开始')
})

test('普通没匹配上时说明"资料里还没讲到"', () => {
  const hint = stayHint('none')
  assert.ok(hint.includes('辅导'))
  assert.ok(!hint.includes('好几个'), '别把"没有"说成"有多个"')
})

test('未知的 reason 也给一句能看的话', () => {
  assert.ok(stayHint('').length > 0)
  assert.ok(stayHint('something-new').length > 0)
})

test('降级两个选项的文案固定', () => {
  assert.equal(STAY_LABEL, '继续自由学习')
  assert.equal(PICKER_LABEL, '去辅导挑一个')
})

// --------------------------------------------------------------------------- //
// 四、降级后**没有副作用**
// --------------------------------------------------------------------------- //
test('降级结果里不含任何能给 startLearning 用的东西', () => {
  const outcome = learnOutcome(miss({ reason: 'ambiguous' }))
  assert.equal(outcome.kind, 'stay')
  // 只有 focus 能被喂给 startLearning；降级结果里没有它
  assert.deepEqual(Object.keys(outcome).sort(), ['hint', 'kind'])
})
