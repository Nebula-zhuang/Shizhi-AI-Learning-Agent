import type { DisplayMessage } from './useChat'

function RoleTag({ role }: { role: DisplayMessage['role'] }) {
  const isUser = role === 'user'
  return (
    <span
      className={[
        'inline-flex h-6 items-center rounded-md px-2 text-xs font-medium',
        isUser
          ? 'bg-line text-ink-2'
          : 'bg-moss-soft text-moss-ink',
      ].join(' ')}
    >
      {isUser ? '我' : 'Learning Buddy'}
    </span>
  )
}

export function MessageBubble({ message }: { message: DisplayMessage }) {
  const isUser = message.role === 'user'

  return (
    <div className={['flex flex-col gap-2', isUser ? 'items-end' : 'items-start'].join(' ')}>
      <div className="flex items-center gap-2">
        <RoleTag role={message.role} />
        {message.meta && (
          <span className="text-xs text-ink-4">
            {message.meta.model} · {message.meta.mode === 'mock' ? '模拟模式' : '真实模型'}
          </span>
        )}
      </div>

      <div
        className={[
          'max-w-[85%] rounded-xl border px-4 py-3 text-sm leading-relaxed whitespace-pre-wrap break-words',
          isUser
            ? 'border-line bg-paper-sunken text-ink-1'
            : message.error
              ? 'border-brick-line bg-brick-soft text-brick-ink'
              : 'border-line bg-paper-raised text-ink-1',
        ].join(' ')}
      >
        {message.content}
        {message.streaming && <span className="stream-cursor" aria-hidden="true" />}
        {message.streaming && !message.content && (
          <span className="text-ink-4">正在等待模型响应…</span>
        )}
      </div>

      {message.stats && (
        <span className="text-xs text-ink-4">
          共 {message.stats.chunks} 个数据块 / {message.stats.chars} 字符 /{' '}
          {message.stats.elapsed_ms} ms
        </span>
      )}
    </div>
  )
}
