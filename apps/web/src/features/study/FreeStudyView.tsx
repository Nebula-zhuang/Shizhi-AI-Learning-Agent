/**
 * 自由学习空间。
 *
 * ## 四区布局
 *
 *     ┌──────────┬────────────────────────┬──────────┐
 *     │ 对话历史  │      当前对话           │ 辅助信息  │
 *     │          │                        │（可收起） │
 *     │          ├────────────────────────┤          │
 *     │          │      底部输入框         │          │
 *     └──────────┴────────────────────────┴──────────┘
 *
 * ## 三条从设计上下文继承的规矩
 *
 * 1. **两种声音靠排版区分，不靠气泡颜色。**
 *    助教用 `.tutor-voice`（衬线、行高更大），学习者用 `.learner-voice`。
 *    这与 Tutor 页面是同一套语法 —— 同一个产品里"谁在说话"必须有一种表现方式。
 *
 * 2. **开发信息不进主界面。**
 *    用了哪些能力（检索/联网）、状态轨迹、降级原因，都收进右侧面板，
 *    默认**收起**。演示时干净，排障时不缺。
 *
 * 3. **措辞是人话，不是数据库口吻。**
 *    状态直接用后端给的原话（"正在翻你的资料…"），前端不自己拼指标。
 *    空态不是"暂无数据"，而是"想学什么，直接问我。"
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import {
  askQuestion,
  createConversation,
  deleteConversation,
  formatFileSize,
  getCapabilities,
  listConversations,
  listMessages,
  listSavedKnowledge,
  renameConversation,
  saveKnowledge,
  uploadAttachment,
  type AttachmentUpload,
  type ConversationSummary,
  type SavedKnowledgeItem,
  type StudyCapabilities,
  type StudyMessage,
  type ToolEvent,
  type TurnCitation,
  type TurnSource,
} from '../../api/study'
import { stripBlockMarkup, stripInlineMarkup } from '../../lib/plainText'
import { ProgressiveText, Reveal } from '../../motion/primitives'
import {
  Button,
  EmptyState,
  ErrorState,
  IconBookmark,
  IconClose,
  IconPlus,
  IconSpark,
  IconTrash,
  Skeleton,
  SuccessMark,
  Thinking,
  cn,
} from '../../ui'
import { useToast } from '../../ui/overlays'
import {
  formatSavedAt,
  hasMoreSaved,
  mergeSavedItems,
  saveButtonLabel,
  saveEligibility,
  saveFailureMessage,
  savedItemTitle,
  savedMessageIdsOf,
  statusOf,
} from './savedKnowledge'

/** 一行对话在界面上的样子 */
interface Turn {
  /**
   * 本地临时 id。
   *
   * ⚠️ 它**不是**服务端 id —— 流式生成时还没有服务端 id。
   * 服务端 id 在落库后由 `persisted` 事件送到 `messageId`，
   * 而 `key` 保持不变（它只是 React 的列表身份）。
   */
  key: string
  /**
   * 这条消息在服务端的 id。
   * 落库成功后才有；有了它才能"保存这一轮"（正文由服务端按 id 取）。
   */
  messageId?: number
  /**
   * 这条是**打开对话时从历史里读回来的**（不是本轮刚生成的）。
   *
   * 只用来决定正文用不用逐段显影：流式刚结束那一刻，正文其实还在原地，
   * 若此时切成 `ProgressiveText`，它会先隐藏再逐段浮现 ——
   * 用户看到的是"整段文字闪了一下"。历史消息不存在这个问题（本来就是新出现）。
   */
  fromHistory?: boolean
  role: 'user' | 'assistant'
  content: string
  sources: TurnSource[]
  citations: TurnCitation[]
  statusTrace: string[]
  /** 本轮 Agent 调过哪些工具。右侧面板据此展示「它做了什么」。 */
  toolEvents: ToolEvent[]
  /** 用户这条消息带的附件名（只用于展示） */
  attachmentNames: string[]
  /** 正在生成中 —— 用来显示光标与状态行 */
  streaming: boolean
}

