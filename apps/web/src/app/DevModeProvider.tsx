/**
 * 开发者模式**对外只读**的窗口。
 *
 * ## 为什么需要它
 *
 * `Shell.tsx` 里的 `devMode` 以前只控制一件事：侧栏那个「开发者 › 接口调试」
 * 导航项。**它没有传出去**，于是任何页面都无从知道自己该不该收起内部信息 ——
 * 结果是自由学习页把「经由 MCP」这种内部词直接摆给了普通用户
 * （设计文档 §九 明令：「不在界面上暴露 Agent / Tool / RAG / Embedding 等内部词」）。
 *
 * 这个文件只做一件事：把 Shell 已经持有的那两个状态**广播出去**。
 *
 * ## 为什么状态仍留在 Shell
 *
 * 开关的**所有者**是 Shell（`?dev=1`、`Ctrl/⌘+Shift+D`、localStorage 记忆都在那里）。
 * 把状态搬到 Provider 里、Shell 反过来消费，改动面会大一圈，
 * 而收益只是"看上去更整齐" —— 不值得。这里做成纯发布通道，读写分离。
 *
 * ## 为什么 `useDevMode()` **不**在缺 Provider 时抛异常
 *
 * `AuthProvider` 是抛的（没有登录上下文说明装配错了，必须炸）。
 * 但开发者模式是**展示偏好**：拿不到上下文时正确的默认值是"不是开发者模式"，
 * 也就是"收起内部信息"—— 安全的那一侧。抛异常反而会让一个纯展示问题
 * 升级成白屏。
 */

import { createContext, useContext, type ReactNode } from 'react'

export interface DevModeValue {
  /** 浮层开发者面板**可用**吗（触发过 `?dev=1` 或快捷键）—— 决定要不要露出入口 */
  devAvailable: boolean
  /** 开发者模式**打开**吗 —— 决定页面上要不要显示内部信息 */
  devMode: boolean
}

const DevModeContext = createContext<DevModeValue | null>(null)

export function DevModeProvider({
  devAvailable,
  devMode,
  children,
}: DevModeValue & { children: ReactNode }) {
  return (
    <DevModeContext.Provider value={{ devAvailable, devMode }}>
      {children}
    </DevModeContext.Provider>
  )
}

/**
 * 读开发者模式。**没有 Provider 时返回"非开发者模式"**（见文件开头的理由）。
 *
 * 页面要用它来决定"这块内部信息露不露"，**不要**用它来做权限判断 ——
 * 它不是安全边界（前端藏起来的东西，后端该拦的仍然要拦）。
 */
export function useDevMode(): DevModeValue {
  return useContext(DevModeContext) ?? { devAvailable: false, devMode: false }
}
