/**
 * 自由学习空间的接口封装。
 *
 * ## 会话走 cookie，不走请求头
 *
 * 后端把会话令牌放在 httpOnly cookie 里（**不进 localStorage**，
 * 见项目的安全约定）。所以这里所有请求都要带 `credentials: 'include'`，
 * 漏一个就会变成匿名身份 —— 而匿名身份在该项目里会回退到默认学习者，
 * 表现为"看不到自己刚建的对话"，是个很难查的 bug。
 *
 * ## `ask` 为什么不用 `postJson`
 *
 * 它返回的是 SSE 流，不是 JSON。用 `fetch` 手工解帧（复用 `sse.ts` 的
 * `SseDecoder`）—— 帧格式很简单，不值得引库。
 */

import { apiUrl, deleteJson, getJson, postJson } from './http'
import { SseDecoder } from './sse'

// --------------------------------------------------------------------------- //
// 类型
// --------------------------------------------------------------------------- //
export interface ConversationSummary {
  id: number
  title: string
  message_count: number
  created_at: string
  last_message_at: string
}

export interface ConversationList {
  items: ConversationSummary[]
  total: number
}

/** 右侧面板要用的一条"本轮用到了什么" */
export interface TurnSource {
  kind: 'knowledge_base' | 'web_search'
  document_id?: number
  file_name?: string
  page?: number | null
  heading_path?: string[]
  preview?: string
  count?: number
  simulated?: boolean
}

/** 一条引用。联网时是网页，引用资料时是文档出处。 */
export interface TurnCitation {
  kind: string
  title?: string
  url?: string
  simulated?: boolean
  /**
   * **实际**执行这次检索的后端：mcp / tavily / mock。
   *
   * 用户不关心走哪条路，但**系统必须知道** —— 界面上要能看出
   * 联网到底走的是 MCP 还是直连 API。没有这个字段，
   * "MCP 有没有在工作"就只能靠代码里有没有那个类来猜。
   */
  provider?: string
  /** 服务端自报的标识，如 "tavily-mcp v4.0.4" */
  providerDetail?: string
  /** 是否发生过回退（首选通道失败、改用了备用） */
  fellBack?: boolean
  /** 回退原因。**为空的原因是没有意义的** —— 只说回退了不说为什么，等于把线索丢掉。 */
  fallbackReason?: string
}

export interface StudyMessage {
  id: number
  role: 'user' | 'assistant'
  content: string
  sources: TurnSource[] | null
  citations: TurnCitation[] | null
  attachments: Record<string, unknown>[] | null
  /** 自然语言状态轨迹。刷新后仍要能看出"它查没查"，所以后端也存了一份。 */
  status_trace: string[] | null
  degraded_reason: string | null
  created_at: string
}

export interface MessageList {
  conversation_id: number
  items: StudyMessage[]
}

/** MCP 的**真实**状态：服务端自报的身份 + 运行时发现的工具名 */
export interface McpStatus {
  configured: boolean
  url: string
  /** 连上过才有值 —— 它是握手读回来的，不是常量 */
  server_info: { name: string; version: string; protocol_version?: string } | null
  /** 运行时从 tools/list 读到的工具名 */
  discovered_tools: string[]
}

export interface StudyCapabilities {
  free_study: boolean
  knowledge_base: boolean
  web_search_available: boolean
  /** 后端给的原话，前端直接用 —— 避免两处措辞不一致 */
  web_search_note: string
  /** 联网后端策略：auto / mcp / tavily */
  web_search_backend: string
  mcp: McpStatus | null
  tools: string[]
  max_capabilities_per_turn: number
}

export interface AttachmentOption {
  document_id: number
  file_name: string
  file_type: string
  parse_status: string
}

// --------------------------------------------------------------------------- //
// 对话
// --------------------------------------------------------------------------- //
export function listConversations(): Promise<ConversationList> {
  return getJson<ConversationList>('/api/study/conversations')
}

export function createConversation(title = ''): Promise<ConversationSummary> {
  return postJson<ConversationSummary>('/api/study/conversations', { title })
}

export function renameConversation(id: number, title: string): Promise<ConversationSummary> {
  return patchJson<ConversationSummary>(`/api/study/conversations/${id}`, { title })
}