export function FreeStudyView() {
  const toast = useToast()

  const [conversations, setConversations] = useState<ConversationSummary[]>([])
  const [activeId, setActiveId] = useState<number | null>(null)
  const [turns, setTurns] = useState<Turn[]>([])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  /** 后台现在的状态。**直接显示后端原话**，前端不加工。 */
  const [status, setStatus] = useState('')
  const [caps, setCaps] = useState<StudyCapabilities | null>(null)
  /** 右侧辅助面板默认收起 —— 主任务是"问"，不是"看面板" */
  const [panelOpen, setPanelOpen] = useState(false)
  const [loadingHistory, setLoadingHistory] = useState(true)
  /** 已选好、还没发出去的附件 */
  const [pending, setPending] = useState<AttachmentUpload[]>([])
  const [uploading, setUploading] = useState(false)
  const [dragging, setDragging] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  // ── 保存知识（Phase 3C）
  //
  // 已保存的列表是**跨对话**的（后端按 learner 隔离），所以它不属于任何一个
  // 对话 —— 组件挂载时读一次，保存成功后本地补一条，不必重拉。
  const [savedItems, setSavedItems] = useState<SavedKnowledgeItem[]>([])
  const [savedTotal, setSavedTotal] = useState(0)
  const [savedLoading, setSavedLoading] = useState(true)
  const [savedError, setSavedError] = useState('')
  /** 正在保存的那条消息 id —— 用来禁用按钮、挡住重复点击 */
  const [savingMessageId, setSavingMessageId] = useState<number | null>(null)
  const savedMessageIds = useMemo(() => savedMessageIdsOf(savedItems), [savedItems])

  const scrollRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const abortRef = useRef<AbortController | null>(null)

  // ── 首屏：对话列表 + 能力自检
  useEffect(() => {
    let alive = true
    void (async () => {
      try {
        const [list, capability] = await Promise.all([listConversations(), getCapabilities()])
        if (!alive) return
        setConversations(list.items)
        setCaps(capability)
      } catch {
        if (alive) toast.error('没法读取对话列表', '请确认学习伙伴还在运行。')
      } finally {
        if (alive) setLoadingHistory(false)
      }
    })()
    return () => {
      alive = false
    }
  }, [toast])

  // ── 已保存的知识：挂载时读一次
  const loadSaved = useCallback(async () => {
    setSavedLoading(true)
    setSavedError('')
    try {
      const page = await listSavedKnowledge(20, 0)
      setSavedItems(page.items)
      setSavedTotal(page.total)
    } catch (error) {
      // 读不出来**不影响对话** —— 面板里显示失败态即可，不弹 toast 打扰
      setSavedError(saveFailureMessage(statusOf(error), (error as Error).message))
    } finally {
      setSavedLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadSaved()
  }, [loadSaved])

  const loadMoreSaved = useCallback(async () => {
    setSavedLoading(true)
    setSavedError('')
    try {
      const page = await listSavedKnowledge(20, savedItems.length)
      setSavedItems((prev) => mergeSavedItems(prev, page.items))
      setSavedTotal(page.total)
    } catch (error) {
      setSavedError(saveFailureMessage(statusOf(error), (error as Error).message))
    } finally {
      setSavedLoading(false)
    }
  }, [savedItems.length])

  /**
   * 保存一条助手消息。
   *
   * ⚠️ **只把 messageId 发出去**，正文由服务端从那条消息里取 ——
   * 这是后端的硬约定（保存下来的东西将来会被检索、被引用）。
   */
  const handleSave = useCallback(
    async (messageId: number) => {
      setSavingMessageId(messageId)
      try {
        const created = await saveKnowledge(messageId)
        // 本地直接补进列表，不重拉 —— 重拉会让面板闪一下
        setSavedItems((prev) => mergeSavedItems([created], prev))
        setSavedTotal((total) => total + 1)
        toast.success('已保存', '在右侧「已保存的知识」里能找到它。')
      } catch (error) {
        toast.error('没能保存', saveFailureMessage(statusOf(error), (error as Error).message))
      } finally {
        setSavingMessageId(null)
      }
    },
    [toast],
  )

  // ── 新消息进来后滚到底部
  useEffect(() => {
    const node = scrollRef.current
    if (!node) return
    node.scrollTo({ top: node.scrollHeight, behavior: 'smooth' })
  }, [turns, status])

  const latestPanel = useMemo(() => {
    // 右侧面板展示**最后一条助手回复**用了什么 —— 对话在推进，面板跟着走
    for (let i = turns.length - 1; i >= 0; i--) {
      if (turns[i].role === 'assistant') return turns[i]
    }
    return null
  }, [turns])

  // ────────────────────────────────────────────────── 操作
  const openConversation = useCallback(
    async (id: number) => {
      setActiveId(id)
      setTurns([])
      try {
        const data = await listMessages(id)
        setTurns(
          data.items.map((m: StudyMessage) => ({
            key: `srv-${m.id}`,
            // ⚠️ 服务端 id 要**单独存进 `messageId`**，不能只藏在 key 里 ——
            // "保存这一轮"凭的是它（正文由服务端按 id 取），藏在字符串里没法用。
            messageId: m.id,
            fromHistory: true,
            role: m.role,
            content: m.content,
            sources: m.sources ?? [],
            citations: m.citations ?? [],
            statusTrace: m.status_trace ?? [],
            toolEvents: [],
            attachmentNames: (m.attachments ?? [])
              .map((a) => String((a as Record<string, unknown>).file_name ?? ''))
              .filter(Boolean),
            streaming: false,
          })),
        )
      } catch {
        toast.error('没法打开这个对话', '它可能已经被删掉了。')
      }
    },
    [toast],
  )

  /**
   * 收下一批文件。
   *
   * 四个入口（点选 / 粘贴 / 拖拽 / 以后可能的分享）都汇到这里，
   * 上传逻辑只有一份 —— 否则"粘贴能传、拖拽不行"这类不一致迟早出现。
   */
  const acceptFiles = useCallback(
    async (files: FileList | File[]) => {
      const list = [...files]
      if (list.length === 0) return
      setUploading(true)
      try {
        // 串行上传：并发上传在弱网下容易一起超时，而且用户也看不出进度
        for (const file of list) {
          try {
            const uploaded = await uploadAttachment(file)
            setPending((prev) =>
              prev.some((item) => item.document_id === uploaded.document_id)
                ? prev
                : [...prev, uploaded],
            )
          } catch (error) {
            toast.error(`「${file.name}」没传上去`, (error as Error).message)
          }
        }
      } finally {
        setUploading(false)
      }
    },
    [toast],
  )

  const startNewConversation = useCallback(async () => {
    try {
      const created = await createConversation()
      setConversations((prev) => [created, ...prev])
      setActiveId(created.id)
      setTurns([])
      setDraft('')
      inputRef.current?.focus()
    } catch {
      toast.error('没法新建对话', '请稍后再试。')
    }
  }, [toast])

  const removeConversation = useCallback(
    async (id: number) => {
      try {
        await deleteConversation(id)
        setConversations((prev) => prev.filter((c) => c.id !== id))
        if (activeId === id) {
          setActiveId(null)
          setTurns([])
        }
      } catch {
        toast.error('没法删除这个对话', '请稍后再试。')
      }
    },
    [activeId, toast],
  )

  const rename = useCallback(
    async (id: number, title: string) => {
      try {
        const updated = await renameConversation(id, title)
        setConversations((prev) => prev.map((c) => (c.id === id ? updated : c)))
      } catch {
        toast.error('改名没成功', '请稍后再试。')
      }
    },
    [toast],
  )

  const submit = useCallback(async () => {
    const question = draft.trim()
    // 允许"只发一张图、不写字" —— 用户常常就是想让助教看看这个
    if (busy || uploading) return
    if (!question && pending.length === 0) return

    // 还没有对话就先建一个 —— 用户不需要先点"新建"
    let id = activeId
    if (id === null) {
      try {
        const created = await createConversation()
        id = created.id
        setConversations((prev) => [created, ...prev])
        setActiveId(created.id)
      } catch {
        toast.error('没法开始对话', '请稍后再试。')
        return
      }
    }

    // 已选好的附件转成 document_ids —— 它们上传时就落库了，
    // 提问只是把 id 带上，不用再传一次文件
    const attachmentIds = pending.map((item) => item.document_id)
    const attachmentNames = pending.map((item) => item.file_name)
    // 只发了图没写字时补一句 —— 后端不接受空问题，
    // 而且这句默认话术正好让"看图"有明确的意图
    const asked = question || (attachmentNames.length ? '帮我看看这个' : '')

    const userKey = `local-u-${Date.now()}`
    const assistantKey = `local-a-${Date.now()}`
    setDraft('')
    setPending([])
    setBusy(true)
    setStatus(attachmentIds.length ? '正在看你的附件…' : '正在理解你的问题…')
    setTurns((prev) => [
      ...prev,
      {
        key: userKey,
        role: 'user',
        content: question,
        sources: [],
        citations: [],
        statusTrace: [],
        toolEvents: [],
        attachmentNames,
        streaming: false,
      },
      {
        key: assistantKey,
        role: 'assistant',
        content: '',
        sources: [],
        citations: [],
        statusTrace: [],
        toolEvents: [],
        attachmentNames: [],
        streaming: true,
      },
    ])

    const controller = new AbortController()
    abortRef.current = controller

    const patch = (updater: (turn: Turn) => Turn) =>
      setTurns((prev) => prev.map((t) => (t.key === assistantKey ? updater(t) : t)))

    await askQuestion(id, asked, attachmentIds, {
      signal: controller.signal,
      onStatus: (text) => {
        setStatus(text)
        patch((t) => ({ ...t, statusTrace: [...t.statusTrace, text] }))
      },
      onDelta: (text) => patch((t) => ({ ...t, content: t.content + text })),
      onSource: (source) => patch((t) => ({ ...t, sources: [...t.sources, source] })),
      onToolStart: (event) =>
        patch((t) => ({ ...t, toolEvents: [...t.toolEvents, event] })),
      onToolResult: (event) =>
        patch((t) => {
          // 把结果合并到同名的最后一条上 —— 界面要看到「调了什么 + 结果如何」一条完整记录
          const index = [...t.toolEvents].reverse().findIndex((e) => e.tool === event.tool && e.ok === undefined)
          if (index < 0) return { ...t, toolEvents: [...t.toolEvents, event] }
          const at = t.toolEvents.length - 1 - index
          const next = [...t.toolEvents]
          next[at] = { ...next[at], ...event }
          return { ...t, toolEvents: next }
        }),
      onCitation: (citation) =>
        patch((t) => {
          // 同一条引用可能因为重连被推两次，去重后不重复列
          if (citation.url && t.citations.some((c) => c.url === citation.url)) return t
          return { ...t, citations: [...t.citations, citation] }
        }),
      onDone: () => {
        patch((t) => ({ ...t, streaming: false }))
        setStatus('')
        // 消息条数变了，列表要跟着更新（标题也可能被后端自动生成）
        void listConversations().then((data) => setConversations(data.items))
      },
      // 落库成功后后端才推这一帧。拿到它，这条回答才**可以被保存** ——
      // `done` 到达时 id 还不存在（后端是先推 done、再落库）。
      onPersisted: (messageId) => patch((t) => ({ ...t, messageId })),
      onError: (message) => {
        patch((t) => ({
          ...t,
          streaming: false,
          content: t.content || message,
        }))
        setStatus('')
        // 问题已经发出去了，刷新列表让标题等元信息同步
        void listConversations().then((data) => setConversations(data.items))
      },
    })

    abortRef.current = null
    setBusy(false)
    inputRef.current?.focus()
  }, [activeId, busy, draft, pending, toast])

  const stop = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    setBusy(false)
    setStatus('')
    setTurns((prev) => prev.map((t) => (t.streaming ? { ...t, streaming: false } : t)))
  }, [])

  // ────────────────────────────────────────────────── 渲染
  return (
    <div className="flex min-h-0 flex-1 gap-4">
      {/* ═══════════════════════════════ 左：对话历史 */}
      <aside className="flex min-h-0 w-[218px] shrink-0 flex-col gap-2">
        <Button
          variant="primary"
          size="sm"
          onClick={() => void startNewConversation()}
          className="w-full"
        >
          新建学习对话
        </Button>

        <div className="min-h-0 flex-1 space-y-1 overflow-y-auto pr-1">
          {loadingHistory && (
            /* 骨架屏而不是"正在读…"文字：文字只说明"在等"，
               骨架还把**等出来的是什么东西**先摆出来（这一栏放的是对话行）——
               加载完的瞬间版面不跳。`aria-hidden` 由 Skeleton 自己带。 */
            <div className="space-y-2 px-2.5 py-2">
              <Skeleton width="72%" height={11} />
              <Skeleton width="46%" height={9} />
              <Skeleton className="mt-3" width="64%" height={11} />
              <Skeleton width="38%" height={9} />
              <Skeleton className="mt-3" width="78%" height={11} />
              <Skeleton width="52%" height={9} />
            </div>
          )}
          {!loadingHistory && conversations.length === 0 && (
            <p className="px-2 py-3 text-2xs leading-relaxed text-ink-4">
              还没有对话。问第一个问题就会出现在这里。
            </p>
          )}
          {conversations.map((conversation) => (
            <ConversationRow
              key={conversation.id}
              conversation={conversation}
              active={conversation.id === activeId}
              onOpen={() => void openConversation(conversation.id)}
              onRename={(title) => void rename(conversation.id, title)}
              onDelete={() => void removeConversation(conversation.id)}
            />
          ))}
        </div>
      </aside>

      {/* ═══════════════════════════════ 中：对话 + 输入 */}
      <main className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto pb-4">
          {turns.length === 0 ? (
            <StudyEmptyState
              onPick={(sample) => {
                setDraft(sample)
                inputRef.current?.focus()
              }}
            />
          ) : (
            <div className="space-y-6">
              {turns.map((turn) => (
                /* 新消息淡入 + 微升。**只在挂载时播一次** ——
                   流式回答每来一段都会重渲染，但 `Reveal` 的 `animate`
                   不会重放，所以正文增长时不会有任何"抖一下"。
                   `Reveal` 内部走 `useMotion()`，已尊重 prefers-reduced-motion。 */
                <Reveal key={turn.key}>
                  <TurnBlock
                    turn={turn}
                    savedMessageIds={savedMessageIds}
                    savingMessageId={savingMessageId}
                    onSave={(messageId) => void handleSave(messageId)}
                  />
                </Reveal>
              ))}
            </div>
          )}
        </div>

        <Composer
          value={draft}
          busy={busy}
          status={status}
          caps={caps}
          pending={pending}
          uploading={uploading}
          dragging={dragging}
          inputRef={inputRef}
          fileInputRef={fileInputRef}
          onChange={setDraft}
          onSubmit={() => void submit()}
          onStop={stop}
          onTogglePanel={() => setPanelOpen((v) => !v)}
          panelOpen={panelOpen}
          onPickFiles={() => fileInputRef.current?.click()}
          onFiles={(files) => void acceptFiles(files)}
          onRemoveAttachment={(id) =>
            setPending((prev) => prev.filter((item) => item.document_id !== id))
          }
          onDraggingChange={setDragging}
        />
      </main>

      {/* ═══════════════════════════════ 右：辅助信息（默认收起） */}
      {panelOpen && (
        <aside className="flex min-h-0 w-[260px] shrink-0 flex-col gap-3 overflow-y-auto">
          <section className="rounded-xl border border-line bg-paper-raised p-3">
            <p className="mb-2 text-xs font-medium text-ink-2">这一轮参考了什么</p>
            <SourceList turn={latestPanel} />
          </section>

          <section className="rounded-xl border border-line bg-paper-raised p-3">
            <p className="mb-2 text-xs font-medium text-ink-2">它做了什么</p>
            <ToolTrail turn={latestPanel} />
          </section>

          {/* 已保存的知识是**跨对话**的，所以它不跟 `latestPanel` 走 ——
              放在这里只是借右侧这块地方，与上面两节的性质不同。 */}
          <section className="rounded-xl border border-line bg-paper-raised p-3">
            <p className="mb-2 text-xs font-medium text-ink-2">
              已保存的知识
              {savedTotal > 0 && <span className="ml-1 text-ink-4">（{savedTotal}）</span>}
            </p>
            <SavedKnowledgePanel
              items={savedItems}
              total={savedTotal}
              loading={savedLoading}
              error={savedError}
              onRetry={() => void loadSaved()}
              onLoadMore={() => void loadMoreSaved()}
            />
          </section>
        </aside>
      )}
    </div>
  )
}

