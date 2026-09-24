/**
 * 我的 —— 学习记录与偏好。
 *
 * ## 定位
 *
 * 不是"设置页"，是**给学习者看自己的地方**：
 * 我学了多少、我的讲法是怎么被决定的、这个账号是什么时候开始的。
 *
 * 所以语言一律用人话："你习惯先看对比再听解释" 而不是 "preferred_style=contrast"。
 *
 * ## 讲法偏好为什么要显式给"自动/手动"两个来源
 *
 * 系统会从错因里推导讲法，用户也能手动指定。用户手动指定之后**永不被覆盖** ——
 * 这件事必须让用户看得见，否则他会怀疑"我设了到底有没有用"。
 */

import { useCallback, useEffect, useState } from 'react'
import { motion } from 'motion/react'

import { ALL_STYLES, STYLE_LABEL, STYLE_SOURCE_LABEL, updateLearnerProfile } from '../../api/tutor'
import { useAuth } from '../../app/AuthProvider'
import { useLearning } from '../../app/LearningProvider'
import { Reveal } from '../../motion/primitives'
import {
  Badge,
  Button,
  Card,
  CheckMark,
  IconMoon,
  IconSun,
  Modal,
  SectionTitle,
  Switch,
  Skeleton,
  Tooltip,
  cn,
  useTheme,
  useToast,
} from '../../ui'
import { changePassword } from '../../api/auth'
import { messageOf } from '../../api/http'
import { validatePasswordChange } from '../../lib/passwordRules'
import { lastTrouble, masteryMark } from '../learn/voice'

/** 讲法偏好的说明。每一个都对应 Prompt 里一句**具体**的指令，不是空泛的形容 */
const STYLE_HINT: Record<string, string> = {
  balanced: '不预设，按内容自己选最合适的讲法',
  contrast: '把容易混的概念并排摆开，讲清边界',
  structured: '给能记住的框架、分类或口诀',
  stepwise: '把推导链一步一步补全',
  clarify: '先澄清问题到底在问什么',
}

