/**
 * 登录态。
 *
 * ## 一个容易忽略但很关键的细节：**首次要"确认"，不要"猜"**
 *
 * 应用启动时并不知道用户有没有登录 —— 令牌在 httpOnly Cookie 里，JS 读不到，
 * 只能问一次后端。如果不管这件事直接渲染登录页，已登录用户会看到登录页**闪一下**
 * 再跳走。所以这里有一个 `checking` 态：确认期间显示一个安静的过渡，
 * 确认完再决定去登录页还是进主界面。
 *
 * ## 为什么把 `user` 交给外层当 key
 *
 * 换账号时，学习数据（看板、会话、掌握度）必须整体重置 ——
 * 否则 A 的会话会被渲染到 B 的界面上。外层用 `user.id` 当 key 重挂载
 * LearningProvider 就是最省事也最不容易出错的做法。
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

import {
  fetchMe,
  login as loginApi,
  logout as logoutApi,
  register as registerApi,
  type AuthUser,
  type RegisterPayload,
} from '../api/auth'
import { messageOf } from '../api/http'
import { friendlyError } from '../features/learn/voice'

interface AuthContextValue {
  user: AuthUser | null
  /** 首次正在向后端确认登录态。确认期间不要渲染登录页，否则会闪。 */
  checking: boolean
  busy: boolean
  error: string | null
  login: (username: string, password: string) => Promise<boolean>
  register: (payload: RegisterPayload) => Promise<boolean>
  logout: () => Promise<void>
  clearError: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [checking, setChecking] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    fetchMe()
      .then((me) => {
        if (alive) setUser(me)
      })
      .catch(() => {
        // 后端没起来也算"未登录" —— 不让它卡在过渡页上
        if (alive) setUser(null)
      })
      .finally(() => {
        if (alive) setChecking(false)
      })
    return () => {
      alive = false
    }
  }, [])

  const login = useCallback(async (username: string, password: string) => {
    setBusy(true)
    setError(null)
    try {
      const result = await loginApi(username, password)
      setUser(result.user)
      return true
    } catch (err) {
      setError(friendlyError(messageOf(err)))
      return false
    } finally {
      setBusy(false)
    }
  }, [])

  const register = useCallback(async (payload: RegisterPayload) => {
    setBusy(true)
    setError(null)
    try {
      const result = await registerApi(payload)
      setUser(result.user)
      return true
    } catch (err) {
      setError(friendlyError(messageOf(err)))
      return false
    } finally {
      setBusy(false)
    }
  }, [])

  const logout = useCallback(async () => {
    setBusy(true)
    try {
      await logoutApi()
    } catch {
      // 网络失败也必须把本地状态清掉 —— 否则用户以为"点了没反应"，
      // 而界面上还留着他的名字。
    } finally {
      setUser(null)
      setBusy(false)
    }
  }, [])

  const clearError = useCallback(() => setError(null), [])

  const value = useMemo<AuthContextValue>(
    () => ({ user, checking, busy, error, login, register, logout, clearError }),
    [user, checking, busy, error, login, register, logout, clearError],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth 必须在 <AuthProvider> 内使用')
  return context
}