/* ══════════════════════════════════════════════════════════ 子组件 */

function ConversationRow({
  conversation,
  active,
  onOpen,
  onRename,
  onDelete,
}: {
  conversation: ConversationSummary
  active: boolean
  onOpen: () => void
  onRename: (title: string) => void
  onDelete: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(conversation.title)

  const commit = () => {
    setEditing(false)
    const next = draft.trim()
    if (next && next !== conversation.title) onRename(next)
    else setDraft(conversation.title)
  }

  if (editing) {
    return (
      <input
        autoFocus
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter') commit()
          if (e.key === 'Escape') {
            setDraft(conversation.title)
            setEditing(false)
          }
        }}
        className="w-full rounded-lg border border-moss-line bg-surface-1 px-2.5 py-2 text-xs text-ink-1 outline-none"
      />
    )
  }

  return (
    <div
      className={cn(
        'group relative rounded-lg px-2.5 py-2 transition-colors',
        active ? 'bg-moss-soft' : 'hover:bg-paper-sunken',
      )}
    >
      <button type="button" onClick={onOpen} className="block w-full text-left">
        <span
          className={cn(
            'block truncate text-xs',
            active ? 'font-medium text-moss-ink' : 'text-ink-2',
          )}
        >
          {conversation.title || '新的学习对话'}
        </span>
        <span className="mt-0.5 block text-2xs text-ink-4">
          {conversation.message_count > 0
            ? `聊了 ${conversation.message_count} 条`
            : '还没开始'}
        </span>
      </button>
      {/* 操作按钮悬停才出现 —— 常驻会让整列显得像管理列表 */}
      <span className="absolute right-1 top-1 hidden gap-0.5 group-hover:flex">
        <button
          type="button"
          onClick={() => setEditing(true)}
          title="改名"
          className="meta rounded px-1 py-0.5 text-ink-4 hover:bg-paper hover:text-ink-1"
        >
          改
        </button>
        <button
          type="button"
          onClick={onDelete}
          title="删除这个对话"
          className="meta rounded px-1 py-0.5 text-ink-4 hover:bg-brick-soft hover:text-brick"
        >
          <IconTrash size={11} />
        </button>
      </span>
    </div>
  )
}

