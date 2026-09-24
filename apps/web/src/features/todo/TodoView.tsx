/**
 * 待办 —— 一个独立的清单页。
 *
 * ## 边界（刻意不做的事）
 *
 * - 不接 AI：不自动生成待办、不按学习情况推荐做什么
 * - 不接定时/提醒：没有 scheduler、没有通知
 * - 不接知识点：待办与 `knowledge_points` 之间**没有**任何自动关联
 *
 * 这一页只回答一个问题：**「我要做什么」**。
 *
 * ## 判断都在 `todoState.ts` 里
 *
 * 排序、逾期、展示文案、页面状态全部在纯逻辑模块里，这里只负责画和调接口。
 * 原因很实际：前端单测只收 `.ts`，`.tsx` 组件测不到 ——
 * 把判断留在组件里，就等于那些判断没有测试。
 *
 * ## 归属由后端保证
 *
 * 这里**不做任何"这条是不是我的"的判断** —— 列表拿到的就是自己的，
 * 单条操作（改/删）拿到 404 就是"不存在或不是你的"。
 * 前端判归属只会给人"有防护"的错觉。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import {
  createTodo,
  deleteTodo,
  listTodos,
  updateTodo,
  type TimerMode,
  type TodoItem,
} from '../../api/todos'
import { messageOf } from '../../api/http'
import { Reveal } from '../../motion/primitives'
import {
  Button,
  Card,
  CheckMark,
  EmptyState,
  ErrorState,
  IconClock,
  IconInbox,
  IconPause,
  IconPlus,
  IconTrash,
  Modal,
  Skeleton,
  cn,
  useToast,
} from '../../ui'
import {
  TIMER_MINUTES_MAX,
  TIMER_MINUTES_MIN,
  canRunTimer,
  canSubmit,
  cleanTitle,
  describe,
  isOverdue,
  isTimerOver,
  listState,
  remainingLabel,
  sortTodos,
  timerLabel,
  timerPayload,
  toISODate,
  validateTimerMinutes,
} from './todoState'

/** 新增框里的三个选项。**顺序即推荐顺序**：默认第一个是不计时 ✓ */
const TIMER_CHOICES: Array<{ key: TimerMode; label: string }> = [
  { key: 'none', label: '不计时' },
  { key: 'countup', label: '计时' },
  { key: 'countdown', label: '倒计时' },
]

