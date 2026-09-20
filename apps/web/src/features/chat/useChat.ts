import { useCallback, useRef, useState } from 'react'

import { streamChat, type ChatMessage } from '../../api/client'

export interface DisplayMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  /** 是否仍在流式接收中（用于显示光标） */
  streaming?: boolean
  meta?: { model: string; mode: string }
  stats?: { chunks: number; chars: number; elapsed_ms: number }
  error?: string
}

let seq = 0
const nextId = () => `m${++seq}`

/**
 * 对话状态管理。
 *
 * P0 的职责边界很清晰：只负责「把消息发给后端、把 SSE 增量拼到界面上」。
 * P4 接入 Tutor Agent 后，这里会增加 action_type（教学动作标签）、
 * references（引用片段）等字段的渲染，但发送逻辑不变。
 */
export function useChat() {
  const [messages, setMessages] = useState<DisplayMessage[]>([])
  const [isStreaming, setIsStreaming] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const messagesRef = useRef<DisplayMessage[]>([])

  messagesRef.current = messages

  const patch = useCallback((id: string, updater: (m: DisplayMessage) => DisplayMessage) => {
    setMessages((prev) => prev.map((m) => (m.id === id ? updater(m) : m)))
  }, [])

  const send = useCallback(
    async (raw: string) => {
      const content = raw.trim()
      if (!content || isStreaming) return

      setError(null)

      const userMsg: DisplayMessage = { id: nextId(), role: 'user', content }
      const assistantId = nextId()
      const assistantMsg: DisplayMessage = {
        id: assistantId,
        role: 'assistant',
        content: '',
        streaming: true,
      }

      // 只把成功的往返带进上下文，避免把错误信息也喂给模型
      const history: ChatMessage[] = messagesRef.current
        .filter((m) => !m.error && m.content.trim())
        .map((m) => ({ role: m.role, content: m.content }))
      history.push({ role: 'user', content })

      setMessages((prev) => [...prev, userMsg, assistantMsg])
      setIsStreaming(true)

      const controller = new AbortController()
      abortRef.current = controller

      await streamChat(
        history,
        {
          onMeta: (meta) => patch(assistantId, (m) => ({ ...m, meta })),
          onDelta: (text) => patch(assistantId, (m) => ({ ...m, content: m.content + text })),
          onDone: (info) =>
            patch(assistantId, (m) => ({
              ...m,
              streaming: false,
              stats: { chunks: info.chunks, chars: info.chars, elapsed_ms: info.elapsed_ms },
            })),
          onError: (message) => {
            setError(message)
            patch(assistantId, (m) => ({ ...m, streaming: false, error: message }))
          },
        },
        controller.signal,
      )

      setIsStreaming(false)
      abortRef.current = null
    },
    [isStreaming, patch],
  )

  const stop = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    setIsStreaming(false)
    setMessages((prev) =>
      prev.map((m) => (m.streaming ? { ...m, streaming: false } : m)),
    )
  }, [])

  const clear = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    setIsStreaming(false)
    setError(null)
    setMessages([])
  }, [])

  return { messages, isStreaming, error, send, stop, clear }
}