function TurnBlock({
  turn,
  savedMessageIds,
  savingMessageId,
  onSave,
}: {
  turn: Turn
  savedMessageIds: ReadonlySet<number>
  savingMessageId: number | null
  onSave: (messageId: number) => void
}) {
  if (turn.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="learner-voice max-w-[80%] rounded-xl bg-paper-sunken px-3.5 py-2.5">
          {/* 附件名放在正文上方 —— 让用户看到"我发了什么"，而不只是文字 */}
          {turn.attachmentNames.length > 0 && (
            <ul className="mb-1.5 space-y-0.5 border-b border-line pb-1.5">
              {turn.attachmentNames.map((name) => (
                <li key={name} className="text-2xs text-ink-3">
                  附件：{name}
                </li>
              ))}
            </ul>
          )}
          {stripInlineMarkup(turn.content)}
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-2">
      <AssistantText turn={turn} />

      {/* 引用：有来源才出现，没有就不占位置 */}
      {turn.citations.length > 0 && (
        <ul className="space-y-1 border-l-2 border-line pl-3">
          {turn.citations.map((citation, index) => (
            <li key={`${index}-${citation.url ?? citation.title}`} className="text-2xs">
              <a
                href={citation.url}
                target="_blank"
                rel="noreferrer noopener"
                className="text-moss-ink hover:underline"
              >
                {citation.title || citation.url}
              </a>
              {/* **实际走了哪条通道，要看得见。**
                  它是运维信号：一直悄悄回退的系统，
                  和一直正常工作的系统，在界面上应该长得不一样。 */}
              <ProviderTag citation={citation} />
            </li>
          ))}
        </ul>
      )}

      <SaveAction
        turn={turn}
        savedMessageIds={savedMessageIds}
        savingMessageId={savingMessageId}
        onSave={onSave}
      />
    </div>
  )
}

