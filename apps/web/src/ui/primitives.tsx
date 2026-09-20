/**
 * UI 基础组件库。
 *
 * ## 为什么要先建这一层
 *
 * v1 的界面问题不在配色，在**碎片化**：同一个"次要标签"在四个文件里有四种写法，
 * 圆角、内距、字号都差一点。单看每一处都不明显，叠起来就是"不精致"。
 *
 * 所以 v2 的所有界面元素都从这里取。组件内部**只允许用设计令牌**
 * （`text-ink-2` / `shadow-e2` / `rounded-lg`），不写任何字面色值或魔法数字。
 *
 * ## 一条刻意遵守的规则：组件不负责"位置"
 *
 * 这里只管"长什么样"，不管"放在哪"。外距一律由使用处决定 ——
 * 组件自带 margin 是布局失控的开始（改一个间距要翻四层）。
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type HTMLAttributes,
  type ReactNode,
} from 'react'
import clsx from 'clsx'

import { IconCheck } from './icons'

/** 合并类名。后写的 Tailwind 类覆盖先写的同类属性 */
export const cn = (...parts: Array<string | false | null | undefined>) =>
  clsx(parts)

/* ══════════════════════════════════════════════════════════════════════════
   按钮
   ══════════════════════════════════════════════════════════════════════════ */

type ButtonVariant = 'primary' | 'quiet' | 'ghost' | 'danger'
type ButtonSize = 'sm' | 'md' | 'lg'

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  /** 载入中：禁用交互并显示进度点，**但不改变按钮宽度**（否则会跳） */
  loading?: boolean
  icon?: ReactNode
  /** 图标放到文字后面（如"继续 →"） */
  iconEnd?: ReactNode
}

const BUTTON_VARIANT: Record<ButtonVariant, string> = {
  primary: 'btn-primary',
  quiet: 'btn-quiet',
  ghost: 'btn-ghost',
  danger:
    'inline-flex items-center justify-center gap-2 rounded-md border border-brick-line bg-transparent text-brick-ink transition-colors hover:bg-brick-soft',
}

/* 尺寸只覆盖"内距与字号"这一维；底色/描边由 variant 决定。
   md 留空是因为 .btn-* 组件类里已经写了 md 的内距 —— 避免两处都定义。 */
const BUTTON_SIZE: Record<ButtonSize, string> = {
  sm: 'text-xs px-3 py-1.5',
  md: '',
  lg: 'text-sm px-5 py-2.5',
}

export function Button({
  variant = 'quiet',
  size = 'md',
  loading = false,
  icon,
  iconEnd,
  className,
  children,
  disabled,
  ...rest
}: ButtonProps) {
  return (
    <button
      type="button"
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={cn(BUTTON_VARIANT[variant], BUTTON_SIZE[size], className)}
      {...rest}
    >
      {loading ? (
        <span className="inline-flex items-center gap-1" aria-hidden="true">
          {[0, 1, 2].map((i) => (
            <span
              key={i}
              className="h-1 w-1 rounded-full bg-current opacity-40"
              style={{ animation: `breathe 1.1s ease-in-out ${i * 0.16}s infinite` }}
            />
          ))}
        </span>
      ) : (
        icon
      )}
      {children}
      {!loading && iconEnd}
    </button>
  )
}

/* ══════════════════════════════════════════════════════════════════════════
   徽标
   ══════════════════════════════════════════════════════════════════════════ */

export type Tone = 'neutral' | 'moss' | 'sienna' | 'brick' | 'slate'

const TONE_CLASS: Record<Tone, string> = {
  neutral: 'bg-paper-sunken text-ink-2 border-line',
  moss: 'bg-moss-soft text-moss-ink border-moss-line',
  sienna: 'bg-sienna-soft text-sienna-ink border-sienna-line',
  brick: 'bg-brick-soft text-brick-ink border-brick-line',
  slate: 'bg-slate-blue-soft text-slate-blue border-slate-blue-line',
}

/** 状态点：比徽标更轻，用在列表行首表示状态 */
export const TONE_DOT: Record<Tone, string> = {
  neutral: 'bg-ink-4',
  moss: 'bg-moss',
  sienna: 'bg-sienna',
  brick: 'bg-brick',
  slate: 'bg-slate-blue',
}

export function Badge({
  tone = 'neutral',
  className,
  children,
  dot = false,
}: {
  tone?: Tone
  className?: string
  children: ReactNode
  dot?: boolean
}) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-2xs leading-5',
        TONE_CLASS[tone],
        className,
      )}
    >
      {dot && (
        <span className={cn('h-1.5 w-1.5 rounded-full', TONE_DOT[tone])} aria-hidden="true" />
      )}
      {children}
    </span>
  )
}

