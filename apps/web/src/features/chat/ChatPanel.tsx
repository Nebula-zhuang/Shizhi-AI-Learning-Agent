import { useEffect, useRef, useState } from 'react'

import { MessageBubble } from './MessageBubble'
import type { DisplayMessage } from './useChat'

interface ChatPanelProps {
  messages: DisplayMessage[]
  isStreaming: boolean
  onSend: (text: string) => void
  onStop: () => void
  onClear: () => void
}

const SUGGESTIONS = [
  '用一句话解释什么是梯度下降',
  '什么是过拟合？如何缓解？',
  '帮我区分监督学习和无监督学习',
]

export function ChatPanel({ messages, isStreaming, onSend, onStop, onClear }: ChatPanelProps) {
  const [draft, setDraft] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const submit = () => {
    if (!draft.trim() || isStreaming) return
    onSend(draft)
    setDraft('')
  }

  return (
    <section className="flex min-h-0 flex-1 flex-col rounded-xl border border-line bg-paper-raised">
      <header className="flex items-center justify-between border-b border-line px-4 py-3">
        <div>
          <h2 className="text-sm font-medium text-ink-1">对话调试面板</h2>
          <p className="text-xs text-ink-4">
            P0 验证用：直连 LLM 网关，确认流式通道可用
          </p>
        </div>
        <button
          type="button"
          onClick={onClear}
          disabled={messages.length === 0}
          className="rounded-md border border-line px-3 py-1.5 text-xs text-ink-2 transition hover:bg-paper-sunken disabled:cursor-not-allowed disabled:opacity-40"
        >
          清空对话
        </button>
      </header>

      <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-4 py-5">
        {messages.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-4 text-center">
            <p className="text-sm text-ink-4">
              输入一个问题，验证流式输出是否正常
            </p>
            <div className="flex flex-wrap justify-center gap-2">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => onSend(s)}
                  className="rounded-full border border-line px-3 py-1.5 text-xs text-ink-2 transition hover:border-moss-line hover:text-moss"
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        ) : (
          messages.map((m) => <MessageBubble key={m.id} message={m} />)
        )}
        <div ref={bottomRef} />
      </div>

      <footer className="border-t border-line p-3">
        <div className="flex items-end gap-2">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                submit()
              }
            }}
            rows={2}
            placeholder="输入问题，Enter 发送 / Shift + Enter 换行"
            className="min-h-[56px] flex-1 resize-none rounded-lg border border-line px-3 py-2 text-sm text-ink-1 outline-none transition focus:border-moss"
          />
          {isStreaming ? (
            <button
              type="button"
              onClick={onStop}
              className="h-10 shrink-0 rounded-lg bg-brick px-4 text-sm font-medium text-white transition hover:bg-brick"
            >
              停止
            </button>
          ) : (
            <button
              type="button"
              onClick={submit}
              disabled={!draft.trim()}
              className="h-10 shrink-0 rounded-lg bg-moss px-4 text-sm font-medium text-white transition hover:bg-moss-ink disabled:cursor-not-allowed disabled:opacity-40"
            >
              发送
            </button>
          )}
        </div>
      </footer>
    </section>
  )
}