export function ProfileView() {
  const { user } = useAuth()
  const { dashboard, reloadDashboard } = useLearning()
  const { mode, toggle } = useTheme()
  const toast = useToast()
  const [saving, setSaving] = useState<string | null>(null)

  // ── 改密码。
  // 后端 `POST /api/auth/password` 早就有了，缺的一直是入口。
  const [pwOpen, setPwOpen] = useState(false)
  const [oldPw, setOldPw] = useState('')
  const [newPw, setNewPw] = useState('')
  const [confirmPw, setConfirmPw] = useState('')
  const [pwBusy, setPwBusy] = useState(false)
  /** 三个格子一起显示/隐藏 —— 分三个开关只会让人逐个去点 ✓ */
  const [pwRevealed, setPwRevealed] = useState(false)

  const resetPasswordForm = useCallback(() => {
    setOldPw('')
    setNewPw('')
    setConfirmPw('')
    setPwBusy(false)
    // 关掉弹窗时把"明文显示"也收回 —— 下次打开不该还是明文 ✓
    setPwRevealed(false)
  }, [])

  /** 表单当前是否可提交。**规则在 `lib/passwordRules.ts`**（零依赖，有单测 ✓） */
  const passwordProblem = validatePasswordChange({
    oldPassword: oldPw,
    newPassword: newPw,
    confirmPassword: confirmPw,
  })

  const submitPassword = useCallback(async () => {
    if (validatePasswordChange({
      oldPassword: oldPw,
      newPassword: newPw,
      confirmPassword: confirmPw,
    })) {
      return
    }
    setPwBusy(true)
    try {
      await changePassword(oldPw, newPw)
      setPwOpen(false)
      resetPasswordForm()
      toast.success('密码已更新', '下次登录用新密码。')
    } catch (err) {
      // 旧密码不对等业务错误由后端给 detail，直接照实显示 ✓
      toast.error('没能改密码', messageOf(err))
      setPwBusy(false)
    }
  }, [oldPw, newPw, confirmPw, resetPasswordForm, toast])

  const profile = dashboard?.profile
  const overview = dashboard?.overview

  useEffect(() => {
    void reloadDashboard()
  }, [reloadDashboard])

  const choose = async (style: string) => {
    setSaving(style)
    try {
      await updateLearnerProfile(style)
      await reloadDashboard()
      toast.success(`讲法已改成「${STYLE_LABEL[style] ?? style}」`, '之后我都会按这个讲。')
    } catch {
      toast.error('没改成功', '稍后再试一次。')
    } finally {
      setSaving(null)
    }
  }

  return (
    <div className="space-y-8">
      <header>
        <Reveal>
          <p className="meta tracking-[0.14em]">我的</p>
          <h1 className="display mt-2.5">{user?.display_name ?? '同学'}</h1>
          <p className="mt-3 text-sm text-ink-3">
            @{user?.username}
            {user?.created_at && ` · 从 ${user.created_at.slice(0, 10)} 开始`}
          </p>
        </Reveal>
      </header>

      {/* ═══════════════════════════════════ 学习记录 */}
      <section className="space-y-3">
        <SectionTitle eyebrow="学习记录" title="你学到哪儿了" />

        <div className="grid gap-4 sm:grid-cols-3">
          {[
            {
              label: '开过的知识点',
              value: overview?.tracked ?? 0,
              note: '有作答记录的都算',
            },
            {
              label: '已经稳住的',
              value: overview?.mastered ?? 0,
              note: '能清楚讲出关键点',
            },
            {
              label: '还要补一补的',
              value: overview?.weak ?? 0,
              note: '下次会优先带你过',
            },
          ].map((item, index) => (
            <motion.div
              key={item.label}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.36, delay: index * 0.06, ease: [0.16, 1, 0.3, 1] }}
              className="surface p-5"
            >
              <p className="meta tracking-[0.12em]">{item.label}</p>
              <p className="mt-2 font-serif text-2xl text-ink-1 tnum">{item.value}</p>
              <p className="mt-1.5 text-2xs text-ink-3">{item.note}</p>
            </motion.div>
          ))}
        </div>

        {(dashboard?.weak_points?.length ?? 0) > 0 && (
          <Card padded={false} className="divide-y divide-line">
            {dashboard!.weak_points.slice(0, 6).map((item) => (
              <div key={item.knowledge_point_id} className="flex items-center gap-3 px-5 py-3">
                <span
                  aria-hidden="true"
                  className="h-1.5 w-1.5 shrink-0 rounded-full bg-sienna"
                />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-xs text-ink-1">{item.title}</p>
                  <p className="mt-0.5 text-2xs text-ink-3">
                    {masteryMark(item)} · {lastTrouble(item)}
                  </p>
                </div>
                <span className="meta shrink-0">
                  试过 {item.attempt_count} 次
                </span>
              </div>
            ))}
          </Card>
        )}
      </section>

      {/* ═══════════════════════════════════ 讲法偏好 */}
      <section className="space-y-3">
        <SectionTitle
          eyebrow="讲法偏好"
          title="你更喜欢我怎么讲"
          action={
            profile && (
              <Badge tone={profile.style_source === 'manual' ? 'moss' : 'neutral'} dot>
                {STYLE_SOURCE_LABEL[profile.style_source] ?? '系统判断'}
              </Badge>
            )
          }
        />

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {ALL_STYLES.map((style, index) => {
            const active = profile?.preferred_style === style
            return (
              <motion.button
                key={style}
                type="button"
                disabled={saving !== null}
                onClick={() => void choose(style)}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.34, delay: index * 0.05, ease: [0.16, 1, 0.3, 1] }}
                className={cn(
                  'surface surface-interactive relative p-4 text-left disabled:opacity-60',
                  active && 'border-moss-line',
                )}
              >
                <div className="flex items-start justify-between gap-3">
                  <span
                    className={cn(
                      'text-sm',
                      active ? 'font-medium text-moss-ink' : 'text-ink-1',
                    )}
                  >
                    {STYLE_LABEL[style] ?? style}
                  </span>
                  <CheckMark checked={active} />
                </div>
                <p className="mt-1.5 text-2xs leading-relaxed text-ink-3">
                  {STYLE_HINT[style] ?? ''}
                </p>
                {saving === style && (
                  <span className="meta absolute bottom-3 right-4">正在设定…</span>
                )}
              </motion.button>
            )
          })}
        </div>

        <p className="text-2xs leading-relaxed text-ink-3">
          你手动选定之后，系统就不会再按自己的判断去改了 ——
          除非你换回来。
        </p>
      </section>

      {/* ═══════════════════════════════════ 外观与账号 */}
      <section className="space-y-3">
        <SectionTitle eyebrow="外观与账号" title="设定" />

        <Card padded={false} className="divide-y divide-line">
          <div className="flex items-center justify-between gap-4 px-5 py-4">
            <div className="flex items-center gap-3">
              <span className="text-ink-3">
                {mode === 'light' ? <IconMoon size={16} /> : <IconSun size={16} />}
              </span>
              <div>
                <p className="text-xs text-ink-1">夜间模式</p>
                <p className="mt-0.5 text-2xs text-ink-3">
                  暗一点的底色，晚上看久了眼睛没那么累
                </p>
              </div>
            </div>
            <Switch checked={mode === 'dark'} onChange={toggle} label="夜间模式" />
          </div>

          <div className="flex items-center justify-between gap-4 px-5 py-4">
            <div>
              <p className="text-xs text-ink-1">账号</p>
              <p className="mt-0.5 text-2xs text-ink-3">
                你的学习记录记在这个账号下，换台电脑登录也还在
              </p>
            </div>
            <Tooltip label="账号信息由服务端保管" side="top">
              <span className="meta">@{user?.username}</span>
            </Tooltip>
          </div>

          <div className="flex items-center justify-between gap-4 px-5 py-4">
            <div>
              <p className="text-xs text-ink-1">密码</p>
              <p className="mt-0.5 text-2xs text-ink-3">
                改完下一次登录生效，当前这台不用重新登
              </p>
            </div>
            <Button variant="quiet" size="sm" onClick={() => setPwOpen(true)}>
              修改密码
            </Button>
          </div>
        </Card>

        <Modal
          open={pwOpen}
          onClose={() => {
            setPwOpen(false)
            resetPasswordForm()
          }}
          title="修改密码"
          description="先填当前的，再填两遍新的。"
          footer={
            <div className="flex justify-end gap-2">
              <Button
                size="sm"
                onClick={() => {
                  setPwOpen(false)
                  resetPasswordForm()
                }}
              >
                算了
              </Button>
              <Button
                variant="primary"
                size="sm"
                disabled={passwordProblem !== null}
                loading={pwBusy}
                onClick={() => void submitPassword()}
              >
                确认修改
              </Button>
            </div>
          }
        >
          <div className="space-y-3">
            <PasswordField
              label="当前密码"
              value={oldPw}
              onChange={setOldPw}
              autoComplete="current-password"
              revealed={pwRevealed}
            />
            <PasswordField
              label="新密码"
              value={newPw}
              onChange={setNewPw}
              autoComplete="new-password"
              revealed={pwRevealed}
            />
            <PasswordField
              label="再输一遍新密码"
              value={confirmPw}
              onChange={setConfirmPw}
              autoComplete="new-password"
              revealed={pwRevealed}
            />
            <button
              type="button"
              onClick={() => setPwRevealed((prev) => !prev)}
              aria-pressed={pwRevealed}
              className="text-2xs text-ink-3 underline decoration-line-strong underline-offset-2 hover:text-ink-1"
            >
              {pwRevealed ? '隐藏密码' : '显示密码'}
            </button>
            {/* 只在用户已经输过东西之后才提示 —— 一打开就报错很烦 */}
            {passwordProblem && (oldPw || newPw || confirmPw) && (
              <p className="text-2xs text-brick-ink">{passwordProblem}</p>
            )}
          </div>
        </Modal>
      </section>

      {!dashboard && (
        <div className="space-y-3">
          <Skeleton width="12rem" height={14} />
          <Skeleton width="100%" height={90} />
        </div>
      )}
    </div>
  )
}

/**
 * 弹窗里的一个密码输入格。
 *
 * 局部组件，不往 `src/ui` 里加 —— 只有这一处用得到，
 * 抽成公共组件就是"为一次使用建一个 API" ✗
 * （与 `TodoView` 里那几个局部组件同一个理由 ✓）
 */
function PasswordField({
  label,
  value,
  onChange,
  autoComplete,
  revealed,
}: {
  label: string
  value: string
  onChange: (next: string) => void
  autoComplete: string
  /** 明文显示。三个格子一起切 —— 单独切容易看不出哪个是明文 ✓ */
  revealed: boolean
}) {
  return (
    <label className="block">
      <span className="text-2xs text-ink-4">{label}</span>
      <input
        // 切换 type 而不是切换样式 —— 这是唯一真正让浏览器不遮蔽输入的方式 ✓
        type={revealed ? 'text' : 'password'}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        autoComplete={autoComplete}
        aria-label={label}
        className="mt-1 w-full rounded-lg border border-line-strong bg-surface-1 px-2.5 py-1.5 text-sm text-ink-1 outline-none focus:border-moss-line"
      />
    </label>
  )
}
