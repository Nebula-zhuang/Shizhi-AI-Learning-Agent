/**
 * 状态组件：骨架屏 / 空态 / 错误 / 载入 / 成功。
 *
 * ## 这五个状态决定"完成度"
 *
 * 大多数界面在"有数据"时都好看，拉开差距的是**没有数据、正在加载、出错了**的时候。
 * v1 在这几处都是干巴巴一行灰字，v2 全部重做。
 *
 * ## 空态的一条原则：给下一步，不给抱歉
 *
 * 空态不是"这里什么都没有"，而是"这里本来可以有什么、你要怎么做才能有"。
 * 所以每个空态都必须带一个具体的行动入口，措辞用产品口吻而不是系统口吻。
 */

import type { ReactNode } from 'react'
import { motion } from 'motion/react'

import { cn } from './primitives'
import { IconAlert, IconInbox } from './icons'

/* ══════════════════════════════════════════════════════════════════════════
   骨架屏
   ══════════════════════════════════════════════════════════════════════════ */

export function Skeleton({
  className,
  width,
  height,
}: {
  className?: string
  width?: string | number
  height?: string | number
}) {
  return (
    <span
      aria-hidden="true"
      className={cn('skeleton block', className)}
      style={{ width, height }}
    />
  )
}

/** 学习空间的骨架：左侧书签列 + 中间内容 + 右侧状态 */
export function SkeletonWorkspace() {
  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_300px]" aria-hidden="true">
      <div className="space-y-6">
        <div className="surface p-6">
          <Skeleton width="32%" height={13} />
          <Skeleton className="mt-4" width="68%" height={26} />
          <div className="mt-6 space-y-2.5">
            <Skeleton width="92%" height={14} />
            <Skeleton width="78%" height={14} />
            <Skeleton width="85%" height={14} />
          </div>
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          {[0, 1].map((i) => (
            <div key={i} className="surface p-5">
              <Skeleton width="40%" height={12} />
              <Skeleton className="mt-3" width="72%" height={17} />
              <Skeleton className="mt-2.5" width="56%" height={13} />
            </div>
          ))}
        </div>
      </div>
      <div className="surface h-fit p-5">
        <Skeleton width="46%" height={12} />
        <div className="mt-5 space-y-3">
          <Skeleton width="100%" height={38} />
          <Skeleton width="100%" height={38} />
          <Skeleton width="82%" height={38} />
        </div>
      </div>
    </div>
  )
}

/* ══════════════════════════════════════════════════════════════════════════
   空态
   ══════════════════════════════════════════════════════════════════════════ */

export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
}: {
  icon?: ReactNode
  title: string
  description?: ReactNode
  action?: ReactNode
  className?: string
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.34, ease: [0.16, 1, 0.3, 1] }}
      className={cn(
        'flex flex-col items-center justify-center rounded-lg border border-dashed border-line-strong px-6 py-14 text-center',
        className,
      )}
    >
      <span className="mb-4 inline-flex h-11 w-11 items-center justify-center rounded-full bg-paper-sunken text-ink-4">
        {icon ?? <IconInbox size={20} />}
      </span>
      <p className="font-serif text-base text-ink-1">{title}</p>
      {description && (
        <p className="mt-2 max-w-[30rem] text-xs leading-relaxed text-ink-3">{description}</p>
      )}
      {action && <div className="mt-5">{action}</div>}
    </motion.div>
  )
}

/* ══════════════════════════════════════════════════════════════════════════
   错误态
   ══════════════════════════════════════════════════════════════════════════ */

export function ErrorState({
  title = '这一步没走通',
  message,
  onRetry,
  retryLabel = '再试一次',
  className,
}: {
  title?: string
  message?: string
  onRetry?: () => void
  retryLabel?: string
  className?: string
}) {
  return (
    <div
      role="alert"
      className={cn(
        'flex items-start gap-3 rounded-lg border border-brick-line bg-brick-soft/70 px-4 py-3.5',
        className,
      )}
    >
      <IconAlert size={17} className="mt-0.5 shrink-0 text-brick" />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-brick-ink">{title}</p>
        {message && <p className="mt-1 text-xs leading-relaxed text-brick-ink/85">{message}</p>}
      </div>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="shrink-0 rounded-md border border-brick-line bg-surface-1/60 px-2.5 py-1 text-2xs text-brick-ink transition-colors hover:bg-surface-1"
        >
          {retryLabel}
        </button>
      )}
    </div>
  )
}

/* ══════════════════════════════════════════════════════════════════════════
   载入 / 成功标记
   ══════════════════════════════════════════════════════════════════════════ */

/** 呼吸点。等待态的基础单元 —— 比转圈更"安静"，也更符合"它在想"的语义 */
export function Pulse({ className, delay = 0 }: { className?: string; delay?: number }) {
  return (
    <span
      aria-hidden="true"
      className={cn('inline-block h-1.5 w-1.5 rounded-full bg-current', className)}
      style={{ animation: `breathe 1.3s ease-in-out ${delay}s infinite` }}
    />
  )
}

export function Thinking({ label = '正在想…' }: { label?: string }) {
  return (
    <p className="flex items-center gap-2.5 text-sm text-ink-3">
      <span className="flex gap-1 text-ink-4">
        <Pulse delay={0} />
        <Pulse delay={0.16} />
        <Pulse delay={0.32} />
      </span>
      {label}
    </p>
  )
}

/** 成功：一个描边勾在圆形里画出来。用在"完成"这类需要一点仪式感的地方 */
export function SuccessMark({ label }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-moss-ink">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.5" opacity="0.35" />
        <motion.path
          d="m7.8 12.4 2.9 2.9 5.6-6"
          stroke="currentColor"
          strokeWidth="1.7"
          strokeLinecap="round"
          strokeLinejoin="round"
          initial={{ pathLength: 0 }}
          animate={{ pathLength: 1 }}
          transition={{ duration: 0.42, ease: [0.16, 1, 0.3, 1] }}
        />
      </svg>
      {label && <span className="text-xs">{label}</span>}
    </span>
  )
}
