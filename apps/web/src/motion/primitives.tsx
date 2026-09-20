/**
 * 动效基元 —— 克制、有意图、尊重减少动效偏好。
 *
 * 设计原则：
 *
 * 1. **克制优先。** 学习场景的动效是"手感"，不是"表演"。
 *    所有位移 ≤ 6px，时长 ≤ 320ms，缓动用 ease-out-quart（起步快、收尾稳）。
 *    禁用弹性/回弹（bounce/spring-with-overshoot）—— 那是"泛 AI 装饰"，不是教育软件。
 *
 * 2. **有动效是为了说事。** 掌握度涨、答对了、换讲法了 —— 这些是值得用动画
 *    强调的状态变化；而"内容出现了"用一次性的淡入就够，不该每次刷屏。
 *
 * 3. **全程尊重减少动效。** `useReducedMotion()` 返回 true 时，所有组件
 *    退化成"直接呈现"（opacity/位移归零、时长归零），但**信息与状态不丢** ——
 *    只是把"怎么出现"改成"已经出现"。
 *
 * 这里导出的是**给学习场景专用的封装**，不是 motion 的直接转发：
 * 业务组件只面对语义（Reveal / AnimatedNumber / ProgressBar），不面对 transition 参数。
 */

import {
  AnimatePresence as _AnimatePresence,
  motion,
  useReducedMotion,
  type Variants,
} from 'motion/react'
import { useEffect, useRef, useState } from 'react'

export const EASE_OUT_QUART = [0.165, 0.84, 0.44, 1] as const

/** 项目统一的过渡参数。所有位移小、收尾稳。 */
export const transition = (duration = 0.28) => ({
  duration,
  ease: EASE_OUT_QUART,
})

/** 现在是否可以做动画。减动效偏好时返回 false。 */
export function useMotion(): boolean {
  const reduced = useReducedMotion()
  return !reduced
}

/**
 * 一次性进入动画：淡入 + 微升。
 *
 * 用于「助教的消息」「待复习卡片」「评价反馈」这类**新出现**的内容。
 * 不是打字机 —— 整段一次到位，只是分了几行依次显影。
 */
export function Reveal({
  children,
  delay = 0,
  className,
}: {
  children: React.ReactNode
  delay?: number
  className?: string
}) {
  const enabled = useMotion()
  return (
    <motion.div
      className={className}
      initial={enabled ? { opacity: 0, y: 6 } : false}
      animate={{ opacity: 1, y: 0 }}
      transition={{ ...transition(0.3), delay }}
    >
      {children}
    </motion.div>
  )
}

/**
 * 段落逐个显影的容器与子项。
 *
 * 助教的长回复按段分开浮现：是"他在一页页翻给你看"，
 * 不是"他在打字"。逐段间隔很短（40ms），整体小于 0.4s。
 */
export const staggerParent: Variants = {
  hidden: {},
  show: { transition: { staggerChildren: 0.045 } },
}

export const staggerChild: Variants = {
  hidden: { opacity: 0, y: 4 },
  show: { opacity: 1, y: 0, transition: transition(0.24) },
}

export function ProgressiveText({
  lines,
  className,
  render,
}: {
  lines: string[]
  className?: string
  render: (line: string, index: number) => React.ReactNode
}) {
  const enabled = useMotion()
  if (!enabled) {
    return <div className={className}>{lines.map((line, index) => render(line, index))}</div>
  }
  return (
    <motion.div
      className={className}
      variants={staggerParent}
      initial="hidden"
      animate="show"
    >
      {lines.map((line, index) => (
        <motion.div key={index} variants={staggerChild}>
          {render(line, index)}
        </motion.div>
      ))}
    </motion.div>
  )
}

/**
 * 数字平滑变化。
 *
 * 掌握度 0.00 → 0.24 这类变化不该"啪"地跳过去 ——
 * 用一个 0.5s 的 ease-out 从旧值数到新值，让学习者"看见在动"。
 * 减动效时直接显示新值。
 */
export function AnimatedNumber({
  value,
  format = (v: number) => v.toFixed(2),
  className,
}: {
  value: number
  format?: (v: number) => string
  className?: string
}) {
  const enabled = useMotion()
  const [display, setDisplay] = useState(value)
  const frame = useRef<number>(0)
  const fromRef = useRef(value)

  useEffect(() => {
    if (!enabled) {
      setDisplay(value)
      fromRef.current = value
      return
    }
    const from = fromRef.current
    const to = value
    if (from === to) return
    const started = performance.now()
    const duration = 500

    const tick = (now: number) => {
      const t = Math.min(1, (now - started) / duration)
      // ease-out-quart
      const eased = 1 - Math.pow(1 - t, 4)
      const current = from + (to - from) * eased
      setDisplay(current)
      if (t < 1) {
        frame.current = requestAnimationFrame(tick)
      } else {
        fromRef.current = to
      }
    }
    frame.current = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(frame.current)
  }, [value, enabled])

  return (
    <span className={className} aria-live="polite">
      {format(display)}
    </span>
  )
}

/**
 * 掌握度进度条。
 *
 * 宽度用 transform（scaleX）而不是 width —— 不触发布局，60fps 稳。
 * 减动效时宽度直接到位。
 */
export function ProgressBar({
  value,
  className,
  trackClassName = 'bg-paper-sunken',
  fillClassName = 'bg-moss',
  height = 4,
}: {
  /** 0~1 */
  value: number
  className?: string
  trackClassName?: string
  fillClassName?: string
  height?: number
}) {
  const enabled = useMotion()
  const clamped = Math.max(0, Math.min(1, value))
  return (
    <div
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(clamped * 100)}
      className={`relative overflow-hidden rounded-full ${trackClassName} ${className ?? ''}`}
      style={{ height }}
    >
      <motion.div
        className={`absolute inset-y-0 left-0 w-full origin-left rounded-full ${fillClassName}`}
        initial={false}
        animate={{ scaleX: clamped }}
        transition={
          enabled ? { type: 'tween', duration: 0.5, ease: EASE_OUT_QUART } : { duration: 0 }
        }
        style={{ transformOrigin: 'left' }}
      />
    </div>
  )
}

/** 进入/退出都能被管理的一组元素。 */
export const AnimatePresence = _AnimatePresence