export function deleteConversation(id: number): Promise<{ deleted: boolean }> {
  return deleteJson<{ deleted: boolean }>(`/api/study/conversations/${id}`)
}

export function listMessages(id: number): Promise<MessageList> {
  return getJson<MessageList>(`/api/study/conversations/${id}/messages`)
}

export interface AttachmentUpload {
  document_id: number
  file_name: string
  file_type: string
  file_size: number
  /** 是不是图片 —— 界面据此决定要不要显示缩略图 */
  is_image: boolean
  parse_status: string
  dedup: boolean
}

/**
 * 上传一个附件。
 *
 * ⚠️ **必须带 `credentials: 'include'`** —— 会话在 httpOnly cookie 里，
 * 漏了就会以匿名身份上传，然后被后端拒（自由学习要求登录）。
 *
 * 用 `fetch` + `FormData` 而不是 XHR：这里不需要上传进度条
 * （对话附件通常只有几百 KB），少一层抽象更不容易错。
 */
export async function uploadAttachment(file: File): Promise<AttachmentUpload> {
  const form = new FormData()
  form.append('file', file)

  let response: Response
  try {
    response = await fetch(apiUrl('/api/study/attachments'), {
      method: 'POST',
      credentials: 'include',
      body: form,
    })
  } catch {
    throw new Error('连不上服务，附件没传上去。')
  }

  if (!response.ok) {
    throw new Error(await readError(response))
  }
  return (await response.json()) as AttachmentUpload
}

/** 人类可读的文件大小。**不要直接显示字节数** —— 用户看不出 4538 是多大。 */
export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export function getCapabilities(): Promise<StudyCapabilities> {
  return getJson<StudyCapabilities>('/api/study/capabilities')
}

export function listAttachments(): Promise<{ items: AttachmentOption[] }> {
  return getJson<{ items: AttachmentOption[] }>('/api/study/attachments')
}

// --------------------------------------------------------------------------- //
// 保存知识（后端 Phase 3A/3B 的能力，Phase 3C 接入界面）
// --------------------------------------------------------------------------- //
export interface SavedKnowledgeItem {
  id: number
  question: string
  answer: string
  source_message_id: number | null
  source_urls: { title: string; url: string }[] | null
  kp_ids: number[] | null
  tags: string[] | null
  /** 向量库里的 id。为 null 表示这条暂时搜不到（写向量失败，可事后回填）。 */
  embedding_id: string | null
  created_at: string
}

export interface SavedKnowledgeList {
  items: SavedKnowledgeItem[]
  total: number
}

/**
 * 保存一条助手消息。
 *
 * ⚠️ **只传 message_id，不传正文。** 正文由服务端从那条消息里取 ——
 * 这是后端的硬约定：保存下来的东西将来会被检索、被引用，
 * 不能让任意文本（包括被篡改的前端内容）混进"我保存的知识"里。
 */
export function saveKnowledge(
  messageId: number,
  tags?: string[],
): Promise<SavedKnowledgeItem> {
  return postJson<SavedKnowledgeItem>('/api/study/knowledge', {
    message_id: messageId,
    ...(tags && tags.length > 0 ? { tags } : {}),
  })
}

export function listSavedKnowledge(limit = 20, offset = 0): Promise<SavedKnowledgeList> {
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  return getJson<SavedKnowledgeList>(`/api/study/knowledge?${query.toString()}`)
}

// --------------------------------------------------------------------------- //
// 学习主题 → 知识点（5C-2）
// --------------------------------------------------------------------------- //
/**
 * 后端把主题解析成已有知识点的结果。
 *
 * ⚠️ **`matched` 为 false 时 `kp_id` 一定是 null** —— 后端不会编一个 id 出来。
 * 前端也必须照此办理：拿不到 id 就**不要去开教学**（那条路会走进一个
 * 完全无关的知识点）。判断逻辑见 `features/study/learnTarget.ts`。
 */
export interface LearnTargetResponse {
  matched: boolean
  kp_id: number | null
  title: string
  document_id: number | null
  /** 机器码：`exact` / `normalized` / `contained` / `none` / `ambiguous` */
  reason: string
}

