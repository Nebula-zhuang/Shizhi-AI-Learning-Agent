/**
 * 后端接口封装。
 *
 * 关于 SSE：浏览器的 EventSource 只支持 GET，无法提交对话历史，
 * 因此这里用 fetch + ReadableStream 手动解析 text/event-stream。
 * 协议约定见后端 app/api/sse.py。
 * 帧解析逻辑抽在 ./sse.ts，便于脱离浏览器环境做单元测试。
 */

import { SseDecoder, type SseFrame } from './sse'

export type ChatRole = 'system' | 'user' | 'assistant'

export interface ChatMessage {
  role: ChatRole
  content: string
}

export interface StreamMeta {
  model: string
  mode: string
  request_id: string
}

export interface StreamDone {
  request_id: string
  chunks: number
  chars: number
  elapsed_ms: number
}

export interface StreamHandlers {
  onMeta?: (meta: StreamMeta) => void
  onDelta: (text: string) => void
  onDone?: (info: StreamDone) => void
  onError?: (message: string) => void
}

export interface ComponentStatus {
  name: string
  ok: boolean
  detail: Record<string, unknown>
}

export interface HealthResult {
  status: 'ok' | 'degraded'
  app: string
  env: string
  version: string
  components: ComponentStatus[]
}

/** 留空则使用相对路径，由 Vite 代理转发到后端 */
const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

function url(path: string): string {
  return `${API_BASE}${path}`
}

/** 从错误响应里尽力提取可读信息 */
async function extractError(res: Response): Promise<string> {
  try {
    const text = await res.text()
    try {
      const parsed = JSON.parse(text) as { detail?: unknown }
      if (typeof parsed.detail === 'string') return parsed.detail
    } catch {
      /* 非 JSON，直接用原文 */
    }
    return text || `HTTP ${res.status}`
  } catch {
    return `HTTP ${res.status} ${res.statusText}`
  }
}

/**
 * 发起流式对话。
 * 失败与中断都通过 handlers.onError 上报，不抛异常。
 *
 * ⚠️ **必须带 `credentials: 'include'`** —— 这个接口已要求登录
 * （2026-09-21 安全审计后闭合的"未授权 LLM 代理"），
 * 而会话在 httpOnly cookie 里。漏了这一行，**已登录用户也会拿到 401**，
 * 表现为"开发者 → 接口调试"页莫名其妙不可用。
 */
export async function streamChat(
  messages: ChatMessage[],
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  let res: Response
  try {
    res = await fetch(url('/api/chat/stream'), {
      method: 'POST',
      // 会话在 httpOnly cookie 里，不带凭据就是匿名身份
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
      },
      body: JSON.stringify({ messages }),
      signal,
    })
  } catch (err) {
    if ((err as Error).name === 'AbortError') return
    handlers.onError?.('无法连接后端服务，请确认后端已启动（默认 http://127.0.0.1:8000）。')
    return
  }

  if (!res.ok) {
    handlers.onError?.(await extractError(res))
    return
  }
  if (!res.body) {
    handlers.onError?.('响应体为空，无法读取流。')
    return
  }

  const reader = res.body.getReader()
  const textDecoder = new TextDecoder('utf-8')
  const sseDecoder = new SseDecoder()

  const dispatch = (frame: SseFrame): void => {
    switch (frame.event) {
      case 'meta':
        handlers.onMeta?.(frame.data as StreamMeta)
        break
      case 'delta':
        handlers.onDelta((frame.data as { text: string }).text ?? '')
        break
      case 'done':
        handlers.onDone?.(frame.data as StreamDone)
        break
      case 'error':
        handlers.onError?.((frame.data as { message: string }).message ?? '未知错误')
        break
      default:
        break
    }
  }

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      // 一次 read 可能拿到半个帧，也可能拿到好几个帧，交给解码器处理
      for (const frame of sseDecoder.push(textDecoder.decode(value, { stream: true }))) {
        dispatch(frame)
      }
    }
    // 流结束时清掉尾部残留（服务端未以空行收尾的情况）
    for (const frame of sseDecoder.flush()) {
      dispatch(frame)
    }
  } catch (err) {
    if ((err as Error).name !== 'AbortError') {
      handlers.onError?.(`流式读取中断：${(err as Error).message}`)
    }
  } finally {
    reader.releaseLock()
  }
}

/** 查询后端各组件状态 */
export async function fetchHealth(): Promise<HealthResult> {
  const res = await fetch(url('/api/health'))
  if (!res.ok) throw new Error(await extractError(res))
  return (await res.json()) as HealthResult
}