/**
 * 助手的正文。**三种形态，各有各的道理**：
 *
 * 1. **还在等第一个字** → `Thinking`（三点呼吸）。空白等最先几秒最难受，
 *    这里给一个"确实在动"的信号，而不是让用户盯着一片空。
 * 2. **正在流式输出** → 纯文本 + 光标。**正文的真增量本身就是动画**，
 *    再叠一层打字机/显影只会变成两次动画打架（这也是 Tutor 的做法：
 *    「正文逐字流入（真实增量，不是打字机动画）」）。
 * 3. **打开对话时读回来的多段回答** → `ProgressiveText` 按段依次显影。
 *    这时文字是**新出现**的，逐段浮现才有意义（组件注释明说"不是打字机"）。
 *
 * ⚠️ 第 3 种**只用于历史消息**（`turn.fromHistory`）。流式刚结束的那条不用 ——
 * 正文当时还在原地，切成 `ProgressiveText` 会先隐藏再逐段浮现，
 * 用户看到的是一次"闪一下"。单段回答也仍走纯文本：一段话"依次显影"
 * 等于整段淡入，白跑一层动画。
 *
 * ⚠️ 正文一律**纯文本**：模型写的 `**重点**` 在进界面之前就摘掉。
 * FreeStudy 刻意不接 Markdown 渲染器（见 `docs/13` 的既有决定）。
 */
