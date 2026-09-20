import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

import '../styles/markdown.css'

interface MarkdownViewProps {
  /** Markdown 原文 */
  children: string
  className?: string
}

/**
 * Markdown 渲染。
 *
 * 知识点的 `summary` 与 `details` 都是 Markdown（可能含表格、列表、代码块），
 * 因此必须真正渲染而不是直接显示原文 —— 这也是修掉 P0 遗留问题
 * 「**粗体** 被当作纯文本显示」的地方。
 */
export function MarkdownView({ children, className }: MarkdownViewProps) {
  return (
    <div className={['md-body', className].filter(Boolean).join(' ')}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
    </div>
  )
}
