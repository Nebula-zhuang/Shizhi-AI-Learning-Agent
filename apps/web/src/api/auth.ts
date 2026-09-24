/**
 * 账号接口。
 *
 * **前端不接触令牌** —— 登录态在 httpOnly Cookie 里，浏览器自动携带，
 * 这里的函数只负责"把用户名口令送出去"和"把当前是谁读回来"。
 * 所以这个文件里既没有存 token 的代码，也没有取 token 的代码 ——
 * 不是忘了写，是没有这个东西。
 */

import { getJson, postJson } from './http'

export interface AuthUser {
  id: number
  username: string
  display_name: string
  learner_id: string
  created_at: string | null
  last_login_at: string | null
}

export interface AuthResponse {
  user: AuthUser
  message: string
}

export interface RegisterPayload {
  username: string
  password: string
  display_name?: string
}

/** 当前登录用户。未登录返回 null —— 这是正常初始状态，不是错误。 */
export function fetchMe(): Promise<AuthUser | null> {
  return getJson<AuthUser | null>('/api/auth/me')
}

export function login(username: string, password: string): Promise<AuthResponse> {
  return postJson<AuthResponse>('/api/auth/login', { username, password })
}

export function register(payload: RegisterPayload): Promise<AuthResponse> {
  return postJson<AuthResponse>('/api/auth/register', payload)
}

export function logout(): Promise<AuthResponse> {
  return postJson<AuthResponse>('/api/auth/logout')
}

/**
 * 改密码。
 *
 * 后端早就做好了（`POST /api/auth/password`，校验旧密码 + 新密码 6–64 位 ✓），
 * 只是**前端一直没有入口** —— 这个函数补的就是那一段 ✗
 *
 * ⚠️ 改完之后**不自动登出**：后端返回的还是同一个会话 ✓
 * 也不刷新 `user`（密码不在 `user` 里，没什么可更新的 ✓）。
 * 后端失败（旧密码不对）会抛带 detail 的错，交给调用方展示 ✓
 */
export function changePassword(oldPassword: string, newPassword: string): Promise<AuthResponse> {
  return postJson<AuthResponse>('/api/auth/password', {
    old_password: oldPassword,
    new_password: newPassword,
  })
}

/**
 * 前端能做的校验就放在前端做 —— 不是为了替代后端（后端照样会校验），
 * 而是让用户在按下按钮之前就知道哪里不对，少一次往返。
 */
export const USERNAME_PATTERN = /^[A-Za-z][A-Za-z0-9_]{2,19}$/

export function validateUsername(value: string): string | null {
  if (!value) return '请填写登录名'
  if (!USERNAME_PATTERN.test(value)) return '以字母开头，可含数字与下划线，3–20 位'
  return null
}

/**
 * ⚠️ 密码规则**定义在零依赖的 `lib/passwordRules.ts`**，这里只是转发。
 *
 * 原因很实际：单测跑 `node --experimental-strip-types` 会真的解析模块路径，
 * 而本文件带着 `./http` 的运行时导入 → 任何"从 api/auth 导入一个函数"的测试
 * 都会 `ERR_MODULE_NOT_FOUND` ✗
 * 规则放在零依赖模块里才测得到（这是 5C-1 踩过的坑 ✓）。
 */
export { PASSWORD_MAX, PASSWORD_MIN, validatePassword } from '../lib/passwordRules'