function AssistantText({ turn }: { turn: Turn }) {
  const text = stripBlockMarkup(turn.content)

  if (turn.streaming && !text.trim()) {
    return <Thinking label="正在组织回答…" />
  }

  const paragraphs = text.split(/\n{2,}/).filter((part) => part.trim())

  if (turn.fromHistory && !turn.streaming && paragraphs.length > 1) {
    return (
      <ProgressiveText
        className="tutor-voice"
        lines={paragraphs}
        render={(line) => <p className="whitespace-pre-wrap">{line}</p>}
      />
    )
  }

  return (
    <div className="tutor-voice whitespace-pre-wrap">
      {text}
      {turn.streaming && (
        <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-moss align-text-bottom" />
      )}
    </div>
  )
}

/**
 * 「保存知识」入口。
 *
 * 三条规矩（都在 `savedKnowledge.ts` 里，这里只负责画）：
 *
 * 1. **没有服务端 id 就不显示可点的按钮。** 保存凭的是 `messageId`，
 *    让用户点一个注定 404 的按钮，比不给按钮更糟。
 * 2. **已保存是一个状态**，不是还能再点的动作 —— 避免重复保存。
 * 3. 保存中禁用，挡住连点。
 */
function SaveAction({
  turn,
  savedMessageIds,
  savingMessageId,
  onSave,
}: {
  turn: Turn
  savedMessageIds: ReadonlySet<number>
  savingMessageId: number | null
  onSave: (messageId: number) => void
}) {
  const eligibility = saveEligibility(turn, savedMessageIds)
  // 用户消息、流式中、落库失败的空回答……都不该出现这一行
  if (turn.role !== 'assistant' || turn.streaming) return null
  if (eligibility.reason === 'no-message-id' || eligibility.reason === 'empty') return null

  const saving = savingMessageId !== null && savingMessageId === turn.messageId
  const done = eligibility.reason === 'already-saved'

  // 存好了就**不再是一个按钮**：换成描边勾 + 「已保存」。
  // 按钮留在那儿（哪怕是灰的）会一直勾人去点第二次。
  if (done) {
    return (
      <div className="flex items-center gap-2">
        <SuccessMark label="已保存" />
      </div>
    )
  }

  return (
    <div className="flex items-center gap-2">
      <button
        type="button"
        disabled={!eligibility.canSave || saving}
        title={eligibility.hint || '把这一轮存下来，之后可以跨对话搜到'}
        onClick={() => {
          if (turn.messageId === undefined) return
          onSave(turn.messageId)
        }}
        className={cn(
          'meta inline-flex items-center gap-1 rounded px-1.5 py-0.5 transition-colors',
          'text-ink-4 hover:bg-paper-sunken hover:text-ink-1 disabled:cursor-not-allowed disabled:opacity-50',
        )}
      >
        <IconBookmark size={11} />
        {saveButtonLabel(eligibility, saving)}
      </button>
    </div>
  )
}

/**
 * 一条引用的「经由什么」标注。
 *
 * 两个信息都要给：
 *   · provider —— 实际用的通道（MCP / 直连）
 *   · fellBack —— 有没有回退过，以及为什么
 *
 * 只显示 provider 而不显示回退，会让人以为首选通道一直是好的。
 */
function ProviderTag({ citation }: { citation: TurnCitation }) {
  if (citation.fellBack) {
    return (
      <span className="ml-1 text-sienna-ink" title={citation.fallbackReason || '首选通道没连上'}>
        （备用通道）
      </span>
    )
  }
  if (citation.provider === 'mcp') {
    return (
      <span className="ml-1 text-ink-4" title={citation.providerDetail || 'MCP'}>
        经由 MCP
      </span>
    )
  }
  if (citation.simulated) {
    return <span className="ml-1 text-ink-4">（离线模拟）</span>
  }
  return null
}

/** 工具 → 给用户看的说法。**不暴露内部术语。** */
const TOOL_LABEL: Record<string, string> = {
  retrieve_knowledge: '翻了你的资料',
  document_analysis: '在那份资料里找',
  web_search: '联网查了',
  image_analysis: '看了这张图',
}

/**
 * 右侧面板的「它做了什么」。
 *
 * 用**工具调用序列**而不是状态文本 —— 状态是过程的散文，
 * 而这里要回答的是「它到底查了什么、成功了没有」。
 */
