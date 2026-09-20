import { ChatPanel } from './ChatPanel'
import { useChat } from './useChat'

/**
 * 对话视图。
 *
 * 包装层，把 P0 的 ChatPanel 与 useChat 原样组合起来。
 * P0 的这两个文件一行未改 —— 它们已经过验证，没有理由为了结构调整去动它们。
 */
export function ChatView() {
  const { messages, isStreaming, error, send, stop, clear } = useChat()

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4">
      {error && (
        <div className="rounded-lg border border-brick-line bg-brick-soft px-4 py-3 text-sm text-brick-ink">
          {error}
        </div>
      )}
      <ChatPanel
        messages={messages}
        isStreaming={isStreaming}
        onSend={(text) => void send(text)}
        onStop={stop}
        onClear={clear}
      />
    </div>
  )
}