/**
 * 把"想学 X"的主题解析成一个已有知识点。
 *
 * 只做解析，**不写任何状态、不开会话** —— 拿到 kpId 之后由调用方
 * 走 `LearningProvider.startLearning(focus)`，与在「辅导」页亲手挑一个点完全同一条路。
 */
export function resolveLearnTarget(topic: string): Promise<LearnTargetResponse> {
  return postJson<LearnTargetResponse>('/api/study/learn-target', { topic })
}

/**
 * PATCH 的封装。
 *
 * `http.ts` 里只导出了 get/post/put/delete —— 加这一个比让调用方
 * 自己拼 fetch 好：凭证、错误提取、超时兜底都在同一处。
 */
async function patchJson<T>(path: string, body: unknown): Promise<T> {
  let response: Response
  try {
    response = await fetch(apiUrl(path), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify(body),
    })
  } catch {
    throw new Error('连不上服务。请确认学习伙伴还在运行，稍后再试。')
  }
  if (!response.ok) {
    const detail = await response.text()
    throw new Error(detail.slice(0, 200) || `请求失败（${response.status}）`)
  }
  return (await response.json()) as T
}

// --------------------------------------------------------------------------- //
// 提问（流式）
// --------------------------------------------------------------------------- //
/** 一次工具调用的开始/结果（阶段 2 新增，用于右侧面板展示 Agent 做了什么） */
export interface ToolEvent {
  tool: string
  argumentsSummary?: string
  ok?: boolean
  summary?: string
  provider?: string
  fellBack?: boolean
  fallbackReason?: string
}

/** 一轮里 Agent 走的每一步（Developer Mode 用） */
export interface LoopStep {
  index: number
  state: string
  thought: string
  tool: string | null
  tool_ok: boolean | null
  tool_error: string | null
  elapsed_ms: number
}

/**
 * 学习意图的类型与解析。
 *
 * ⚠️ 定义在 `./learnSuggestion` 而不是这里 —— `api/study.ts` 有运行时依赖
 * （`./http`），而前端单测用 `--experimental-strip-types` 会真的去解析模块路径，
 * 于是"从 api/study 导入一个值"的测试必然 `ERR_MODULE_NOT_FOUND`。
 * 那个文件零依赖、可以放心被 `.ts` 测试引用。
 *
 * ⚠️ 这里要**既 import 又 re-export**：`export … from` 只是转发，
 * 不会在本文件里引入名字，而下面的 `onDone` 签名要用到 `LearnSuggestion`。
 */
import { parseLearnSuggestion, type LearnSuggestion } from './learnSuggestion'

export { parseLearnSuggestion }
export type { LearnSuggestion }

export interface AskHandlers {
  /** 自然语言状态。**直接用后端给的原话** —— 前端不自己编措辞。 */
  onStatus?: (text: string) => void
  /** 正文增量 */
  onDelta?: (text: string) => void
  /** 本轮用到的资料片段 */
  onSource?: (source: TurnSource) => void
  /** 引用来源 */
  onCitation?: (citation: TurnCitation) => void
  /** 开始调用某个工具 —— 用来显示「正在翻你的资料」这类过程 */
  onToolStart?: (event: ToolEvent) => void
  /** 工具返回。**带上实际用的后端**，界面据此显示「经由 MCP」 */
  onToolResult?: (event: ToolEvent) => void
  onDone?: (result: {
    capabilities: string[]
    sources: TurnSource[]
    citations: TurnCitation[]
    status_trace: string[]
    degraded_reason: string | null
    steps: LoopStep[]
    /** 本轮联网实际用的后端：mcp / tavily / 空串表示没联网 */
    provider: string
    fell_back: boolean
    fallback_reason: string
    /**
     * 学习意图。**只有后端判断出"他想学一个主题"时才有这个键** ——
     * 普通提问这里是 `undefined`（不是 null），据此决定要不要显示提议卡。
     */
    learn_suggestion?: LearnSuggestion
  }) => void
  onError?: (message: string) => void
  /**
   * 落库成功后的那条助手消息的 id。
   *
   * ⚠️ 它**必然在 `done` 之后**到达 —— 后端把 `persisted` 放在
   * `append_message` 成功之后才推（落库失败就不推）。
   * 所以"保存这一轮"要等这个回调，而不是 `done`。
   */
  onPersisted?: (messageId: number) => void
  signal?: AbortSignal
}

