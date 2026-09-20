/**
 * 浮层：Toast 与 Modal。
 *
 * ## Toast 的定位
 *
 * 只用于**操作结果**（保存成功、上传失败、退出登录），不用于承载信息。
 * 需要用户读一段话的地方一律用页内展示 —— 会自动消失的东西不能放重要内容。
 *
 * ## Modal 的定位
 *
 * 只在"必须打断"时用。学习类产品里绝大多数情况用页内展开或抽屉更好 ——
 * 模态会切断"我正在读的东西"这个上下文。这里只提供最基础的实现。
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { AnimatePresence, motion } from 'motion/react'

import { cn } from './primitives'
import { IconAlert, IconCheck, IconClose } from './icons'

/* ══════════════════════════════════════════════════════════════════════════
   Toast
   ══════════════════════════════════════════════════════════════════════════ */

type ToastTone = 'success' | 'error' | 'info'

interface ToastItem {
  id: number
  tone: ToastTone
  message: string
  detail?: string
}

interface ToastContextValue {
  push: (tone: ToastTone, message: string, detail?: string) => void
  success: (message: string, detail?: string) => void
  error: (message: string, detail?: string) => void
  info: (message: string, detail?: string) => void
}

const ToastContext = createContext<ToastContextValue>({
  push: () => {},
  success: () => {},
  error: () => {},
  info: () => {},
})

export const useToast = () => useContext(ToastContext)

const TONE_ACCENT: Record<ToastTone, string> = {
  success: 'text-moss',
  error: 'text-brick',
  info: 'text-slate-blue',
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])
  const nextId = useRef(1)

  const remove = useCallback((id: number) => {
    setItems((current) => current.filter((item) => item.id !== id))
  }, [])

  const push = useCallback(
    (tone: ToastTone, message: string, detail?: string) => {
      const id = nextId.current++
      setItems((current) => [...current.slice(-2), { id, tone, message, detail }])
      // 成功 2.6s，出错 5s —— 出错要留够读完的时间
      window.setTimeout(() => remove(id), tone === 'error' ? 5000 : 2600)
    },
    [remove],
  )

  const value = useMemo<ToastContextValue>(
    () => ({
      push,
      success: (m, d) => push('success', m, d),
      error: (m, d) => push('error', m, d),
      info: (m, d) => push('info', m, d),
    }),
    [push],
  )

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        role="region"
        aria-label="操作结果"
        className="pointer-events-none fixed bottom-6 left-1/2 z-[70] flex -translate-x-1/2 flex-col items-center gap-2"
      >
        <AnimatePresence initial={false}>
          {items.map((item) => (
            <motion.div
              key={item.id}
              layout
              initial={{ opacity: 0, y: 14, scale: 0.97 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 6, scale: 0.98 }}
              transition={{ duration: 0.26, ease: [0.16, 1, 0.3, 1] }}
              className="glass pointer-events-auto flex max-w-[26rem] items-start gap-2.5 rounded-lg px-3.5 py-2.5 shadow-e3"
            >
              <span className={cn('mt-0.5 shrink-0', TONE_ACCENT[item.tone])}>
                {item.tone === 'success' ? <IconCheck size={15} /> : <IconAlert size={15} />}
              </span>
              <div className="min-w-0">
                <p className="text-xs text-ink-1">{item.message}</p>
                {item.detail && (
                  <p className="mt-0.5 text-2xs leading-relaxed text-ink-3">{item.detail}</p>
                )}
              </div>
              <button
                type="button"
                onClick={() => remove(item.id)}
                aria-label="关闭提示"
                className="ml-1 shrink-0 text-ink-4 transition-colors hover:text-ink-2"
              >
                <IconClose size={13} />
              </button>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </ToastContext.Provider>
  )
}

/* ══════════════════════════════════════════════════════════════════════════
   Modal
   ══════════════════════════════════════════════════════════════════════════ */

export function Modal({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  width = '30rem',
}: {
  open: boolean
  onClose: () => void
  title: string
  description?: string
  children?: ReactNode
  footer?: ReactNode
  width?: string
}) {
  const panel = useRef<HTMLDivElement>(null)

  // Esc 关闭 + 打开时锁滚动 + 焦点移入面板（键盘用户不会"丢在背景里"）
  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => event.key === 'Escape' && onClose()
    document.addEventListener('keydown', onKey)
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    panel.current?.focus()
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = previous
    }
  }, [open, onClose])

  return (
    <AnimatePresence>
      {open && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center p-6">
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
            onClick={onClose}
            className="absolute inset-0 bg-[rgb(30_26_21_/_0.4)] backdrop-blur-[3px]"
          />
          <motion.div
            ref={panel}
            role="dialog"
            aria-modal="true"
            aria-label={title}
            tabIndex={-1}
            initial={{ opacity: 0, y: 14, scale: 0.985 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 8, scale: 0.99 }}
            transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
            className="surface relative z-10 w-full overflow-hidden shadow-e4 outline-none"
            style={{ maxWidth: width }}
          >
            <div className="flex items-start justify-between gap-4 px-6 pb-4 pt-5">
              <div>
                <h2 className="text-lg">{title}</h2>
                {description && (
                  <p className="mt-1.5 text-xs leading-relaxed text-ink-3">{description}</p>
                )}
              </div>
              <button
                type="button"
                onClick={onClose}
                aria-label="关闭"
                className="btn-ghost -mr-1.5 -mt-1 p-1.5"
              >
                <IconClose size={15} />
              </button>
            </div>

            {children && <div className="px-6 pb-5">{children}</div>}

            {footer && (
              <div className="flex items-center justify-end gap-2.5 border-t border-line bg-paper-sunken/60 px-6 py-4">
                {footer}
              </div>
            )}
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  )
}
