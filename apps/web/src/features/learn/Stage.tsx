/**
 * 辅导 —— Tutor 教学主舞台。
 *
 * ## 这一页的设计目标只有一个
 *
 * 让学习者在**任何一刻**都清楚三件事：
 *   我在学什么 · 我现在要做什么 · 做完之后会怎样
 *
 * 所以顶栏常驻"正在学哪个知识点 + 到什么程度"，问题渲染成任务卡，
 * 作答框下面写明"回答后会发生什么"。
 *
 * ## 空态不是"空"
 *
 * 没在学时这一页不显示空白，而是显示**可以学什么** ——
 * 从资料里读出的知识点按章节排开，点一个就开始。
 * 这样"辅导"这一页永远有内容，也顺便承担了"选知识点"的职责。
 */

import { useMemo, useRef, useState } from 'react'
import { motion } from 'motion/react'

import { useLearning } from '../../app/LearningProvider'
import { Reveal } from '../../motion/primitives'
import {
  Button,
  ErrorState,
  IconArrowRight,
  IconCompass,
  IconLayers,
  IconRefresh,
  Ring,
} from '../../ui'
import {
  AssessmentFeedback,
  Composer,
  LearnerBubble,
  StreamingTurn,
  TutorMessage,
} from './messages'
import { PointPicker } from './PointPicker'
import { greeting, lastTrouble, nextStepWords, pointStatusWords } from './voice'

/* ------------------------------------------------------------------ 顶栏 */

/** 正在学什么 + 到什么程度。常驻在对话上方，滚动时不参与滚动 */
function SessionHeader({
  title,
  mastery,
  statusWords,
  trouble,
  onPick,
  onRestart,
}: {
  title: string
  mastery: number
  statusWords: string
  trouble: string
  onPick: () => void
  onRestart: () => void
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-4 border-b border-line pb-4">
      <div className="flex min-w-0 items-center gap-4">
        <Ring value={mastery} size={44} stroke={3.5}>
          <span className="text-2xs text-moss-ink">{Math.round(mastery * 100)}%</span>
        </Ring>
        <div className="min-w-0">
          <p className="meta tracking-[0.12em]">正在学</p>
          <h1 className="truncate text-lg leading-snug">{title}</h1>
          <p className="mt-0.5 truncate text-2xs text-ink-3">
            {statusWords}
            {trouble && ` · ${trouble}`}
          </p>
        </div>
      </div>

      <div className="flex shrink-0 items-center gap-2">
        <Button variant="ghost" size="sm" icon={<IconLayers size={14} />} onClick={onPick}>
          换一个
        </Button>
        <Button variant="ghost" size="sm" icon={<IconRefresh size={14} />} onClick={onRestart}>
          重新开始
        </Button>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ 页面 */

export function Stage() {
  const {
    turn,
    history,
    answers,
    busy,
    stage,
    streaming,
    focus,
    state,
    error,
    submitAnswer,
    clearError,
    startLearning,
    endSession,
  } = useLearning()

  const [draft, setDraft] = useState('')
  //: 作答框的引用。任务卡上的「开始作答」要把光标送进去 ——
  //: 那个按钮如果什么都不做，会被当成"坏了"。它该做的是**把注意力接过去**。
  const answerRef = useRef<HTMLTextAreaElement>(null)

  const focusAnswer = () => {
    const el = answerRef.current
    if (!el) return
    el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    el.focus()
  }
  const [pickerOpen, setPickerOpen] = useState(false)

  const onSubmit = () => {
    const text = draft.trim()
    if (!text) return
    void submitAnswer(text)
    setDraft('')
  }

  // 会话里的最后一轮决定"现在该干什么"
  const displayTurn = turn

  const header = useMemo(() => {
    if (!displayTurn) return null
    return (
      <SessionHeader
        title={focus?.title ?? ''}
        mastery={displayTurn.state_after.mastery}
        statusWords={pointStatusWords(state ?? displayTurn.state_after)}
        trouble={lastTrouble({
          attempt_count: state?.attempt_count ?? displayTurn.state_after.attempt_count,
          consecutive_wrong: state?.consecutive_wrong ?? displayTurn.state_after.consecutive_wrong,
          last_error_type: state?.last_error_type ?? displayTurn.state_after.last_error_type,
        })}
        onPick={() => setPickerOpen(true)}
        onRestart={() => {
          if (focus) void startLearning({ ...focus })
        }}
      />
    )
  }, [displayTurn, focus, state, startLearning])

  /* ─────────────────────────────────────────── 没在学：选一个开始 */
  if (!displayTurn) {
    return (
      <>
        <PointPicker open={pickerOpen} onClose={() => setPickerOpen(false)} />
        <div className="space-y-7">
          <header>
            <Reveal>
              <p className="meta tracking-[0.14em]">辅导</p>
              <h1 className="display mt-2.5">{greeting()}，今天想弄懂什么？</h1>
              <p className="mt-3 max-w-[var(--size-reading)] text-sm leading-relaxed text-ink-2">
                挑一个知识点，我先讲清楚它是什么、为什么要这么理解，
                然后问你几个问题 —— 目的不是考你，是看看哪一步还没通。
              </p>
            </Reveal>
          </header>
          <PointPicker open onClose={() => {}} embedded />
        </div>
      </>
    )
  }

  return (
    <>
      <PointPicker open={pickerOpen} onClose={() => setPickerOpen(false)} />

      <div className="flex h-full min-h-0 flex-col gap-5">
        {header}

        {error && (
          <ErrorState
            title="这一步没走通"
            message={error}
            onRetry={clearError}
            retryLabel="知道了"
          />
        )}

        {/* ───────────────────────────────── 对话流 */}
        <div className="min-h-0 flex-1 space-y-7 overflow-y-auto pr-1">
          {history.map((item, index) => (
            <article key={index}>
              {answers[index] && <LearnerBubble text={answers[index]} />}
              {item.assessment && (
                <AssessmentFeedback
                  assessment={item.assessment}
                  nextStep={nextStepWords(item.decision.rule, item.action)}
                />
              )}
              <TutorMessage turn={item} onStart={focusAnswer} />
            </article>
          ))}

          {busy && <StreamingTurn text={streaming} stage={stage} />}
        </div>

        {/* ───────────────────────────────── 作答 / 收口 */}
        {displayTurn.session_finished ? (
          <motion.div
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            className="shrink-0 border-t border-line pt-4"
          >
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div className="flex items-center gap-3">
                <span className="text-moss">
                  <IconCompass size={18} />
                </span>
                <div>
                  <p className="text-sm text-ink-1">这个知识点已经收口了</p>
                  <p className="mt-0.5 text-xs text-ink-3">
                    到这里你算是讲得清楚了。要不要试试下面这个？
                  </p>
                </div>
              </div>
              <div className="flex gap-2">
                <Button variant="quiet" size="sm" onClick={endSession}>
                  先歇一下
                </Button>
                <Button
                  variant="primary"
                  size="sm"
                  iconEnd={<IconArrowRight size={13} />}
                  onClick={() => setPickerOpen(true)}
                >
                  学下一个
                </Button>
              </div>
            </div>
          </motion.div>
        ) : (
          <Composer
            value={draft}
            onChange={setDraft}
            onSubmit={onSubmit}
            busy={busy}
            inputRef={answerRef}
          />
        )}
      </div>
    </>
  )
}