/* ══════════════════════════════════════════════════════════════════════════
   表面
   ══════════════════════════════════════════════════════════════════════════ */

export function Card({
  className,
  interactive = false,
  padded = true,
  children,
  ...rest
}: HTMLAttributes<HTMLDivElement> & { interactive?: boolean; padded?: boolean }) {
  return (
    <div
      className={cn(
        'surface',
        interactive && 'surface-interactive cursor-pointer',
        padded && 'p-5',
        className,
      )}
      {...rest}
    >
      {children}
    </div>
  )
}

/** 区块标题。统一"小标签 + 大标题"的层级，避免每页各写一套 */
export function SectionTitle({
  eyebrow,
  title,
  action,
  className,
}: {
  eyebrow?: string
  title: ReactNode
  action?: ReactNode
  className?: string
}) {
  return (
    <div className={cn('flex items-end justify-between gap-4', className)}>
      <div className="min-w-0">
        {eyebrow && <p className="meta mb-1 tracking-[0.14em]">{eyebrow}</p>}
        <h2 className="truncate text-lg">{title}</h2>
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  )
}

/* ══════════════════════════════════════════════════════════════════════════
   分段控件
   ══════════════════════════════════════════════════════════════════════════ */

export interface SegmentOption<T extends string> {
  value: T
  label: string
  hint?: string
}

export function SegmentedControl<T extends string>({
  options,
  value,
  onChange,
  className,
}: {
  options: SegmentOption<T>[]
  value: T
  onChange: (value: T) => void
  className?: string
}) {
  return (
    <div
      role="tablist"
      className={cn(
        'inline-flex items-center gap-0.5 rounded-lg border border-line bg-paper-sunken p-0.5',
        className,
      )}
    >
      {options.map((option) => {
        const active = option.value === value
        return (
          <button
            key={option.value}
            type="button"
            role="tab"
            aria-selected={active}
            title={option.hint}
            onClick={() => onChange(option.value)}
            className={cn(
              'relative rounded-md px-3 py-1.5 text-xs transition-colors duration-150',
              active
                ? 'bg-surface-1 font-medium text-ink-1 shadow-e0'
                : 'text-ink-3 hover:text-ink-1',
            )}
          >
            {option.label}
          </button>
        )
      })}
    </div>
  )
}

/* ══════════════════════════════════════════════════════════════════════════
   进度
   ══════════════════════════════════════════════════════════════════════════ */

export function Progress({
  value,
  tone = 'moss',
  className,
  showTrack = true,
}: {
  /** 0–1 */
  value: number
  tone?: Tone
  className?: string
  showTrack?: boolean
}) {
  const clamped = Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0))
  return (
    <div
      role="progressbar"
      aria-valuenow={Math.round(clamped * 100)}
      aria-valuemin={0}
      aria-valuemax={100}
      className={cn(
        'h-1.5 w-full overflow-hidden rounded-full',
        showTrack && 'bg-paper-sunken',
        className,
      )}
    >
      <div
        className={cn('h-full rounded-full', TONE_DOT[tone])}
        style={{
          width: `${clamped * 100}%`,
          transition: 'width var(--dur-slower) var(--ease-out-expo)',
        }}
      />
    </div>
  )
}

/* ══════════════════════════════════════════════════════════════════════════
   提示（Tooltip）
   ══════════════════════════════════════════════════════════════════════════ */

export function Tooltip({
  label,
  children,
  side = 'top',
}: {
  label: string
  children: ReactNode
  side?: 'top' | 'bottom' | 'right'
}) {
  const [open, setOpen] = useState(false)
  const timer = useRef<number | null>(null)

  // 悬停 220ms 才出现 —— 鼠标划过不弹提示，是"安静"的关键
  const show = useCallback(() => {
    if (timer.current) window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setOpen(true), 220)
  }, [])

  const hide = useCallback(() => {
    if (timer.current) window.clearTimeout(timer.current)
    setOpen(false)
  }, [])

  useEffect(() => () => {
    if (timer.current) window.clearTimeout(timer.current)
  }, [])

  const position =
    side === 'top'
      ? 'bottom-full left-1/2 -translate-x-1/2 mb-2'
      : side === 'bottom'
        ? 'top-full left-1/2 -translate-x-1/2 mt-2'
        : 'left-full top-1/2 -translate-y-1/2 ml-2'

  return (
    <span
      className="relative inline-flex"
      onMouseEnter={show}
      onMouseLeave={hide}
      onFocus={show}
      onBlur={hide}
    >
      {children}
      {open && (
        <span
          role="tooltip"
          className={cn(
            'pointer-events-none absolute z-50 w-max max-w-[18rem] rounded-md px-2.5 py-1.5',
            'text-2xs leading-relaxed text-white',
            'animate-[breathe_0.001s]', // 触发一次极短动画，让出现不是"硬切"
            position,
          )}
          style={{
            backgroundColor: 'var(--color-ink-1)',
            boxShadow: 'var(--shadow-e3)',
            animation: 'none',
            opacity: 1,
          }}
        >
          {label}
        </span>
      )}
    </span>
  )
}

