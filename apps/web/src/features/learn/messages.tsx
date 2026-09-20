/**
 * Tutor 对话区的组成部分。
 *
 * ## 三件事都必须让学习者一眼看到
 *
 *   1. **我要回答什么** —— 任务卡把问题、要点、回答方式分开摆
 *   2. **我答得怎么样** —— 反馈分四段：对在哪 / 缺什么 / 为什么 / 接下来做什么
 *   3. **现在发生了什么** —— 等待时的状态提示说人话，不报工具名
 *
 * ## 一条贯穿全文的取舍
 *
 * 助教的输出是**模型生成的散文**。要把它变成结构化界面，
 * 就会有"解析失败"的风险。这里所有结构化渲染都遵守同一条规则：
 *
 *   **能拆就拆，拆不动就原样显示散文。** 绝不让学习者看到空白或错位的框。
 */

import { useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'

import type { TutorAssessment, TutorTurn } from '../../api/tutor'
import { useMotion } from '../../motion/primitives'
import {
  Badge,
  Button,
  IconArrowUpRight,
  IconCheck,
  IconChevronDown,
  IconCompass,
  IconLightbulb,
  IconSend,
  Pulse,
  cn,
} from '../../ui'
import { parseTaskCard, stripInlineMarkup } from './taskCard'
import { assessmentWords, feedbackHeadline } from './voice'

/* ══════════════════════════════════════════════════════════════════════════
   助教头像与消息骨架
   ══════════════════════════════════════════════════════════════════════════ */

export function TutorAvatar({ active = false }: { active?: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        'relative mt-0.5 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-full transition-shadow duration-500',
        active ? 'bg-moss text-white' : 'bg-moss-soft text-moss',
      )}
      style={active ? { boxShadow: 'var(--glow-moss)' } : undefined}
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
        <path
          d="M6 3.5h12a1.5 1.5 0 0 1 1.5 1.5v15.2a.6.6 0 0 1-.94.5L12 16.4l-6.56 4.3a.6.6 0 0 1-.94-.5V5A1.5 1.5 0 0 1 6 3.5Z"
          fill="currentColor"
        />
      </svg>
    </span>
  )
}

/* ══════════════════════════════════════════════════════════════════════════
   教学任务卡
   ══════════════════════════════════════════════════════════════════════════ */

function TaskRow({
  label,
  children,
  tone = 'neutral',
}: {
  label: string
  children: React.ReactNode
  tone?: 'neutral' | 'moss'
}) {
  return (
    <div className="flex gap-3.5">
      <span
        className={cn(
          'mt-0.5 w-[4.5rem] shrink-0 text-2xs font-medium tracking-[0.1em]',
          tone === 'moss' ? 'text-moss' : 'text-ink-4',
        )}
      >
        {label}
      </span>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  )
}

/**
 * 任务卡。
 *
 * 结构对照学习者心里的三个问题：
 *   「问什么」→ 问题区（最大字号，视觉重心）
 *   「答到什么程度」→ 要点清单（编号圆点）
 *   「怎么答」→ 回答方式（贴士样式，弱化）
 */
