/**
 * 应用入口与门禁。
 *
 * 三种状态，顺序很重要：
 *
 *   1. **正在确认登录态** → 一个安静的过渡。**不能直接渲染登录页** ——
 *      令牌在 httpOnly Cookie 里，JS 读不到，只能问一次后端；
 *      这个空档如果渲染登录页，已登录用户会看到它闪一下再跳走。
 *   2. **未登录** → 登录 / 注册（Landing 式双栏）。
 *   3. **已登录** → 学习空间。
 *
 * `LearningProvider` 用 `user.id` 当 key：换账号时整体重挂载，
 * 看板、会话、掌握度全部重置。**这是防止"上一个人的学习状态漏到下一个人界面上"的
 * 最省事也最可靠的做法**。
 *
 * Provider 的嵌套顺序也讲究：Theme 在最外（只影响样式），
 * 然后 Auth（决定看到谁），再 Toast（任何操作都可能弹提示），
 * 最后 Learning（依赖当前用户）。
 */

import { AuthProvider, useAuth } from './app/AuthProvider'
import { LearningProvider, useLearning, type View } from './app/LearningProvider'
import { Shell } from './app/Shell'
import { BrandMarkSplash } from './components/BrandMark'
import { AuthView } from './features/auth/AuthView'
import { ChatView } from './features/chat/ChatView'
import { KnowledgeGraphView } from './features/graph/KnowledgeGraphView'
import { LearnView } from './features/learn/LearnView'
import { LibraryView } from './features/library/LibraryView'
import { ProfileView } from './features/profile/ProfileView'
import { FreeStudyView } from './features/study/FreeStudyView'
import { TodayView } from './features/today/TodayView'
import { ThemeProvider, ToastProvider } from './ui'

const VIEWS: Record<View, () => JSX.Element> = {
  today: TodayView,
  learn: LearnView,
  library: LibraryView,
  map: KnowledgeGraphView,
  study: FreeStudyView,
  profile: ProfileView,
  chat: ChatView,
}

function ActiveView() {
  const { view } = useLearning()
  const Component = VIEWS[view]
  return <Component />
}

/** 确认登录态期间的过渡。用品牌标记而不是转圈 —— 不要出现"通用加载中"的观感 */
function Confirming() {
  return (
    <div className="flex min-h-screen items-center justify-center">
      <BrandMarkSplash />
    </div>
  )
}

function Gate() {
  const { user, checking } = useAuth()

  if (checking) return <Confirming />
  if (!user) return <AuthView />

  return (
    <LearningProvider key={user.id}>
      <Shell>
        <ActiveView />
      </Shell>
    </LearningProvider>
  )
}

export default function App() {
  return (
    <ThemeProvider>
      <ToastProvider>
        <AuthProvider>
          <Gate />
        </AuthProvider>
      </ToastProvider>
    </ThemeProvider>
  )
}
