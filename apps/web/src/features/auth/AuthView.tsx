/**
 * 登录 / 注册。
 *
 * ## 从"登记卡"改成"左图右表单"
 *
 * v1 是一张居中的读者登记卡 —— 好看，但它是**应用内**的语汇：
 * 一张表单孤零零摆在屏幕中央，第一眼传达的是"请先办事"。
 *
 * 用户第一次打开这个产品时，应该先看到**这是个什么东西**。
 * 所以改成左右分栏：左边是产品的样子（一张活的、会生长的知识网络），
 * 右边才是表单。表单本身仍然保留登记卡的细节（书脊、账本式页签），
 * 只是不再独自霸占整个屏幕。
 *
 * ## 左边的网络为什么是"活的"
 *
 * 节点逐个出现、连线缓慢流动 —— 这正对应产品的实际行为：
 * 你传一份资料，它读出知识点、建起关系、越长越大。
 * **不是装饰性的粒子特效**（那种东西和产品没关系），而是产品机制的可视化。
 *
 * 它同时也是背景：真正的主角是右边的表单，左侧网络的亮度压得很低。
 */

import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { motion, useReducedMotion } from 'motion/react'

import { validatePassword, validateUsername } from '../../api/auth'
import { useAuth } from '../../app/AuthProvider'
import { BrandMark } from '../../components/BrandMark'
import { Reveal } from '../../motion/primitives'
import { Button, IconCheck, cn } from '../../ui'

type Mode = 'login' | 'register'

/* ══════════════════════════════════════════════════════════════════════════
   左侧：知识网络
   ══════════════════════════════════════════════════════════════════════════ */

interface Node {
  id: number
  x: number
  y: number
  r: number
  delay: number
  hub: boolean
}

/**
 * 网络节点与连线。
 *
 * ⚠️ **所有节点必须落在 y 18–78 之间。**
 *
 * SVG 用 `preserveAspectRatio="slice"`：容器比 viewBox 扁时，
 * 它会等比放大到"盖满"再把上下裁掉。实测在常见的分栏比例下，
 * 可见的纵向范围大约是 viewBox 的 18–78 —— 摆在外面的节点**根本不会显示**。
 * 第一版就踩了这个：画了 13 个节点，屏幕上只剩中间那几个大的。
 *
 * 用固定坐标而不是随机：随机意味着每次刷新布局都不同，
 * 而"同一张图"才有品牌识别度。
 */
function buildNetwork(): { nodes: Node[]; edges: [number, number][] } {
  const nodes: Node[] = []

  // 三个枢纽：模拟"章节"，它们之间的连线构成骨架
  const hubs = [
    { x: 30, y: 30, r: 2.4 },
    { x: 68, y: 38, r: 2.0 },
    { x: 42, y: 62, r: 2.2 },
  ]
  hubs.forEach((hub, index) => {
    nodes.push({ id: index, x: hub.x, y: hub.y, r: hub.r, delay: index * 0.2, hub: true })
  })

  // 卫星：知识点。围绕枢纽铺开，密度比第一版高得多 ——
  // 三五颗看不出"网络"，二十几颗才有群星的感觉
  const satellites: [number, number, number][] = [
    [14, 22, 0.85], [46, 24, 0.7], [58, 27, 0.75], [84, 30, 0.8], [24, 42, 0.75],
    [40, 44, 0.68], [55, 46, 0.72], [78, 48, 0.78], [88, 40, 0.65], [12, 55, 0.7],
    [28, 58, 0.66], [56, 60, 0.74], [72, 62, 0.68], [86, 58, 0.72], [18, 70, 0.7],
    [34, 72, 0.64], [52, 74, 0.7], [66, 70, 0.66], [80, 74, 0.62], [94, 66, 0.6],
  ]
  satellites.forEach(([x, y, r], index) => {
    nodes.push({ id: 100 + index, x, y, r, delay: 0.55 + index * 0.05, hub: false })
  })

  // 连线：枢纽之间 + 每个枢纽连自己的几颗卫星
  const edges: [number, number][] = [
    [0, 1], [1, 2], [0, 2],
    [0, 100], [0, 101], [0, 104], [0, 105], [0, 109],
    [1, 102], [1, 103], [1, 106], [1, 107], [1, 110], [1, 114],
    [2, 111], [2, 112], [2, 113], [2, 115], [2, 116], [2, 117], [2, 118], [2, 119],
  ]
  return { nodes, edges }
}