export function TaskCard({
  content,
  onStart,
  compact = false,
}: {
  content: string
  onStart?: () => void
  compact?: boolean
}) {
  const card = parseTaskCard(content)
  const [showAll, setShowAll] = useState(false)

  // 解析不出来 → 原样渲染散文。**这是设计的兜底，不是失败**
  if (!card) {
    return (
      <div className="space-y-1">
        {content.split('\n').map((line, index) =>
          line.trim() === '' ? (
            <div key={index} className="h-2" />
          ) : (
            <p key={index} className="tutor-voice">
              {stripInlineMarkup(line)}
            </p>
          ),
        )}
      </div>
    )
  }

  // 兜底行由解析函数自己产出 —— 不再另算一遍
  // （上一版正是在这里对不齐：拿整段拼接串当已覆盖、却用单行比对，
  //   于是每一行都判成没覆盖，又被渲染了一遍 —— 那就是内容重复的来源）
  const leftover = card.leftover
  const visiblePoints = showAll ? card.points : card.points.slice(0, 4)
  const hiddenCount = card.points.length - visiblePoints.length

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.42, ease: [0.16, 1, 0.3, 1] }}
      className={cn(
        'surface relative overflow-hidden',
        compact ? 'p-4' : 'p-5',
      )}
    >
      {/* 左缘一道主色，表示"这是一个待完成的任务" */}
      <span aria-hidden="true" className="absolute inset-y-0 left-0 w-[3px] bg-moss" />

      <div className={cn('space-y-4', compact ? 'pl-2' : 'pl-3')}>
        {/* ── 引导语：铺垫讲解 + 为什么问这道题。
                **逐行渲染**：拼成一整段的话 HTML 会把换行折掉，
                「是什么 / 为什么 / 怎么用」三个小标题会糊成一大坨。 */}
        {card.leadLines.length > 0 && (
          <div className="space-y-1.5">
            {card.leadLines.map((line, index) => (
              <p
                key={`${index}-${line.slice(0, 12)}`}
                className="text-xs leading-relaxed text-ink-3"
              >
                {line}
              </p>
            ))}
          </div>
        )}

        {/* ── 问题：视觉重心 */}
        <TaskRow label="现在的问题" tone="moss">
          <p className="font-serif text-base leading-relaxed text-ink-1">
            {card.question}
          </p>
        </TaskRow>

        {/* ── 要点 */}
        {card.points.length > 0 && (
          <TaskRow label="你需要回答">
            <ul className="space-y-1.5">
              {visiblePoints.map((point, index) => (
                <motion.li
                  key={point}
                  initial={{ opacity: 0, x: -4 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ duration: 0.3, delay: 0.1 + index * 0.06 }}
                  className="flex gap-2.5 text-sm leading-relaxed text-ink-2"
                >
                  <span className="mt-1.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-moss-soft text-2xs text-moss-ink">
                    {index + 1}
                  </span>
                  <span className="min-w-0">{point}</span>
                </motion.li>
              ))}
            </ul>
            {hiddenCount > 0 && (
              <button
                type="button"
                onClick={() => setShowAll(true)}
                className="mt-2 text-2xs text-ink-3 transition-colors hover:text-ink-1"
              >
                还有 {hiddenCount} 点没显示，展开看看
              </button>
            )}
          </TaskRow>
        )}

        {/* ── 回答方式 */}
        {card.howTo && (
          <TaskRow label="怎么答">
            <div className="flex items-start gap-2 rounded-md bg-paper-sunken px-3 py-2">
              <span className="mt-0.5 shrink-0 text-sienna">
                <IconLightbulb size={13} />
              </span>
              <p className="text-xs leading-relaxed text-ink-2">{card.howTo}</p>
            </div>
          </TaskRow>
        )}

        {/* 解析没覆盖到的行（理论上为空，兜住漏句） */}
        {leftover.length > 0 && (
          <p className="text-xs leading-relaxed text-ink-3">{leftover.join(' ')}</p>
        )}

        {card.tail && <p className="text-xs leading-relaxed text-ink-3">{card.tail}</p>}

        {onStart && (
          <Button variant="primary" size="sm" iconEnd={<IconArrowUpRight size={13} />} onClick={onStart}>
            开始作答
          </Button>
        )}
      </div>
    </motion.div>
  )
}

/* ══════════════════════════════════════════════════════════════════════════
   助教消息
   ══════════════════════════════════════════════════════════════════════════ */

export function TutorMessage({ turn, onStart }: { turn: TutorTurn; onStart?: () => void }) {
  const body = turn.content.trim()
  // 只要这一轮结尾是要作答，就把它渲染成任务卡
  const isTask = parseTaskCard(body) !== null

  return (
    <div className="flex gap-3">
      <TutorAvatar />
      <div className="min-w-0 flex-1 pt-0.5">
        {isTask ? (
          <TaskCard content={body} onStart={onStart} />
        ) : (
          <div className="space-y-1">
            {body.split('\n').map((line, index) =>
              line.trim() === '' ? (
                <div key={index} className="h-2" />
              ) : (
                <p key={index} className="tutor-voice">
                  {/* 教学内容是当纯文本渲染的，行内 Markdown 标记只会碍眼 */}
                  {stripInlineMarkup(line)}
                </p>
              ),
            )}
          </div>
        )}

        {turn.sources.length > 0 && <Sources turn={turn} />}
      </div>
    </div>
  )
}

