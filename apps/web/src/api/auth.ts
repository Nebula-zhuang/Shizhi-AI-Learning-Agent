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
 * 前端能做的校验就放在前端做 —— 不是为了替代后端（后端照样会校验），
 * 而是让用户在按下按钮之前就知道哪里不对，少一次往返。
 */
export const USERNAME_PATTERN = /^[A-Za-z][A-Za-z0-9_]{2,19}$/

export function validateUsername(value: string): string | null {
  if (!value) return '请填写登录名'
  if (!USERNAME_PATTERN.test(value)) return '以字母开头，可含数字与下划线，3–20 位'
  return null
}

export function validatePassword(value: string): string | null {
  if (!value) return '请填写密码'
  if (value.length < 6) return '至少 6 位'
  if (value.length > 64) return '不超过 64 位'
  return null
}
