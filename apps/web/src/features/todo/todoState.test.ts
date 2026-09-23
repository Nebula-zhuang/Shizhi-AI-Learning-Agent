/**
 * 待办纯逻辑测试（Phase 5D）。
 *
 * ## 这组测试守的核心
 *
 * | 不变量 | 为什么 |
 * |---|---|
 * | **空标题不能提交** | 后端也会拒（422），但让用户先被拒一次是多余的往返；而且按钮该是灰的 |
 * | **未完成在前** | 做完的事不该占着视野的上半部分 |
 * | **已完成不再算逾期** | 逾期意味着"还没做且过了"；做完的事不该继续变红 |
 * | **日期比较不带时区** | `new Date("2026-09-30")` 是 UTC 零点，在东八区会差一天 |
 *
 * 所有涉及"今天"的用例都**注入固定的 today** —— 否则测试会在某几个日期
 * 莫名其妙地红，而那是运行日期的问题，不是代码的问题。
 *
 * 运行：`npm run test:unit`
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  TITLE_MAX,
  canSubmit,
  cleanTitle,
  daysBetween,
  describe,
  formatDueDate,
  isDueToday,
  isOverdue,
  listState,
  remainingLabel,
  sortTodos,
  toISODate,
} from './todoState.ts'
import type { TodoItem } from '../../api/todos'

// --------------------------------------------------------------------------- //
// 造数据
// --------------------------------------------------------------------------- //
function todo(over: Partial<TodoItem> = {}): TodoItem {
  return {
    id: 1,
    title: '复习 Java 线程',
    completed: false,
    due_date: null,
    created_at: '2026-09-20T10:00:00',
    updated_at: '2026-09-20T10:00:00',
    ...over,
  }
}

const TODAY = '2026-09-23'

// --------------------------------------------------------------------------- //
// 一、标题清洗与校验
// --------------------------------------------------------------------------- //
test('空标题不能提交', () => {
  for (const raw of ['', '   ', '\t', '\n', '  \n  ']) {
    assert.equal(canSubmit(raw), false, `${JSON.stringify(raw)} 不该可提交`)
    assert.equal(cleanTitle(raw), null)
  }
})

test('正常标题可以提交，且清掉首尾空白', () => {
  assert.equal(canSubmit('  复习线程  '), true)
  assert.equal(cleanTitle('  复习线程  '), '复习线程')
})

test('超长标题被截断到上限', () => {
  const long = 'a'.repeat(TITLE_MAX + 50)
  const cleaned = cleanTitle(long)
  assert.ok(cleaned)
  assert.equal(cleaned.length, TITLE_MAX)
  assert.equal(canSubmit(long), true, '超长仍然可提交，只是会被截断')
})

// --------------------------------------------------------------------------- //
// 二、排序：未完成优先
// --------------------------------------------------------------------------- //
test('未完成的排在前面，已完成沉到后面', () => {
  const items = [
    todo({ id: 1, title: '已完成', completed: true, created_at: '2026-09-23T12:00:00' }),
    todo({ id: 2, title: '未完成', completed: false, created_at: '2026-09-20T12:00:00' }),
  ]
  const sorted = sortTodos(items)
  assert.deepEqual(
    sorted.map((t) => t.title),
    ['未完成', '已完成'],
    '新的已完成项也不该压过旧的未完成项',
  )
})

test('同为未完成时，新的在上', () => {
  const items = [
    todo({ id: 1, title: '旧的', created_at: '2026-09-20T10:00:00' }),
    todo({ id: 2, title: '新的', created_at: '2026-09-22T10:00:00' }),
  ]
  assert.deepEqual(
    sortTodos(items).map((t) => t.title),
    ['新的', '旧的'],
  )
})

test('同一时刻创建时按 id 倒序，顺序稳定', () => {
  const same = '2026-09-20T10:00:00'
  const items = [todo({ id: 1, created_at: same }), todo({ id: 2, created_at: same })]
  assert.deepEqual(
    sortTodos(items).map((t) => t.id),
    [2, 1],
  )
  assert.deepEqual(
    sortTodos(items).map((t) => t.id),
    [2, 1],
    '两次排序结果必须一样，否则界面会闪',
  )
})

test('排序不修改原数组', () => {
  const items = [todo({ id: 1, completed: true }), todo({ id: 2, completed: false })]
  const before = items.map((t) => t.id)
  sortTodos(items)
  assert.deepEqual(
    items.map((t) => t.id),
    before,
  )
})

// --------------------------------------------------------------------------- //
// 三、日期：展示 / 逾期（全程字符串，不带时区）
// --------------------------------------------------------------------------- //
test('toISODate 用本地时区，不给 UTC 差一天的机会', () => {
  // 本地 2026-09-23 00:30 —— 若用 toISOString() 在 UTC+8 会变成 09-22
  assert.equal(toISODate(new Date(2026, 8, 23, 0, 30)), '2026-09-23')
  assert.equal(toISODate(new Date(2026, 8, 23, 23, 59)), '2026-09-23')
  assert.equal(toISODate(new Date(2026, 0, 1)), '2026-01-01')
})

test('没有截止日 → 不显示、也不算逾期', () => {
  assert.equal(formatDueDate(null, TODAY), null)
  assert.equal(isOverdue(todo({ due_date: null }), TODAY), false)
})

test('截止日的相对说法', () => {
  assert.equal(formatDueDate('2026-09-23', TODAY), '今天到期')
  assert.equal(formatDueDate('2026-09-24', TODAY), '明天到期')
  assert.equal(formatDueDate('2026-09-22', TODAY), '昨天到期')
  assert.equal(formatDueDate('2026-09-25', TODAY), '2 天后到期')
  assert.equal(formatDueDate('2026-09-30', TODAY), '7 天后到期')
})

test('久远的截止日显示绝对日期', () => {
  assert.equal(formatDueDate('2026-12-31', TODAY), '2026-12-31 到期')
})

test('逾期判断：过了今天才算逾期', () => {
  assert.equal(isOverdue(todo({ due_date: '2026-09-22' }), TODAY), true, '昨天 → 逾期')
  assert.equal(isOverdue(todo({ due_date: '2026-09-23' }), TODAY), false, '今天 → 还没逾期')
  assert.equal(isOverdue(todo({ due_date: '2026-09-24' }), TODAY), false)
})

test('⚠️ 已完成的待办不再算逾期', () => {
  // 逾期 = "还没做且已经过了"。做完的事不该继续变红。
  assert.equal(isOverdue(todo({ due_date: '2026-09-01', completed: true }), TODAY), false)
  assert.equal(isDueToday(todo({ due_date: '2026-09-23', completed: true }), TODAY), true, '日期本身还对，只是不该催')
})

test('跨月与跨年算天数正确', () => {
  assert.equal(daysBetween('2026-09-30', '2026-10-01'), 1)
  assert.equal(daysBetween('2026-12-31', '2027-01-01'), 1)
  assert.equal(daysBetween('2026-02-28', '2026-03-01'), 1, '2026 不是闰年')
  assert.equal(daysBetween('2026-09-23', '2026-09-23'), 0)
})

// --------------------------------------------------------------------------- //
// 四、完成态
// --------------------------------------------------------------------------- //
test('完成态文案与弱化标记', () => {
  const open = describe(todo({ completed: false }), TODAY)
  assert.equal(open.status, '待完成')
  assert.equal(open.muted, false)

  const done = describe(todo({ completed: true }), TODAY)
  assert.equal(done.status, '已完成')
  assert.equal(done.muted, true, '已完成要在视觉上弱化')
})

test('describe 把展示用得到的都算好了', () => {
  const got = describe(todo({ due_date: '2026-09-22' }), TODAY)
  assert.equal(got.title, '复习 Java 线程')
  assert.equal(got.due, '昨天到期')
  assert.equal(got.overdue, true)
  assert.equal(got.dueToday, false)
})

// --------------------------------------------------------------------------- //
// 五、页面状态
// --------------------------------------------------------------------------- //
test('页面的整体状态：加载 → 出错 → 空 → 正常', () => {
  assert.equal(listState({ loading: true, error: null, count: 0 }), 'loading')
  assert.equal(listState({ loading: false, error: '炸了', count: 0 }), 'error')
  assert.equal(listState({ loading: false, error: null, count: 0 }), 'empty')
  assert.equal(listState({ loading: false, error: null, count: 3 }), 'ready')
})

test('⚠️ 加载中优先于"空" —— 别把加载中显示成空列表', () => {
  assert.equal(listState({ loading: true, error: null, count: 0 }), 'loading')
})

test('出错时即使有旧数据也算出错', () => {
  assert.equal(listState({ loading: false, error: '连不上', count: 5 }), 'error')
})

test('未完成计数：都做完了就不显示', () => {
  assert.equal(remainingLabel([]), null, '空列表没什么可说的')
  assert.equal(remainingLabel([todo({ completed: true })]), null, '全做完就不催了')
  assert.equal(
    remainingLabel([todo({ completed: false }), todo({ completed: true }), todo({ completed: false })]),
    '还有 2 条没做完',
  )
})