/** 依据的资料。只说"来自你的哪几份资料"，不报相似度数字 */
function Sources({ turn }: { turn: TutorTurn }) {
  const [open, setOpen] = useState(false)
  const files = Array.from(new Set(turn.sources.map((s) => s.file_name)))

  return (
    <div className="mt-3">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="meta inline-flex items-center gap-1.5 transition-colors hover:text-ink-2"
      >
        <IconCompass size={12} />
        依据 {turn.sources.length} 条资料
        <IconChevronDown
          size={11}
          className={cn('transition-transform duration-200', open && 'rotate-180')}
        />
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.ul
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.24, ease: [0.16, 1, 0.3, 1] }}
            className="mt-2 space-y-1 overflow-hidden border-l-2 border-line pl-3"
          >
            {files.map((file) => (
              <li key={file} className="truncate text-2xs text-ink-3">
                {file}
              </li>
            ))}
          </motion.ul>
        )}
      </AnimatePresence>
    </div>
  )
}

/* ══════════════════════════════════════════════════════════════════════════
   作答反馈
   ══════════════════════════════════════════════════════════════════════════ */

/**
 * 反馈四段：对在哪 / 缺什么 / 为什么 / 接下来。
 *
 * 顺序是刻意的 —— **先说对的，再说缺的**。
 * 反过来会让人一进门就看到"你错了"，后面的解释都不太读得进去。
 */
