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

/* ══════════════════════════════════════════════════════════════════════════
   计时（Phase 5D+）

   两种模式，语义完全不同：

   | 模式 | 看的是 | 归零时 |
   |---|---|---|
   | `countup` | 「**已经花了多久**」 | 不会归零，一直往上走 |
   | `countdown` | 「**还剩多少**」 | 到 0 就停，提示一句 |

   ⚠️ `spent_seconds` 是**累计**值，不是某一次的时长 ——
   暂停、继续、刷新页面，它都接着累加（前端运行中的秒数另算，见下）。

   ⚠️ **计时跑起来的秒数不进这里**：那是前端内存里的 `runningSeconds`，
   定期回写成 `spent_seconds`。纯函数只负责把两者加起来算显示值，
   不持有任何"现在几点"的状态 —— 否则就没法测了 ✗
   ══════════════════════════════════════════════════════════════════════════ */

/** 这条待办能不能计时（不计时的、已完成的，都不能）。 */
export function canRunTimer(todo: Pick<TodoItem, 'timer_mode' | 'completed'>): boolean {
  return todo.timer_mode !== 'none' && !todo.completed
}

/**
 * 当前该显示的总秒数。
 *
 * `runningSeconds` 是**这次运行**已经跑过的秒数（未落库的那部分）。
 * 倒计时会**封底到 0**，不会显示负数 —— 归零后停在那儿 ✓
 */
export function timerSeconds(
  todo: Pick<TodoItem, 'timer_mode' | 'timer_minutes' | 'spent_seconds'>,
  runningSeconds = 0,
): number {
  const total = Math.max(0, todo.spent_seconds + Math.max(0, runningSeconds))
  if (todo.timer_mode === 'countdown') {
    const target = (todo.timer_minutes ?? 0) * 60
    return Math.max(0, target - total)
  }
  return total
}

/** 倒计时是不是已经走完了。 */
export function isTimerOver(
  todo: Pick<TodoItem, 'timer_mode' | 'timer_minutes' | 'spent_seconds'>,
  runningSeconds = 0,
): boolean {
  if (todo.timer_mode !== 'countdown') return false
  const target = (todo.timer_minutes ?? 0) * 60
  return target > 0 && todo.spent_seconds + Math.max(0, runningSeconds) >= target
}

/**
 * 秒数 → `mm:ss`（超过一小时变 `h:mm:ss`）。
 *
 * 不做"约等于几分钟"这种模糊化 —— 计时器显示的就是精确到秒的时间，
 * 这是它唯一的作用 ✓
 */
export function formatClock(seconds: number): string {
  const safe = Math.max(0, Math.floor(seconds))
  const hours = Math.floor(safe / 3600)
  const minutes = Math.floor((safe % 3600) / 60)
  const secs = safe % 60
  const mm = String(minutes).padStart(2, '0')
  const ss = String(secs).padStart(2, '0')
  return hours > 0 ? `${hours}:${mm}:${ss}` : `${mm}:${ss}`
}

/** 计时条上显示的文字。不计时的返回 `null`。 */
export function timerLabel(
  todo: Pick<TodoItem, 'timer_mode' | 'timer_minutes' | 'spent_seconds'>,
  runningSeconds = 0,
): string | null {
  if (todo.timer_mode === 'none') return null
  const shown = timerSeconds(todo, runningSeconds)
  if (todo.timer_mode === 'countdown') {
    return isTimerOver(todo, runningSeconds) ? '时间到了' : `剩 ${formatClock(shown)}`
  }
  return formatClock(shown)
}

/** 累计时长给一句人话（列表里不显示，报告/详情里用）。 */
export function spentLabel(seconds: number): string | null {
  if (seconds < 60) return null
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `累计 ${minutes} 分钟`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest ? `累计 ${hours} 小时 ${rest} 分` : `累计 ${hours} 小时`
}

/**
 * 添加框里的计时选项 → **提交给后端的两个字段**（snake_case）。
 *
 * 后端要求这两个字段**成对且自洽**（`countdown` 必须给分钟数，
 * 其余必须不给）✓ 这个小函数是那层转换的唯一样本，
 * 免得每处调用各写一遍、各错一次 ✗
 *
 * ⚠️ 返回的是后端字段名。`createTodo` 接收的是 camelCase 选项，
 * 两边由各自负责映射 —— 想直接对后端的调用方（比如 PATCH）用这个 ✓
 */
export function timerPayload(
  mode: 'none' | 'countup' | 'countdown',
  minutes: number | null,
): { timer_mode: 'none' | 'countup' | 'countdown'; timer_minutes: number | null } {
  if (mode !== 'countdown') return { timer_mode: mode, timer_minutes: null }
  return { timer_mode: mode, timer_minutes: minutes }
}

/** 倒计时分钟数是否合法（与后端 `TIMER_MINUTES_MIN/MAX` 同一口径）。 */
export const TIMER_MINUTES_MIN = 1
export const TIMER_MINUTES_MAX = 600

export function validateTimerMinutes(value: number | null): string | null {
  if (value === null || value === undefined || Number.isNaN(value)) return '填个分钟数'
  if (!Number.isInteger(value)) return '要整数分钟'
  if (value < TIMER_MINUTES_MIN) return `至少 ${TIMER_MINUTES_MIN} 分钟`
  if (value > TIMER_MINUTES_MAX) return `不超过 ${TIMER_MINUTES_MAX} 分钟`
  return null
}