export function TodoView() {
  const toast = useToast()

  const [items, setItems] = useState<TodoItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [draft, setDraft] = useState('')
  const [adding, setAdding] = useState(false)
  /** 正在改的那一条（勾选/重命名期间禁用它自己的按钮，防连点） */
  const [busyId, setBusyId] = useState<number | null>(null)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editDraft, setEditDraft] = useState('')
  const [pendingDelete, setPendingDelete] = useState<TodoItem | null>(null)

  // ── 新增框里的计时选择
  const [timerMode, setTimerMode] = useState<TimerMode>('none')
  const [timerMinutesText, setTimerMinutesText] = useState('25')

  // ── 正在跑的那一条。
  //
  // ⚠️ **只记开始时刻，不累加秒数**。原因有两个，都是坑：
  //   1. 累加就会丢 tick —— 标签页切到后台时 `setInterval` 会被节流，
  //      一分钟只跑几次，累加出来的时长就是错的 ✗
  //   2. 每 15 秒落库要靠 `spent_seconds + 本地秒数`，而闭包里的 items 会过期，
  //      结算时用的是旧值 → **直接把时长算没** ✗✗
  // 用「现在 - 开始时刻」就没这两个问题：墙钟不会漏 ✓ 也不需要累加状态 ✓
  const [runningId, setRunningId] = useState<number | null>(null)
  const [runStartedAt, setRunStartedAt] = useState<number | null>(null)
  /** 每秒更新一次的"现在"，只为驱动重渲染 —— 不参与任何计算口径 ✓ */
  const [tickNow, setTickNow] = useState(() => Date.now())

  /** 最近一次渲染的 items，给计时结算用（避开闭包过期）*/
  const itemsRef = useRef<TodoItem[]>([])
  itemsRef.current = items

  /** 这次运行已经跑了多少秒（派生值，不落库） */
  const runningSeconds =
    runningId !== null && runStartedAt !== null
      ? Math.max(0, Math.floor((tickNow - runStartedAt) / 1000))
      : 0

  // 「今天」取一次。跨零点不刷新 —— 待办页停留跨天是极小概率，
  // 而为此挂一个定时器属于"为了边界情况付常态成本"。
  const today = useMemo(() => toISODate(new Date()), [])

  /** 把新列表**统一过一遍排序**再放进 state —— 勾选后不重新拉取也不会乱序。 */
  const apply = useCallback((next: TodoItem[]) => setItems(sortTodos(next)), [])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await listTodos()
      apply(data.items)
    } catch (err) {
      setError(messageOf(err))
    } finally {
      setLoading(false)
    }
  }, [apply])

  useEffect(() => {
    void load()
  }, [load])

  // 只有真的有计时在跑时才挂这个 1 秒的定时器 —— 不在跑的时候一个定时器都不挂 ✓
  useEffect(() => {
    if (runningId === null) return
    const timer = window.setInterval(() => setTickNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [runningId])

  // ── 计时
  //
  // `flushRun` 只负责"把一段秒数加进 spent_seconds"，**不动 runningId** ——
  // 这是刻意的：切换任务时要先把上一个结算掉，如果它顺手把 runningId 置空，
  // 就会把刚启动的那个也一起清掉（异步先后顺序问题）✗
  const flushRun = useCallback(
    async (id: number, seconds: number) => {
      if (seconds <= 0) return
      const target = itemsRef.current.find((item) => item.id === id)
      if (!target) return
      const total = target.spent_seconds + seconds
      try {
        const updated = await updateTodo(id, { spent_seconds: total })
        setItems((prev) => prev.map((item) => (item.id === id ? updated : item)))
      } catch (err) {
        toast.error('计时没能存下来', messageOf(err))
      }
    },
    [toast],
  )

  /** 暂停：结算这次运行的秒数，然后停下。 */
  const pauseTimer = useCallback(() => {
    if (runningId === null) return
    const seconds = Math.floor((Date.now() - (runStartedAt ?? Date.now())) / 1000)
    void flushRun(runningId, seconds)
    setRunningId(null)
    setRunStartedAt(null)
  }, [runningId, runStartedAt, flushRun])

  /** 开跑 / 切到这一条。切之前先把上一条结算掉。 */
  const startTimer = useCallback(
    (todo: TodoItem) => {
      if (!canRunTimer(todo)) return
      if (runningId !== null && runningId !== todo.id && runStartedAt !== null) {
        const seconds = Math.floor((Date.now() - runStartedAt) / 1000)
        void flushRun(runningId, seconds)
      }
      const now = Date.now()
      setTickNow(now)
      setRunStartedAt(now)
      setRunningId(todo.id)
    },
    [runningId, runStartedAt, flushRun],
  )

  // 每 15 秒把这批秒数落库一次，然后**重置起点** ——
  // 显示值 = 已落库的 + 这次运行至今的，所以重置起点不会让表盘跳回 0 ✓
  // 这样一次会话中途关掉标签页，最多只丢 15 秒 ✓
  useEffect(() => {
    if (runningId === null || runStartedAt === null) return
    const flush = window.setInterval(() => {
      const seconds = Math.floor((Date.now() - runStartedAt) / 1000)
      if (seconds < 15) return
      void flushRun(runningId, seconds)
      setRunStartedAt(Date.now())
    }, 15000)
    return () => window.clearInterval(flush)
  }, [runningId, runStartedAt, flushRun])

  // 倒计时走完：停下 + 说一声。
  // ⚠️ **不自动打勾** —— 归零只说明"这段时间用完了"，不代表这条做完了
  // （用户很可能中途去干别的了）✓ 这是产品口径，不是省事。
  const overNotified = useRef<number | null>(null)
  useEffect(() => {
    if (runningId === null) return
    const target = items.find((item) => item.id === runningId)
    if (!target || overNotified.current === runningId) return
    if (!isTimerOver(target, runningSeconds)) return
    overNotified.current = runningId
    toast.info('时间到了', `「${target.title}」的倒计时走完了。`)
    pauseTimer()
  }, [runningId, items, runningSeconds, toast, pauseTimer])

  // ── 新增
  const handleAdd = useCallback(async () => {
    const title = cleanTitle(draft)
    if (!title) return
    // 倒计时要在前端先校验分钟数 —— 让用户按按钮之前就知道哪里不对 ✓
    const mode = timerMode
    const minutes = Number.parseInt(timerMinutesText, 10)
    if (mode === 'countdown' && validateTimerMinutes(Number.isNaN(minutes) ? null : minutes)) {
      toast.error('倒计时分钟数不对', `填 ${TIMER_MINUTES_MIN}–${TIMER_MINUTES_MAX} 之间的整数。`)
      return
    }
    setAdding(true)
    try {
      // timerPayload 给的是后端字段名，createTodo 收的是 camelCase 选项 ——
      // 这里用一个中间变量把它们对上，**转换规则仍然只有一份**（在 timerPayload 里）✓
      const fields = timerPayload(mode, mode === 'countdown' ? minutes : null)
      const created = await createTodo(title, {
        timerMode: fields.timer_mode,
        timerMinutes: fields.timer_minutes,
      })
      apply([created, ...items])
      setDraft('')
    } catch (err) {
      toast.error('没能加进去', messageOf(err))
    } finally {
      setAdding(false)
    }
  }, [draft, items, apply, toast, timerMode, timerMinutesText])

  // ── 勾选 / 取消
  const handleToggle = useCallback(
    async (todo: TodoItem) => {
      // 完成之前先把正在跑的计时结掉，否则那段时长会随着计时重置而丢掉 ✗
      if (runningId === todo.id) pauseTimer()
      setBusyId(todo.id)
      try {
        const updated = await updateTodo(todo.id, { completed: !todo.completed })
        apply(items.map((item) => (item.id === updated.id ? updated : item)))
      } catch (err) {
        // 404 表示这条已经不在了（别处删掉）—— 从列表里摘掉，并如实说明
        if (messageOf(err).includes('不存在')) {
          apply(items.filter((item) => item.id !== todo.id))
          toast.info('这条已经不在了', '它可能刚被删掉了。')
        } else {
          toast.error('没能改动', messageOf(err))
        }
      } finally {
        setBusyId(null)
      }
    },
    [items, apply, toast, runningId, pauseTimer],
  )

  // ── 行内重命名
  const startEdit = useCallback((todo: TodoItem) => {
    setEditingId(todo.id)
    setEditDraft(todo.title)
  }, [])

  const commitEdit = useCallback(async () => {
    if (editingId === null) return
    const title = cleanTitle(editDraft)
    if (!title) {
      // 空标题：不提交，也不静默丢弃 —— 提示一句，保留编辑态让他改
      toast.error('标题不能为空', '写点什么，或者按 Esc 取消。')
      return
    }
    const target = items.find((item) => item.id === editingId)
    setEditingId(null)
    if (!target || target.title === title) return // 没改就不打扰后端
    setBusyId(target.id)
    try {
      const updated = await updateTodo(target.id, { title })
      apply(items.map((item) => (item.id === updated.id ? updated : item)))
    } catch (err) {
      toast.error('没能改名', messageOf(err))
    } finally {
      setBusyId(null)
    }
  }, [editingId, editDraft, items, apply, toast])

  // ── 删除（确认后）
  const confirmDelete = useCallback(async () => {
    if (!pendingDelete) return
    const target = pendingDelete
    // 要删的那条正在计时 → 先停下，否则计时器会指向一条不存在的记录
    if (runningId === target.id) pauseTimer()
    setPendingDelete(null)
    try {
      await deleteTodo(target.id)
      apply(items.filter((item) => item.id !== target.id))
      toast.success('已删除', `「${target.title}」清理掉了。`)
    } catch (err) {
      toast.error('没能删除', messageOf(err))
    }
  }, [pendingDelete, items, apply, toast])

  const state = listState({ loading, error, count: items.length })
  const remaining = remainingLabel(items)

  return (
    <div className="space-y-8">
      <header>
        <Reveal>
          <p className="meta tracking-[0.14em]">待办</p>
          <h1 className="display mt-2.5">我要做什么</h1>
          <p className="mt-3 text-sm text-ink-3">
            {remaining ?? '想记就记，做完打勾。'}
          </p>
        </Reveal>
      </header>

      {/* ── 新增 */}
      <Reveal>
        <Card className="p-3">
          <div className="flex items-center gap-2">
            <input
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
                  e.preventDefault()
                  void handleAdd()
                }
              }}
              placeholder="要做什么？"
              aria-label="新的待办"
              className="w-full rounded-lg border border-line-strong bg-surface-1 px-2.5 py-1.5 text-sm text-ink-1 outline-none placeholder:text-ink-4 focus:border-moss-line"
            />
            <Button
              variant="primary"
              size="sm"
              icon={<IconPlus size={14} />}
              // 空白标题不给点：后端也会拒（422），但没必要让用户先被拒一次
              disabled={!canSubmit(draft) || adding}
              loading={adding}
              onClick={() => void handleAdd()}
            >
              添加
            </Button>
          </div>

          {/* 计时是可选的 —— 默认"不计时"，不打扰只想记一笔的人 ✓ */}
          <div className="mt-2 flex flex-wrap items-center gap-2 border-t border-line pt-2">
            <span className="text-2xs text-ink-4">计时</span>
            {TIMER_CHOICES.map((choice) => (
              <button
                key={choice.key}
                type="button"
                onClick={() => setTimerMode(choice.key)}
                aria-pressed={timerMode === choice.key}
                className={cn(
                  'rounded-full border px-2.5 py-0.5 text-2xs transition-colors',
                  timerMode === choice.key
                    ? 'border-moss-line bg-moss-soft text-moss-ink'
                    : 'border-line-strong bg-surface-1 text-ink-3 hover:border-moss-line',
                )}
              >
                {choice.label}
              </button>
            ))}
            {timerMode === 'countdown' && (
              <span className="flex items-center gap-1.5">
                <input
                  value={timerMinutesText}
                  onChange={(e) => setTimerMinutesText(e.target.value)}
                  inputMode="numeric"
                  aria-label="倒计时分钟数"
                  className="w-14 rounded-lg border border-line-strong bg-surface-1 px-2 py-0.5 text-2xs text-ink-1 outline-none focus:border-moss-line"
                />
                <span className="text-2xs text-ink-4">分钟</span>
              </span>
            )}
          </div>
        </Card>
      </Reveal>

      {state === 'loading' && (
        <div className="space-y-2">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-12 w-full rounded-lg" />
          ))}
        </div>
      )}

      {state === 'error' && (
        <ErrorState
          title="没能读到你的待办"
          message={error ?? undefined}
          onRetry={() => void load()}
        />
      )}

      {state === 'empty' && (
        <EmptyState
          icon={<IconInbox size={22} />}
          title="还没有待办"
          description="上面写一句，按回车就加进来了。"
        />
      )}

      {state === 'ready' && (
        <ul className="space-y-2">
          {items.map((todo) => (
            <li key={todo.id}>
              <TodoRow
                todo={todo}
                today={today}
                busy={busyId === todo.id}
                editing={editingId === todo.id}
                editDraft={editDraft}
                running={runningId === todo.id}
                runningSeconds={runningId === todo.id ? runningSeconds : 0}
                onEditDraft={setEditDraft}
                onToggle={() => void handleToggle(todo)}
                onStartEdit={() => startEdit(todo)}
                onCommitEdit={() => void commitEdit()}
                onCancelEdit={() => setEditingId(null)}
                onDelete={() => setPendingDelete(todo)}
                onToggleTimer={() => (runningId === todo.id ? pauseTimer() : startTimer(todo))}
              />
            </li>
          ))}
        </ul>
      )}

      <Modal
        open={pendingDelete !== null}
        onClose={() => setPendingDelete(null)}
        title="删掉这条待办？"
        description={pendingDelete ? `「${pendingDelete.title}」会被移除。` : undefined}
        footer={
          <div className="flex justify-end gap-2">
            <Button size="sm" onClick={() => setPendingDelete(null)}>
              先留着
            </Button>
            <Button variant="danger" size="sm" onClick={() => void confirmDelete()}>
              删掉
            </Button>
          </div>
        }
      />
    </div>
  )
}