/* ══════════════════════════════════════════════════════════════════════════
   阅读进度环（Tutor 侧栏用）
   ══════════════════════════════════════════════════════════════════════════ */

export function Ring({
  value,
  size = 44,
  stroke = 3,
  tone = 'moss',
  children,
}: {
  value: number
  size?: number
  stroke?: number
  tone?: Tone
  children?: ReactNode
}) {
  const clamped = Math.max(0, Math.min(1, value))
  const radius = (size - stroke) / 2
  const circumference = 2 * Math.PI * radius

  return (
    <span className="relative inline-flex items-center justify-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          strokeWidth={stroke}
          stroke="var(--color-paper-sunken)"
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          strokeWidth={stroke}
          strokeLinecap="round"
          stroke={`var(--color-${tone === 'neutral' ? 'ink-4' : tone})`}
          strokeDasharray={circumference}
          strokeDashoffset={circumference * (1 - clamped)}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
          style={{ transition: 'stroke-dashoffset var(--dur-slower) var(--ease-out-expo)' }}
        />
      </svg>
      {children && (
        <span className="absolute inset-0 flex items-center justify-center">{children}</span>
      )}
    </span>
  )
}

/* ══════════════════════════════════════════════════════════════════════════
   开关
   ══════════════════════════════════════════════════════════════════════════ */

export function Switch({
  checked,
  onChange,
  label,
}: {
  checked: boolean
  onChange: (checked: boolean) => void
  label: string
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className={cn(
        'relative h-5 w-9 shrink-0 rounded-full border transition-colors duration-200',
        checked ? 'border-moss bg-moss' : 'border-line-strong bg-paper-sunken',
      )}
    >
      <span
        className="absolute top-0.5 h-3.5 w-3.5 rounded-full bg-white shadow-e0 transition-[left]"
        style={{
          left: checked ? '1.125rem' : '0.125rem',
          transitionDuration: 'var(--dur-normal)',
          transitionTimingFunction: 'var(--ease-out-quart)',
        }}
      />
    </button>
  )
}

/* ══════════════════════════════════════════════════════════════════════════
   选中标记（选项列表用）
   ══════════════════════════════════════════════════════════════════════════ */

export function CheckMark({ checked }: { checked: boolean }) {
  if (!checked) return null
  return (
    <span className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-moss text-white">
      <IconCheck size={11} />
    </span>
  )
}

/* ══════════════════════════════════════════════════════════════════════════
   主题
   ══════════════════════════════════════════════════════════════════════════ */

export type ThemeMode = 'light' | 'dark'

interface ThemeContextValue {
  mode: ThemeMode
  toggle: () => void
}

const ThemeContext = createContext<ThemeContextValue>({ mode: 'light', toggle: () => {} })

const THEME_KEY = 'shizhi:theme'

export function ThemeProvider({ children }: { children: ReactNode }) {
  /**
   * **默认亮色，不跟随系统。**
   *
   * 跟随 `prefers-color-scheme` 是常见做法，但这里刻意不用：
   * 产品的视觉身份（暖纸、衬线、纸上的批注感）是在亮色下建立的，
   * 如果首屏长什么样取决于用户系统怎么设，那这个身份就不成立了 ——
   * 演示时更不该出现"打开是黑的还是白的看运气"。
   *
   * 用户手动切过就记住他的选择。
   */
  const [mode, setMode] = useState<ThemeMode>(() => {
    const stored = window.localStorage.getItem(THEME_KEY)
    return stored === 'dark' ? 'dark' : 'light'
  })

  useEffect(() => {
    document.documentElement.dataset.theme = mode
    window.localStorage.setItem(THEME_KEY, mode)
  }, [mode])

  const value = useMemo<ThemeContextValue>(
    () => ({ mode, toggle: () => setMode((m) => (m === 'light' ? 'dark' : 'light')) }),
    [mode],
  )

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export const useTheme = () => useContext(ThemeContext)
