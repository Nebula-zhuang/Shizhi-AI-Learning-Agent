/**
 * 待办清单的接口封装。
 *
 * ## PATCH 的类型为什么写成 `?: string | null`
 *
 * 后端靠"字段**有没有出现**在请求体里"来区分两件事：
 *
 * | 前端给的 | JSON 里 | 后端行为 |
 * |---|---|---|
 * | 键**不出现** | `{}` | 这一项不动 |
 * | `due_date: null` | `{"due_date":null}` | **清空**截止日 |
 * | `due_date: undefined` | `{}`（被 `JSON.stringify` 丢掉） | 这一项不动 |
 *
 * 所以「清空」必须用 `null`，**不能用 `undefined`** ——
 * `JSON.stringify` 会把 `undefined` 的键整个删掉，于是"清空"静默变成"不动"。
 * 类型里保留 `| null` 就是为了让"传 null"这件事在调用方显式可见。
 */

import { deleteJson, getJson, patchJson, postJson } from './http'

export interface TodoItem {
  id: number
  title: string
  completed: boolean
  /** `YYYY-MM-DD`；null = 没有截止日 */
  due_date: string | null
  created_at: string
  updated_at: string
}

export interface TodoList {
  items: TodoItem[]
  total: number
}

export interface TodoListQuery {
  /** 不传 = 全部；true = 只看已完成；false = 只看未完成 */
  completed?: boolean
  limit?: number
  offset?: number
}

/** 新建。`due_date` 省略或给 null 都表示"没有截止日"。 */
export function createTodo(title: string, dueDate?: string | null): Promise<TodoItem> {
  return postJson<TodoItem>('/api/todos', {
    title,
    ...(dueDate ? { due_date: dueDate } : {}),
  })
}

export function listTodos(query: TodoListQuery = {}): Promise<TodoList> {
  const params = new URLSearchParams()
  if (query.completed !== undefined) params.set('completed', String(query.completed))
  if (query.limit !== undefined) params.set('limit', String(query.limit))
  if (query.offset !== undefined) params.set('offset', String(query.offset))
  const suffix = params.toString()
  return getJson<TodoList>(`/api/todos${suffix ? `?${suffix}` : ''}`)
}

/**
 * 改一条。**只把要改的键放进 `changes`**：
 * 没放进来的键后端不会动（包括 `due_date`）。
 */
export interface TodoChanges {
  title?: string
  completed?: boolean
  /** 显式传 `null` 才是**清空**；不传这个键 = 不动 */
  due_date?: string | null
}

export function updateTodo(id: number, changes: TodoChanges): Promise<TodoItem> {
  return patchJson<TodoItem>(`/api/todos/${id}`, changes)
}

export function deleteTodo(id: number): Promise<{ deleted: boolean; todo_id: number }> {
  return deleteJson<{ deleted: boolean; todo_id: number }>(`/api/todos/${id}`)
}