function ToolTrail({ turn }: { turn: Turn | null }) {
  if (!turn || turn.toolEvents.length === 0) {
    return (
      <div className="text-2xs leading-relaxed text-ink-4">
        {turn?.statusTrace.length ? (
          turn.statusTrace.map((line, index) => (
            <span key={index} className="block">
              {line}
            </span>
          ))
        ) : (
          <span>这一轮没有用到外部资料 —— 它是凭已有知识回答的。</span>
        )}
      </div>
    )
  }

  return (
    <ol className="space-y-1.5">
      {turn.toolEvents.map((event, index) => (
        <li key={`${index}-${event.tool}`} className="text-2xs leading-relaxed">
          <span className={event.ok === false ? 'text-brick' : 'text-ink-2'}>
            {index + 1}. {TOOL_LABEL[event.tool] ?? '查了一下'}
          </span>
          {event.ok === false && <span className="ml-1 text-brick">（没成功）</span>}
          {event.argumentsSummary && (
            <span className="mt-0.5 block text-ink-4">{event.argumentsSummary}</span>
          )}
        </li>
      ))}
    </ol>
  )
}

function SourceList({ turn }: { turn: Turn | null }) {
  if (!turn || turn.sources.length === 0) {
    return (
      <p className="text-2xs leading-relaxed text-ink-4">
        这一轮没有用到外部资料 —— 它是凭已有知识回答的。
      </p>
    )
  }
  return (
    <ul className="space-y-2">
      {turn.sources.map((source, index) => (
        <li key={index} className="text-2xs leading-relaxed text-ink-3">
          {source.kind === 'knowledge_base' ? (
            <>
              <span className="text-ink-2">
                {source.file_name}
                {source.page ? ` 第 ${source.page} 页` : ''}
              </span>
              {source.preview && (
                <span className="mt-0.5 block text-ink-4">{source.preview}…</span>
              )}
            </>
          ) : (
            <span className="text-ink-2">
              联网查到 {source.count} 条参考
              {source.simulated ? '（离线模拟）' : ''}
            </span>
          )}
        </li>
      ))}
    </ul>
  )
}

/**
 * 右侧面板的「已保存的知识」。
 *
 * 与上面两节的**性质不同**：`SourceList` / `ToolTrail` 讲的是"这一轮"，
 * 而这里是**跨对话**的一个列表（后端按 learner 隔离）。
 *
 * 三个状态都复用现有组件，不另造轮子；**失败时只占这一小块**，
 * 不影响对话本身 —— 读不出保存列表不是"这轮学不成"。
 */
function SavedKnowledgePanel({
  items,
  total,
  loading,
  error,
  onRetry,
  onLoadMore,
}: {
  items: SavedKnowledgeItem[]
  total: number
  loading: boolean
  error: string
  onRetry: () => void
  onLoadMore: () => void
}) {
  if (error) {
    return <ErrorState title="没读出已保存的知识" message={error} onRetry={onRetry} />
  }

  if (loading && items.length === 0) {
    return (
      <div className="space-y-2" aria-hidden="true">
        <Skeleton width="86%" height={11} />
        <Skeleton width="62%" height={11} />
        <Skeleton width="74%" height={11} />
      </div>
    )
  }

  if (items.length === 0) {
    return (
      <p className="text-2xs leading-relaxed text-ink-4">
        还没有保存过。在任意一条回答下面点「保存知识」，它就会出现在这里 ——
        以后再问相关的东西，我会想起你存过。
      </p>
    )
  }

  return (
    <div className="space-y-2.5">
      <ul className="space-y-2.5">
        {items.map((item) => (
          <li key={item.id} className="text-2xs leading-relaxed">
            <span className="block text-ink-2">{savedItemTitle(item)}</span>
            <span className="mt-0.5 block text-ink-4">
              {formatSavedAt(item.created_at)}
              {item.tags && item.tags.length > 0 && ` ｜${item.tags.join('、')}`}
            </span>
          </li>
        ))}
      </ul>

      {hasMoreSaved(items.length, total) && (
        <button
          type="button"
          disabled={loading}
          onClick={onLoadMore}
          className="meta rounded px-1 py-0.5 text-ink-4 transition-colors hover:bg-paper-sunken hover:text-ink-1 disabled:opacity-50"
        >
          {loading ? '正在读…' : `还有 ${total - items.length} 条`}
        </button>
      )}
    </div>
  )
}

function StudyEmptyState({ onPick }: { onPick: (sample: string) => void }) {
  const samples = [
    '什么是 JVM？',
    '帮我解释一下这段代码',
    '根据我的资料讲讲第三章',
  ]
  return (
    <div className="flex h-full flex-col items-center justify-center gap-5 text-center">
      <EmptyState
        icon={<IconSpark size={18} />}
        title="想学什么，直接问我。"
        description="不用先选课程或知识点。你也可以上传资料、发张截图，我会自己决定怎么答。"
      />
      <div className="flex flex-wrap justify-center gap-2">
        {samples.map((sample) => (
          <button
            key={sample}
            type="button"
            onClick={() => onPick(sample)}
            className="rounded-full border border-line bg-paper-raised px-3 py-1.5 text-2xs text-ink-3 transition-colors hover:border-moss-line hover:text-moss-ink"
          >
            {sample}
          </button>
        ))}
      </div>
    </div>
  )
}