function KnowledgeBackdrop() {
  const { nodes, edges } = useMemo(buildNetwork, [])
  const still = useReducedMotion()
  const byId = useMemo(() => new Map(nodes.map((node) => [node.id, node])), [nodes])

  return (
    <svg
      viewBox="0 0 100 100"
      preserveAspectRatio="xMidYMid slice"
      className="absolute inset-0 h-full w-full"
      aria-hidden="true"
    >
      <defs>
        <radialGradient id="nodeGlow" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="var(--color-moss)" stopOpacity="0.5" />
          <stop offset="100%" stopColor="var(--color-moss)" stopOpacity="0" />
        </radialGradient>
      </defs>

      {/* 连线：先画，压在节点下面。虚线缓慢流动，像"知识在连起来" */}
      {edges.map(([from, to], index) => {
        const a = byId.get(from)
        const b = byId.get(to)
        if (!a || !b) return null
        return (
          <motion.line
            key={`${from}-${to}`}
            x1={a.x}
            y1={a.y}
            x2={b.x}
            y2={b.y}
            stroke="var(--color-moss)"
            strokeWidth={0.11}
            strokeDasharray="1.2 1.6"
            initial={{ opacity: 0 }}
            animate={{ opacity: still ? 0.24 : 0.3 }}
            transition={{ duration: 0.9, delay: 0.5 + index * 0.08 }}
            style={
              still
                ? undefined
                : { animation: `flow 3.4s linear ${index * 0.18}s infinite` }
            }
          />
        )
      })}

      {/* 节点 */}
      {nodes.map((node) => (
        <motion.g
          key={node.id}
          initial={{ opacity: 0, scale: 0.4 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ duration: 0.6, delay: node.delay, ease: [0.16, 1, 0.3, 1] }}
          style={{ transformOrigin: `${node.x}px ${node.y}px` }}
        >
          {node.hub && (
            <motion.circle
              cx={node.x}
              cy={node.y}
              r={node.r * 4.2}
              fill="url(#nodeGlow)"
              animate={still ? undefined : { opacity: [0.5, 0.85, 0.5] }}
              transition={{ duration: 4.2, repeat: Infinity, ease: 'easeInOut' }}
            />
          )}
          <circle
            cx={node.x}
            cy={node.y}
            r={node.r}
            fill={node.hub ? 'var(--color-moss)' : 'var(--color-paper-raised)'}
            stroke="var(--color-moss)"
            strokeWidth={0.18}
            opacity={node.hub ? 0.9 : 0.85}
          />
        </motion.g>
      ))}
    </svg>
  )
}

/** 左侧的文案区：说清这是什么产品，不喊口号 */
function Pitch() {
  const points = [
    '把你上传的讲义、教材、笔记拆成知识点',
    '每个结论都回溯得到原文页码，不凭空编',
    '看你答到哪一步，再决定是追问还是换讲法',
  ]

  return (
    <div className="relative z-10 max-w-[26rem]">
      <div className="flex items-center gap-3">
        <BrandMark size={26} />
        <div>
          <p className="font-serif text-xl leading-none text-ink-1">拾知</p>
          <p className="meta mt-1.5">你的私人学习伙伴</p>
        </div>
      </div>

      <h2 className="display mt-9 text-3xl">
        不是问答框，
        <br />
        是会读你资料的助教
      </h2>

      <ul className="mt-7 space-y-3.5">
        {points.map((point, index) => (
          <Reveal key={point} delay={0.25 + index * 0.1}>
            <li className="flex items-start gap-2.5 text-sm leading-relaxed text-ink-2">
              <span className="mt-0.5 shrink-0 text-moss">
                <IconCheck size={15} />
              </span>
              {point}
            </li>
          </Reveal>
        ))}
      </ul>
    </div>
  )
}

/* ══════════════════════════════════════════════════════════════════════════
   右侧：表单
   ══════════════════════════════════════════════════════════════════════════ */

