/**
 * 应用外壳：侧边栏 + 内容区。
 *
 * ## 为什么从顶部标签改成侧边栏
 *
 * v1 是"顶部三个标签 + 一整块内容"，那是**网页**的骨架。
 * v2 要做"学习空间"，空间感来自**有墙、有栏、有稳定的定位锚**：
 * 侧边栏常驻不动，内容区独立滚动 —— 这样滚动时"我在哪"始终可见，
 * 而不是滚到一半连自己在哪个模块都不知道。
 *
 * 另外侧边栏能容纳"层级"（分组 + 当前项 + 底部账号区），
 * 顶部标签只能平铺，加一个入口就挤一分。
 *
 * ## 页面转场
 *
 * 切页用 `AnimatePresence` 做一个很轻的"淡入 + 上移 6px"，
 * 时长 260ms。**刻意不做左右滑动** —— 那是移动端的手势语言，
 * 桌面端横向滑动会让眼睛追着画面跑，反而更累。
 *
 * ## 开发者入口
 *
 * 默认不存在。`?dev=1` 或 `Ctrl/⌘ + Shift + D` 唤出（唤出后会记住）。
 * 它是内部工具，不是产品功能 —— 不该出现在任何用户的第一眼里。
 */

import { useEffect, useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'

import { useAuth } from './AuthProvider'
import { DevModeProvider } from './DevModeProvider'
import { useLearning, type View } from './LearningProvider'
import { DevPanel } from '../components/DevPanel'
import {
  IconCompass,
  IconHome,
  IconLibrary,
  IconMap,
  IconMoon,
  IconSpark,
  IconSun,
  IconUser,
  Tooltip,
  cn,
  useTheme,
} from '../ui'

interface NavItem {
  key: View
  label: string
  hint: string
  icon: (props: { size?: number }) => JSX.Element
}

/**
 * 导航分三组，但**只给中间一组带标签** —— 上下两组语义自明
 * （"今天 / 辅导"是主入口，"我的"是个人区），
 * 只有"资料 / 知识地图"需要一个共同的前提才读得通。
 *
 * 命名上刻意避开重复：组标签叫「书房」，条目叫「资料」——
 * 若组标签也叫"资料"，导航里会连着出现两个"资料"。
 */
const PRIMARY_NAV: NavItem[] = [
  { key: 'today', label: '今天', hint: '今天该学什么', icon: IconHome },
  { key: 'study', label: '自由学习', hint: '想问什么直接问，不用先选知识点', icon: IconCompass },
  { key: 'learn', label: '辅导', hint: '和助教一起把一个知识点学透', icon: IconSpark },
]

const LIBRARY_NAV: NavItem[] = [
  { key: 'library', label: '资料', hint: '上传的资料与从中读出的知识点', icon: IconLibrary },
  { key: 'map', label: '知识地图', hint: '知识点之间的关系', icon: IconMap },
]

const PERSONAL_NAV: NavItem[] = [
  { key: 'profile', label: '我的', hint: '学习记录与偏好', icon: IconUser },
]

const DEV_HINT_KEY = 'shizhi:dev-hint'

/** 侧边栏导航项。选中态用"左侧一道主色竖条 + 底色"，不用整块高亮 —— 后者太重 */
function NavButton({
  item,
  active,
  onSelect,
  collapsed,
}: {
  item: NavItem
  active: boolean
  onSelect: () => void
  collapsed: boolean
}) {
  const Icon = item.icon
  const button = (
    <button
      type="button"
      onClick={onSelect}
      aria-current={active ? 'page' : undefined}
      className={cn(
        'group relative flex w-full items-center gap-2.5 rounded-md py-2 text-sm transition-colors duration-150',
        collapsed ? 'justify-center px-0' : 'px-2.5',
        active ? 'font-medium text-ink-1' : 'text-ink-3 hover:text-ink-1',
      )}
    >
      {/* 选中指示：一道 3px 主色竖条，从中间"长"出来 */}
      <span
        aria-hidden="true"
        className={cn(
          'absolute left-0 w-[3px] rounded-full bg-moss transition-all duration-300',
          active ? 'h-4 opacity-100' : 'h-0 opacity-0',
        )}
        style={{ transitionTimingFunction: 'var(--ease-out-quart)' }}
      />
      {active && (
        <motion.span
          layoutId="nav-active-bg"
          className="absolute inset-0 rounded-md bg-paper-sunken"
          transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
        />
      )}
      <span className="relative z-10 flex items-center gap-2.5">
        <Icon size={16} />
        {!collapsed && <span className="truncate">{item.label}</span>}
      </span>
    </button>
  )

  return collapsed ? <Tooltip label={item.label} side="right">{button}</Tooltip> : button
}

export function Shell({ children }: { children: React.ReactNode }) {
  const { view, setView } = useLearning()
  const { user, logout, busy } = useAuth()
  const { mode, toggle } = useTheme()

  const [devMode, setDevMode] = useState(false)
  const [devAvailable, setDevAvailable] = useState(false)

  useEffect(() => {
    const fromUrl = new URLSearchParams(window.location.search).get('dev') === '1'
    const remembered = window.localStorage.getItem(DEV_HINT_KEY) === '1'
    if (fromUrl || remembered) {
      setDevAvailable(true)
      setDevMode(true)
    }
  }, [])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === 'd') {
        event.preventDefault()
        setDevAvailable(true)
        setDevMode((value) => {
          const next = !value
          window.localStorage.setItem(DEV_HINT_KEY, next ? '1' : '0')
          return next
        })
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const renderGroup = (items: NavItem[]) =>
    items.map((item) => (
      <NavButton
        key={item.key}
        item={item}
        active={view === item.key}
        onSelect={() => setView(item.key)}
        collapsed={false}
      />
    ))

  return (
    <div className="flex h-screen overflow-hidden">
      {/* ═══════════════════════════════════════════════ 侧边栏 */}
      <aside
        aria-label="主导航"
        className="glass relative z-20 flex w-[var(--size-sidebar)] shrink-0 flex-col border-r border-line/70"
      >
        {/* 品牌 */}
        <div className="flex items-center gap-2.5 px-4 pb-5 pt-5">
          <span className="text-moss">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path
                d="M6 3.5h12a1.5 1.5 0 0 1 1.5 1.5v15.2a.6.6 0 0 1-.94.5L12 16.4l-6.56 4.3a.6.6 0 0 1-.94-.5V5A1.5 1.5 0 0 1 6 3.5Z"
                fill="currentColor"
              />
              <path
                d="M9.2 8.6h5.6M9.2 11.6h3.4"
                stroke="var(--color-glass)"
                strokeWidth="1.3"
                strokeLinecap="round"
              />
            </svg>
          </span>
          <div className="min-w-0">
            <p className="font-serif text-base leading-none text-ink-1">拾知</p>
            <p className="meta mt-1 truncate">你的学习伙伴</p>
          </div>
        </div>

        {/* 导航 */}
        <nav className="min-h-0 flex-1 space-y-5 overflow-y-auto px-2.5">
          <div className="space-y-0.5">{renderGroup(PRIMARY_NAV)}</div>

          <div className="space-y-0.5">
            <p className="meta px-2.5 pb-1 tracking-[0.14em]">书房</p>
            {renderGroup(LIBRARY_NAV)}
          </div>

          <div className="space-y-0.5">{renderGroup(PERSONAL_NAV)}</div>

          {devMode && (
            <div className="space-y-0.5">
              <p className="meta px-2.5 pb-1 tracking-[0.14em]">开发者</p>
              <NavButton
                item={{ key: 'chat', label: '接口调试', hint: '直连网关', icon: IconSpark }}
                active={view === 'chat'}
                onSelect={() => setView('chat')}
                collapsed={false}
              />
            </div>
          )}
        </nav>

        {/* 底部：账号 + 主题 */}
        <div className="space-y-2 border-t border-line/70 px-3 py-3">
          <button
            type="button"
            onClick={toggle}
            className="btn-ghost w-full justify-start px-2 text-xs"
            aria-label={mode === 'light' ? '切换到夜间' : '切换到日间'}
          >
            {mode === 'light' ? <IconMoon size={15} /> : <IconSun size={15} />}
            {mode === 'light' ? '夜间' : '日间'}
          </button>

          <div className="flex items-center gap-2 rounded-md px-2 py-1.5">
            <span
              aria-hidden="true"
              className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-moss-soft font-serif text-2xs text-moss-ink"
            >
              {user?.display_name.slice(0, 1) ?? '?'}
            </span>
            <span className="min-w-0 flex-1 truncate text-xs text-ink-2">
              {user?.display_name ?? ''}
            </span>
            <Tooltip label={busy ? '正在退出…' : '退出登录'} side="top">
              <button
                type="button"
                onClick={() => void logout()}
                disabled={busy}
                className="text-2xs text-ink-4 transition-colors hover:text-ink-2 disabled:opacity-50"
              >
                退出
              </button>
            </Tooltip>
          </div>
        </div>
      </aside>

      {/* ═══════════════════════════════════════════════ 内容区 */}
      <div className="relative flex min-w-0 flex-1 flex-col">
        {devAvailable && (
          <div className="absolute right-4 top-4 z-30">
            <DevPanel onDevMode={setDevMode} devMode={devMode} />
          </div>
        )}

        <main className="min-h-0 flex-1 overflow-y-auto">
          {/*
            `min-h-full` + 纵向 flex 是给"工作台型"页面（知识地图）用的：
            它们要撑满可视高度，而不是靠内容把高度堆出来。

            ⚠️ 之前没有这一段，知识地图的高度**其实是被左侧栏的内容撑出来的** ——
            侧栏一收起，画布高度就塌成一条 150px 的窄带。
            高度由"邻居有多少内容"决定，是个藏得很深的布局 bug：
            侧栏内容少的时候图本来就矮，只是没人往那个方向想。

            `min-h-full` 是**最小**高度：普通阅读页内容超过一屏时照常滚动，不受影响。
          */}
          <div
            className={cn(
              'mx-auto flex min-h-full w-full flex-col px-7 py-7',
              // 知识地图是**工作台**，不是阅读栏 —— 它要吃满横向空间。
              // 其余页面套 1240px 是对的：那是让人读得下去的行宽上限。
              // 地图没有"行宽"这回事，多出来的宽度直接变成能看见的更多节点。
              // 工作台型页面（地图 / 自由学习）不套阅读栏宽度上限 ——
              // 它们的空间就是用来铺内容的，多出来的宽度直接变成可见的信息。
              view !== 'map' && view !== 'study' && 'max-w-[var(--size-content-max)]',
            )}
          >
            <AnimatePresence mode="wait">
              <motion.div
                key={view}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -4 }}
                transition={{ duration: 0.26, ease: [0.16, 1, 0.3, 1] }}
                className="flex min-h-0 flex-1 flex-col"
              >
                {/* 开发者模式**只读广播**给页面：开关的所有权仍在 Shell
                    （快捷键 + localStorage + `?dev=1` 都在这里），
                    这一层只让页面知道该不该收起内部信息。 */}
                <DevModeProvider devAvailable={devAvailable} devMode={devMode}>
                  {children}
                </DevModeProvider>
              </motion.div>
            </AnimatePresence>
          </div>
        </main>
      </div>
    </div>
  )
}