export function AssessmentFeedback({
  assessment,
  nextStep,
}: {
  assessment: TutorAssessment
  nextStep?: string
}) {
  const enabled = useMotion()
  const correct = assessment.correct

  return (
    <motion.div
      initial={enabled ? { opacity: 0, y: -6 } : false}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.36, ease: [0.16, 1, 0.3, 1] }}
      className={cn(
        'mb-5 rounded-lg border px-4 py-3.5',
        correct ? 'border-moss-line bg-moss-soft/50' : 'border-sienna-line bg-sienna-soft/50',
      )}
    >
      {/* 结论行 */}
      <div className="flex flex-wrap items-center gap-2.5">
        <span
          className={cn(
            'inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full',
            correct ? 'bg-moss text-white' : 'bg-sienna text-white',
          )}
        >
          {correct ? <IconCheck size={12} /> : <span className="text-2xs font-medium">!</span>}
        </span>
        <span className={cn('text-sm font-medium', correct ? 'text-moss-ink' : 'text-sienna-ink')}>
          {feedbackHeadline(assessment)}
        </span>
        <span className="text-xs text-ink-3">{assessmentWords(assessment)}</span>
      </div>

      {/* 指对在哪 / 为什么 */}
      {assessment.feedback && (
        <p className="mt-2.5 text-xs leading-relaxed text-ink-2">{assessment.feedback}</p>
      )}

      {/* 缺什么 */}
      {assessment.missing_points.length > 0 && (
        <div className="mt-2.5">
          <p className="text-2xs font-medium tracking-[0.1em] text-ink-3">没说到</p>
          <ul className="mt-1 space-y-1">
            {assessment.missing_points.map((point) => (
              <li key={point} className="flex gap-2 text-xs leading-relaxed text-ink-2">
                <span aria-hidden="true" className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-ink-4" />
                {point}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* 偏在哪 */}
      {assessment.misunderstood_points.length > 0 && (
        <div className="mt-2.5">
          <p className="text-2xs font-medium tracking-[0.1em] text-ink-3">理解偏了</p>
          <ul className="mt-1 space-y-1">
            {assessment.misunderstood_points.map((point) => (
              <li key={point} className="flex gap-2 text-xs leading-relaxed text-ink-2">
                <span aria-hidden="true" className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-brick" />
                {point}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* 接下来 */}
      {nextStep && (
        <p className="mt-3 border-t border-line/70 pt-2.5 text-xs leading-relaxed text-ink-2">
          <span className="text-ink-3">接下来：</span>
          {nextStep}
        </p>
      )}
    </motion.div>
  )
}

/* ══════════════════════════════════════════════════════════════════════════
   学习者的回答
   ══════════════════════════════════════════════════════════════════════════ */

export function LearnerBubble({ text }: { text: string }) {
  return (
    <div className="mb-5 flex justify-end">
      <div className="max-w-[80%] rounded-lg rounded-tr-sm bg-paper-sunken px-3.5 py-2.5">
        <p className="learner-voice whitespace-pre-wrap">{text}</p>
      </div>
    </div>
  )
}

/* ══════════════════════════════════════════════════════════════════════════
   进行中
   ══════════════════════════════════════════════════════════════════════════ */

export function StreamingTurn({ text, stage }: { text: string; stage: string | null }) {
  return (
    <div className="flex gap-3">
      <TutorAvatar active />
      <div className="min-w-0 flex-1 pt-0.5">
        {text ? (
          <>
            <p className="tutor-voice whitespace-pre-wrap">
              {text}
              <span
                aria-hidden="true"
                className="ml-0.5 inline-block h-4 w-[2px] translate-y-0.5 animate-pulse bg-moss align-middle"
              />
            </p>
            {stage && <p className="meta mt-2">{stage}</p>}
          </>
        ) : (
          <p className="flex items-center gap-2.5 text-sm text-ink-3">
            <span className="flex gap-1 text-ink-4">
              <Pulse delay={0} />
              <Pulse delay={0.16} />
              <Pulse delay={0.32} />
            </span>
            {stage ?? '助教在想…'}
          </p>
        )}
      </div>
    </div>
  )
}

/* ══════════════════════════════════════════════════════════════════════════
   作答区
   ══════════════════════════════════════════════════════════════════════════ */

export function Composer({
  value,
  onChange,
  onSubmit,
  busy,
  disabled,
  inputRef,
}: {
  value: string
  onChange: (value: string) => void
  onSubmit: () => void
  busy: boolean
  disabled?: boolean
  /** 暴露给上层 —— 任务卡上的「开始作答」要把光标送到这里 */
  inputRef?: React.RefObject<HTMLTextAreaElement>
}) {
  const canSubmit = value.trim().length > 0 && !busy && !disabled

  return (
    <div className="shrink-0 border-t border-line pt-4">
      <div
        className={cn(
          'rounded-lg border bg-surface-1 transition-shadow duration-300',
          busy ? 'border-moss-line' : 'border-line-strong',
        )}
        style={busy ? { boxShadow: 'var(--glow-moss)' } : undefined}
      >
        <textarea
          ref={inputRef}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            // ⌘/Ctrl + Enter 提交。单独 Enter 换行 —— 回答经常要分点写
            if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
              event.preventDefault()
              if (canSubmit) onSubmit()
            }
          }}
          disabled={busy || disabled}
          rows={3}
          placeholder="用你自己的话回答，不用写得很正式"
          className="w-full resize-none bg-transparent px-3.5 py-3 text-sm leading-relaxed text-ink-1 outline-none placeholder:text-ink-4 disabled:opacity-60"
        />

        <div className="flex items-center justify-between gap-3 border-t border-line/70 px-3.5 py-2">
          <p className="meta truncate">
            回答后我会看你答到了哪几点，再决定是继续追问、换个讲法，还是提高难度。
          </p>
          <div className="flex shrink-0 items-center gap-2.5">
            <span className="meta hidden sm:inline">⌘/Ctrl + Enter</span>
            <Button
              variant="primary"
              size="sm"
              disabled={!canSubmit}
              loading={busy}
              icon={busy ? undefined : <IconSend size={13} />}
              onClick={onSubmit}
            >
              {busy ? '在看' : '提交'}
            </Button>
          </div>
        </div>
      </div>
    </div>
  )
}

/* ══════════════════════════════════════════════════════════════════════════
   小工具
   ══════════════════════════════════════════════════════════════════════════ */

export { Badge }