function AuthForm() {
  const { login, register, busy, error, clearError } = useAuth()
  const [mode, setMode] = useState<Mode>('login')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [touched, setTouched] = useState(false)

  // 切模式时清掉上一次的错误 —— 否则"登录失败"会挂在注册表单上，让人以为注册也失败
  useEffect(() => {
    clearError()
    setTouched(false)
  }, [mode, clearError])

  const usernameError = touched ? validateUsername(username) : null
  const passwordError = touched ? validatePassword(password) : null
  const blocked = Boolean(validateUsername(username) || validatePassword(password))

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault()
    setTouched(true)
    if (blocked || busy) return

    const ok =
      mode === 'login'
        ? await login(username.trim(), password)
        : await register({
            username: username.trim(),
            password,
            display_name: displayName.trim() || undefined,
          })
    if (ok) setPassword('')
  }

  return (
    <div className="relative z-10 w-full max-w-[24rem]">
      <div className="paper relative overflow-hidden">
        {/* 书脊：把这张卡"装订"起来 */}
        <span aria-hidden="true" className="absolute inset-y-0 left-0 w-[3px] bg-moss" />

        <div className="px-6 pb-6 pl-7 pt-5">
          {/* 账本式页签。**不再另起一个"登录/注册"小标题** ——
              它和页签文字完全重复，只会让卡片顶部显得挤 */}
          <div className="mb-6 flex gap-6 border-b border-line pt-1">
            {(
              [
                ['login', '登录'],
                ['register', '注册'],
              ] as [Mode, string][]
            ).map(([key, label]) => (
              <button
                key={key}
                type="button"
                onClick={() => mode !== key && setMode(key)}
                aria-pressed={mode === key}
                className={cn(
                  '-mb-px border-b-2 pb-2 text-sm transition-colors duration-150',
                  mode === key
                    ? 'border-moss font-medium text-ink-1'
                    : 'border-transparent text-ink-3 hover:text-ink-2',
                )}
              >
                {label}
              </button>
            ))}
          </div>

          <form onSubmit={onSubmit} noValidate className="space-y-4">
            <div>
              <label htmlFor="auth-username" className="meta mb-1.5 block">
                登录名
              </label>
              <input
                id="auth-username"
                name="username"
                autoComplete="username"
                autoFocus
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                onBlur={() => setTouched(true)}
                aria-invalid={Boolean(usernameError)}
                aria-describedby={usernameError ? 'auth-username-error' : undefined}
                placeholder="字母开头，可含数字与下划线"
                className="field text-sm"
              />
              {usernameError && (
                <p id="auth-username-error" role="alert" className="mt-1.5 text-xs text-brick">
                  {usernameError}
                </p>
              )}
            </div>

            {mode === 'register' && (
              <div>
                <label htmlFor="auth-display" className="meta mb-1.5 block">
                  怎么称呼你<span className="text-ink-4">（可不填）</span>
                </label>
                <input
                  id="auth-display"
                  name="display_name"
                  autoComplete="nickname"
                  value={displayName}
                  onChange={(event) => setDisplayName(event.target.value)}
                  placeholder="填了就用它称呼你，可以中文"
                  className="field text-sm"
                />
              </div>
            )}

            <div>
              <label htmlFor="auth-password" className="meta mb-1.5 block">
                密码
              </label>
              <input
                id="auth-password"
                name="password"
                type="password"
                autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                onBlur={() => setTouched(true)}
                aria-invalid={Boolean(passwordError)}
                aria-describedby={passwordError ? 'auth-password-error' : undefined}
                placeholder="至少 6 位"
                className="field text-sm"
              />
              {passwordError && (
                <p id="auth-password-error" role="alert" className="mt-1.5 text-xs text-brick">
                  {passwordError}
                </p>
              )}
            </div>

            {error && (
              <motion.div
                role="alert"
                initial={{ opacity: 0, y: -4 }}
                animate={{ opacity: 1, y: 0 }}
                className="rounded-md border border-brick-line bg-brick-soft px-3 py-2 text-xs leading-relaxed text-brick-ink"
              >
                {error}
              </motion.div>
            )}

            <Button variant="primary" size="lg" loading={busy} className="w-full" type="submit">
              {busy ? '稍等一下' : mode === 'login' ? '进来继续学' : '建好，开始学'}
            </Button>
          </form>
        </div>
      </div>

      <p className="mt-5 px-1 text-xs leading-relaxed text-ink-3">
        {mode === 'login' ? (
          <>
            还没有账号？在上面切到「注册」，十几秒的事。
            <br />
            学习记录跟着账号走 —— 换台电脑登录，进度还在。
          </>
        ) : (
          <>
            账号只用来记住你的学习进度，不需要邮箱和手机号。
            <br />
            密码只存加密后的结果，谁（包括我们）都看不到原文。
          </>
        )}
      </p>
    </div>
  )
}

/* ══════════════════════════════════════════════════════════════════════════
   页面
   ══════════════════════════════════════════════════════════════════════════ */

export function AuthView() {
  return (
    <div className="grid min-h-screen lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
      {/* 左：产品是什么 */}
      <div className="relative hidden overflow-hidden border-r border-line lg:block">
        <KnowledgeBackdrop />
        {/* 一层柔和的遮罩，把网络压到背景层，让文字能读 */}
        <div
          aria-hidden="true"
          className="absolute inset-0"
          style={{
            background:
              'linear-gradient(115deg, color-mix(in srgb, var(--color-canvas) 92%, transparent) 0%, color-mix(in srgb, var(--color-canvas) 55%, transparent) 55%, transparent 100%)',
          }}
        />
        <div className="relative flex h-full items-center px-14">
          <Pitch />
        </div>
      </div>

      {/* 右：表单 */}
      <div className="flex items-center justify-center px-6 py-12">
        <AuthForm />
      </div>
    </div>
  )
}
