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

import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  createTodo,
  deleteTodo,
  listTodos,
  updateTodo,
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
  IconPlus,
  IconTrash,
  Modal,
  Skeleton,
  cn,
  useToast,
} from '../../ui'
import {
  canSubmit,
  cleanTitle,
  describe,
  isOverdue,
  listState,
  remainingLabel,
  sortTodos,
  toISODate,
} from './todoState'

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

  // ── 新增
  const handleAdd = useCallback(async () => {
    const title = cleanTitle(draft)
    if (!title) return
    setAdding(true)
    try {
      const created = await createTodo(title)
      apply([created, ...items])
      setDraft('')
    } catch (err) {
      toast.error('没能加进去', messageOf(err))
    } finally {
      setAdding(false)
    }
  }, [draft, items, apply, toast])

  // ── 勾选 / 取消
  const handleToggle = useCallback(
    async (todo: TodoItem) => {
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
    [items, apply, toast],
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
                onEditDraft={setEditDraft}
                onToggle={() => void handleToggle(todo)}
                onStartEdit={() => startEdit(todo)}
                onCommitEdit={() => void commitEdit()}
                onCancelEdit={() => setEditingId(null)}
                onDelete={() => setPendingDelete(todo)}
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
  onEditDraft,
  onToggle,
  onStartEdit,
  onCommitEdit,
  onCancelEdit,
  onDelete,
}: {
  todo: TodoItem
  today: string
  busy: boolean
  editing: boolean
  editDraft: string
  onEditDraft: (value: string) => void
  onToggle: () => void
  onStartEdit: () => void
  onCommitEdit: () => void
  onCancelEdit: () => void
  onDelete: () => void
}) {
  const view = describe(todo, today)
  const late = isOverdue(todo, today)

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