function Composer({
  value,
  busy,
  status,
  caps,
  pending,
  uploading,
  dragging,
  inputRef,
  fileInputRef,
  onChange,
  onSubmit,
  onStop,
  onTogglePanel,
  panelOpen,
  onPickFiles,
  onFiles,
  onRemoveAttachment,
  onDraggingChange,
}: {
  value: string
  busy: boolean
  status: string
  caps: StudyCapabilities | null
  pending: AttachmentUpload[]
  uploading: boolean
  dragging: boolean
  inputRef: React.RefObject<HTMLTextAreaElement>
  fileInputRef: React.RefObject<HTMLInputElement>
  onChange: (value: string) => void
  onSubmit: () => void
  onStop: () => void
  onTogglePanel: () => void
  panelOpen: boolean
  onPickFiles: () => void
  onFiles: (files: FileList | File[]) => void
  onRemoveAttachment: (documentId: number) => void
  onDraggingChange: (dragging: boolean) => void
}) {
  // 有附件就能发（用户可以只发一张图、不写字）
  const canSubmit = (value.trim().length > 0 || pending.length > 0) && !busy && !uploading

  return (
    <div className="shrink-0 space-y-2 border-t border-line pt-3">
      {/* 状态行：**后台真的在做那件事时才出现**，不是假进度 */}
      <div className="flex min-h-[18px] items-center justify-between gap-2">
        <span className="text-2xs text-moss-ink">{busy ? status : ''}</span>
        <div className="flex items-center gap-2">
          {caps && !caps.web_search_available && (
            <span className="text-2xs text-ink-4" title={caps.web_search_note}>
              暂时不能联网
            </span>
          )}
          <button
            type="button"
            onClick={onTogglePanel}
            className="meta rounded px-1.5 py-0.5 text-ink-4 transition-colors hover:bg-paper-sunken hover:text-ink-1"
          >
            {panelOpen ? '收起参考 ›' : '‹ 参考'}
          </button>
        </div>
      </div>

      {/* 已选好的附件。**先显示再用** —— 用户要能在发之前撤掉选错的那个。 */}
      {pending.length > 0 && (
        <ul className="flex flex-wrap gap-1.5">
          {pending.map((item) => (
            <li
              key={item.document_id}
              className="flex items-center gap-1.5 rounded-lg border border-line bg-paper-raised px-2 py-1 text-2xs"
            >
              <span className="text-ink-2">{item.file_name}</span>
              <span className="text-ink-4">{formatFileSize(item.file_size)}</span>
              <button
                type="button"
                onClick={() => onRemoveAttachment(item.document_id)}
                title="移除"
                className="text-ink-4 transition-colors hover:text-brick"
              >
                <IconClose size={11} />
              </button>
            </li>
          ))}
        </ul>
      )}

      {uploading && <p className="text-2xs text-ink-4">正在上传附件…</p>}

      {/* 隐藏的文件选择器 —— `＋` 按钮点它 */}
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept="image/*,.pdf,.txt,.md,.docx,.pptx"
        className="hidden"
        onChange={(e) => {
          if (e.target.files) onFiles(e.target.files)
          // 清空 value，否则连续选同一个文件不会触发 change
          e.target.value = ''
        }}
      />

      <div
        onDragOver={(e) => {
          e.preventDefault()
          onDraggingChange(true)
        }}
        onDragLeave={() => onDraggingChange(false)}
        onDrop={(e) => {
          e.preventDefault()
          onDraggingChange(false)
          if (e.dataTransfer?.files?.length) onFiles(e.dataTransfer.files)
        }}
        className={cn(
          'flex items-end gap-2 rounded-xl border bg-surface-1 p-2 transition-colors',
          dragging ? 'border-moss-line bg-moss-soft' : 'border-line-strong',
        )}
      >
        <button
          type="button"
          onClick={onPickFiles}
          title="添加图片或文件"
          className="mb-1.5 shrink-0 rounded-lg px-2 py-1 text-ink-4 transition-colors hover:bg-paper-sunken hover:text-ink-1"
        >
          <IconPlus size={15} />
        </button>
        <textarea
          ref={inputRef}
          value={value}
          rows={2}
          placeholder={dragging ? '松手就能添加' : '问任何问题…也可以直接贴一张图'}
          onChange={(e) => onChange(e.target.value)}
          onPaste={(e) => {
            // **粘贴截图**是最常用的入口 —— 他刚截了图，不想再存一遍
            const files = [...(e.clipboardData?.items ?? [])]
              .filter((item) => item.kind === 'file')
              .map((item) => item.getAsFile())
              .filter((file): file is File => file !== null)
            if (files.length > 0) {
              e.preventDefault()
              onFiles(files)
            }
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault()
              onSubmit()
            }
          }}
          className="max-h-40 min-h-[44px] flex-1 resize-none bg-transparent px-1.5 py-1.5 text-sm text-ink-1 outline-none placeholder:text-ink-4"
        />
        {busy ? (
          <Button variant="quiet" size="sm" onClick={onStop}>
            停下
          </Button>
        ) : (
          <Button variant="primary" size="sm" disabled={!canSubmit} onClick={onSubmit}>
            发送
          </Button>
        )}
      </div>
    </div>
  )
}
