/**
 * 待办的纯逻辑 —— 排序、校验、展示文案。**不碰 React、不碰网络。**
 *
 * ## 为什么单独一个文件
 *
 * 前端单测只收 `.ts`（`node --experimental-strip-types --test "src/**\/*.test.ts"`），
 * `.tsx` 组件测不到。所以"该不该提交、排在哪、显示成什么字"这部分抽出来，才测得到。
 *
 * ## 日期为什么全程用字符串比
 *
 * 后端把 `due_date` 存成 **DATE**（不是 DATETIME），传过来是 `"2026-09-30"`。
 * 前端也**只用 `YYYY-MM-DD` 字符串**比较，不构造 Date 对象 ——
 * 一旦经过 `new Date("2026-09-30")`，它就变成 UTC 零点，
 * 在东八区被渲染成 09-29 或 09-30 取决于怎么取字段，
 * 于是"今天到期"会莫名其妙变成"昨天到期"。
 *
 * 字符串比没有时区，也就没有这个 bug。这也是后端选 DATE 的同一个理由。
 */

import type { TodoItem } from '../../api/todos'

/** 标题长度上限，与后端 schema 同一口径。 */
export const TITLE_MAX = 255

/** 输入框→待办标题：清洗并校验。返回 null 表示**不该提交**。 */
export function cleanTitle(raw: string): string | null {
  const cleaned = (raw || '').trim()
  if (!cleaned) return null
  return cleaned.length > TITLE_MAX ? cleaned.slice(0, TITLE_MAX) : cleaned
}

/** 能不能提交（只有"标题非空"这一条）。 */
export function canSubmit(raw: string): boolean {
  return cleanTitle(raw) !== null
}

/** 本地时区的 `YYYY-MM-DD`。**不用 `toISOString()`** —— 那是 UTC，会差一天。 */
export function toISODate(date: Date): string {
  const y = date.getFullYear()
  const m = String(date.getMonth() + 1).padStart(2, '0')
  const d = String(date.getDate()).padStart(2, '0')
  return `${y}-${m}-${d}`
}

/** 排序：**未完成在前；同组内新的在上**。与后端 `list_todos` 的顺序一致。 */
export function sortTodos(items: readonly TodoItem[]): TodoItem[] {
  return [...items].sort((a, b) => {
    if (a.completed !== b.completed) return a.completed ? 1 : -1
    // 新的在上。`created_at` 是后端给的 ISO 字符串，字典序即时间序。
    if (a.created_at !== b.created_at) return a.created_at < b.created_at ? 1 : -1
    // 兜底：同一时刻创建的按 id 倒序，保证顺序稳定（否则两次渲染可能互换）
    return b.id - a.id
  })
}

/** 截止日是不是已经过了。没有截止日 → 永不算逾期。 */
export function isOverdue(todo: Pick<TodoItem, 'due_date' | 'completed'>, today: string): boolean {
  if (!todo.due_date) return false
  // 已完成的不再催 —— 逾期是"还没做且已经过了"。
  if (todo.completed) return false
  return todo.due_date < today
}

/** 截止日是不是今天。 */
export function isDueToday(todo: Pick<TodoItem, 'due_date'>, today: string): boolean {
  return todo.due_date === today
}

/**
 * 截止日怎么显示。`today` 可注入 —— 测试里传固定值，避免"今天"随运行日期变。
 *
 * 用相对说法（今天 / 明天 / 昨天）而不是绝对日期：
 * 看的人关心的是"还来不来得及"，不是"几月几号"。
 */
export function formatDueDate(dueDate: string | null, today: string): string | null {
  if (!dueDate) return null
  const offset = daysBetween(today, dueDate)
  if (offset === 0) return '今天到期'
  if (offset === 1) return '明天到期'
  if (offset === -1) return '昨天到期'
  if (offset < 0) return `已逾期 ${-offset} 天`
  if (offset <= 7) return `${offset} 天后到期`
  return `${dueDate} 到期`
}

/** 两个 `YYYY-MM-DD` 相差多少天（后 - 前）。纯字符串日期 → 用 UTC 构造避免夏令时。 */
export function daysBetween(from: string, to: string): number {
  const a = Date.UTC(...(splitISO(from) as [number, number, number]))
  const b = Date.UTC(...(splitISO(to) as [number, number, number]))
  return Math.round((b - a) / 86_400_000)
}

function splitISO(value: string): [number, number, number] {
  const [y, m, d] = value.split('-').map(Number)
  return [y || 1970, (m || 1) - 1, d || 1]
}

/** 完成态相关的文案。 */
export const STATUS_LABEL = {
  done: '已完成',
  open: '待完成',
} as const

/** 单条待办的显示摘要 —— 组件只负责画，判断都在这里。 */
export interface TodoDisplay {
  title: string
  due: string | null
  overdue: boolean
  dueToday: boolean
  status: string
  /** 已完成的项目在视觉上要弱化 */
  muted: boolean
}

export function describe(todo: TodoItem, today: string): TodoDisplay {
  return {
    title: todo.title,
    due: formatDueDate(todo.due_date, today),
    overdue: isOverdue(todo, today),
    dueToday: isDueToday(todo, today),
    status: todo.completed ? STATUS_LABEL.done : STATUS_LABEL.open,
    muted: todo.completed,
  }
}

/** 页面的整体状态。**顺序有意为之**：先有数据再说空，别把"加载中"当"空"。 */
export type ListState = 'loading' | 'error' | 'empty' | 'ready'

export function listState(input: {
  loading: boolean
  error: string | null
  count: number
}): ListState {
  if (input.loading) return 'loading'
  if (input.error) return 'error'
  return input.count === 0 ? 'empty' : 'ready'
}

/** 新增框的提示语：还有多少条没做完。列表为空时不显示。 */
export function remainingLabel(items: readonly TodoItem[]): string | null {
  const left = items.filter((item) => !item.completed).length
  if (left === 0) return null
  return `还有 ${left} 条没做完`
}