/** 一行待办。勾选 / 行内改名 / 删除。 */
function TodoRow({
  todo,
  today,
  busy,
  editing,
  editDraft,
  running,
  runningSeconds,
  onEditDraft,
  onToggle,
  onStartEdit,
  onCommitEdit,
  onCancelEdit,
  onDelete,
  onToggleTimer,
}: {
  todo: TodoItem
  today: string
  busy: boolean
  editing: boolean
  editDraft: string
  /** 这条的计时是不是正在跑 */
  running: boolean
  /** 这次运行至今的秒数（未落库的那部分）。不在跑时是 0 */
  runningSeconds: number
  onEditDraft: (value: string) => void
  onToggle: () => void
  onStartEdit: () => void
  onCommitEdit: () => void
  onCancelEdit: () => void
  onDelete: () => void
  onToggleTimer: () => void
}) {
  const view = describe(todo, today)
  const late = isOverdue(todo, today)
  const timer = timerLabel(todo, runningSeconds)
  /** 倒计时走到 0（或已过）→ 表盘变红，是"该收尾了"的信号 */
  const over = isTimerOver(todo, runningSeconds)
  const canTime = canRunTimer(todo)

  return (
    <Card
      className={cn(
        'flex items-center gap-3 px-3 py-2.5 transition-colors',
        // 已完成的弱化：底色退一档 + 文字降级。**仍然可见** ——
        // 做完的事突然消失会让人怀疑"我是不是删错了"。
        view.muted && 'bg-paper-sunken opacity-70',
      )}
    >
      <button
        type="button"
        onClick={onToggle}
        disabled={busy}
        aria-pressed={todo.completed}
        aria-label={todo.completed ? '标记为未完成' : '标记为已完成'}
        className={cn(
          'flex h-5 w-5 shrink-0 items-center justify-center rounded-full border transition-colors',
          todo.completed
            ? 'border-moss bg-moss text-white'
            : 'border-line-strong bg-surface-1 hover:border-moss-line',
          busy && 'cursor-wait opacity-60',
        )}
      >
        {todo.completed && <CheckMark checked />}
      </button>

      {editing ? (
        <input
          autoFocus
          value={editDraft}
          onChange={(e) => onEditDraft(e.target.value)}
          onBlur={onCommitEdit}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
              e.preventDefault()
              onCommitEdit()
            }
            if (e.key === 'Escape') {
              e.preventDefault()
              onCancelEdit()
            }
          }}
          aria-label="编辑待办标题"
          className="w-full rounded-lg border border-line-strong bg-surface-1 px-2 py-1 text-sm text-ink-1 outline-none focus:border-moss-line"
        />
      ) : (
        <button
          type="button"
          onClick={onStartEdit}
          title="点一下改标题"
          className={cn(
            'min-w-0 flex-1 truncate text-left text-sm',
            view.muted ? 'text-ink-4 line-through' : 'text-ink-1',
          )}
        >
          {view.title}
        </button>
      )}

      {view.due && (
        <span
          className={cn(
            'flex shrink-0 items-center gap-1 text-2xs',
            late ? 'text-brick-ink' : view.dueToday ? 'text-moss-ink' : 'text-ink-4',
          )}
        >
          <IconClock size={12} />
          {view.due}
        </span>
      )}

      {/* 计时条：只有设了计时的任务才出现 ✓ 平时不占地方 */}
      {timer !== null && (
        <span
          className={cn(
            'flex shrink-0 items-center gap-1.5 rounded-full border px-2 py-0.5 text-2xs',
            over
              ? 'border-brick-line bg-brick-soft text-brick-ink'
              : running
                ? 'border-moss-line bg-moss-soft text-moss-ink'
                : 'border-line text-ink-3',
          )}
        >
          <button
            type="button"
            onClick={onToggleTimer}
            disabled={!canTime}
            aria-label={running ? '暂停计时' : '开始计时'}
            className="flex items-center gap-1 disabled:opacity-40"
          >
            {running ? <IconPause size={11} /> : <IconClock size={11} />}
            <span className="tabular-nums">{timer}</span>
          </button>
        </span>
      )}

      {!editing && (
        <Button
          size="sm"
          variant="quiet"
          disabled={busy}
          icon={<IconTrash size={14} />}
          aria-label={`删除「${todo.title}」`}
          onClick={onDelete}
        >
          <span className="sr-only">删除</span>
        </Button>
      )}
    </Card>
  )
}