/**
 * 提一个问题，边收边回调。
 *
 * 与 Tutor 的流式接口同构：**帧顺序即真实发生顺序**，
 * `status` 帧都对应后台真的在做那件事，没有假进度。
 */
export async function askQuestion(
  conversationId: number,
  question: string,
  documentIds: number[] = [],
  handlers: AskHandlers = {},
): Promise<void> {
  let response: Response
  try {
    response = await fetch(apiUrl(`/api/study/conversations/${conversationId}/ask`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      credentials: 'include',
      body: JSON.stringify({ question, document_ids: documentIds }),
      signal: handlers.signal,
    })
  } catch {
    handlers.onError?.('连不上服务。请确认学习伙伴还在运行，稍后再试。')
    return
  }

  if (!response.ok) {
    handlers.onError?.(await readError(response))
    return
  }
  if (!response.body) {
    handlers.onError?.('服务没有返回流式响应。')
    return
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  const sse = new SseDecoder()

  const dispatch = (event: string, data: unknown) => {
    const payload = (data ?? {}) as Record<string, unknown>
    switch (event) {
      case 'status':
        handlers.onStatus?.(String(payload.text ?? ''))
        break
      case 'delta':
        handlers.onDelta?.(String(payload.text ?? ''))
        break
      case 'source':
        handlers.onSource?.(payload as unknown as TurnSource)
        break
      case 'citation':
        handlers.onCitation?.(payload as unknown as TurnCitation)
        break
      case 'tool_start':
        handlers.onToolStart?.({
          tool: String(payload.tool ?? ''),
          argumentsSummary: String(payload.arguments_summary ?? ''),
        })
        break
      case 'tool_result':
        handlers.onToolResult?.({
          tool: String(payload.tool ?? ''),
          ok: Boolean(payload.ok),
          summary: String(payload.summary ?? ''),
          provider: (payload.provider as string) ?? undefined,
          fellBack: Boolean(payload.fell_back),
          fallbackReason: (payload.fallback_reason as string) ?? undefined,
        })
        break
      case 'done': {
        const suggestion = parseLearnSuggestion(payload.learn_suggestion)
        handlers.onDone?.({
          capabilities: (payload.capabilities as string[]) ?? [],
          sources: (payload.sources as TurnSource[]) ?? [],
          citations: (payload.citations as TurnCitation[]) ?? [],
          status_trace: (payload.status_trace as string[]) ?? [],
          degraded_reason: (payload.degraded_reason as string) ?? null,
          steps: (payload.steps as LoopStep[]) ?? [],
          provider: (payload.provider as string) ?? '',
          fell_back: Boolean(payload.fell_back),
          fallback_reason: (payload.fallback_reason as string) ?? '',
          // 有才带上这个键（不是 `undefined` 占位）——
          // 下游靠"键在不在"判断要不要显示提议卡。
          ...(suggestion ? { learn_suggestion: suggestion } : {}),
        })
        break
      }
      case 'error':
        handlers.onError?.(String(payload.text ?? '这轮回答没能完成。'))
        break
      case 'persisted': {
        // 落库成功后才会来这一帧。拿不到合法 id 就当没收到 ——
        // 前端绝不能自己编一个 id 去保存（后端会 404）。
        const id = Number(payload.message_id)
        if (Number.isInteger(id) && id > 0) handlers.onPersisted?.(id)
        break
      }
      default:
        break
    }
  }

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      for (const frame of sse.push(decoder.decode(value, { stream: true }))) {
        dispatch(frame.event, frame.data)
      }
    }
    for (const frame of sse.flush()) dispatch(frame.event, frame.data)
  } catch (error) {
    // 用户主动中止不算错误
    if ((error as Error)?.name !== 'AbortError') {
      handlers.onError?.('回答中断了。你可以再问一次。')
    }
  }
}

async function readError(response: Response): Promise<string> {
  try {
    const text = await response.text()
    if (!text) return `请求失败（${response.status}）`
    try {
      const parsed = JSON.parse(text) as { detail?: string }
      if (parsed.detail) return parsed.detail
    } catch {
      /* 不是 JSON 就用原文 */
    }
    return text.slice(0, 200)
  } catch {
    return `请求失败（${response.status}）`
  }
}
