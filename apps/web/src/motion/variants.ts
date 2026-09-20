/**
 * 六种教学动作的视觉配置。
 *
 * 刻意**不用彩色徽章**区分动作 —— 那会让学习界面变成"状态灯板"。
 * 区分靠三件事：**一个小符号 + 一句话 + 一种克制的微动**。
 *
 * 每种动作给一套视觉提示，都是轻量的：
 *   probe    追问   —— 一个问号浮现，然后让位给内容
 *   explain  讲一讲 —— 内容从左侧一条墨线展开（像翻开一页）
 *   rephrase 换个讲法 —— 一条竖线轻扫而过，换了个说法
 *   harder   加点难度 —— 内容微微上移，"提了一格"
 *   easier   放慢一点 —— 内容微微下沉坐稳，"退一步"
 *   summarize 收个尾 —— 内容轻轻合拢，加一个柔和的边框收束
 *
 * 视觉提示由 TurnStream 在消息出现时应用一次，不重复播放。
 */

import type { Variants } from 'motion/react'

import { EASE_OUT_QUART } from './primitives'

export type ActionKey =
  | 'probe'
  | 'explain'
  | 'rephrase'
  | 'harder'
  | 'easier'
  | 'summarize'

export interface ActionCue {
  /** 一句话说明这个动作在干嘛（人话） */
  label: string
  /** 更轻的二级描述，用在右栏轨迹里 */
  hint: string
  /** 容器微动（作用于整条消息的入场） */
  container: Variants
  /** 用作强调的小符号（Unicode，避免引入图标库） */
  glyph: string
  /** 强调色的 CSS 变量名（克制，只用在符号与细线上） */
  toneVar: string
}

const enter = (from: Partial<{ y: number; x: number; opacity: number; scaleY: number }>): Variants => ({
  hidden: { opacity: 0, ...from },
  show: {
    opacity: 1,
    x: 0,
    y: 0,
    scaleY: 1,
    transition: { duration: 0.3, ease: EASE_OUT_QUART },
  },
})

export const ACTION_CUE: Record<ActionKey, ActionCue> = {
  probe: {
    label: '追问一句',
    hint: '再问深一点，看是不是真懂了',
    container: enter({ opacity: 0, y: 3 }),
    glyph: '?',
    toneVar: 'var(--color-slate-blue)',
  },
  explain: {
    label: '讲一讲',
    hint: '先把这个知识点讲清楚',
    container: enter({ opacity: 0, x: -6 }),
    glyph: '—',
    toneVar: 'var(--color-moss)',
  },
  rephrase: {
    label: '换个讲法',
    hint: '刚才那种讲法没通，换一种',
    container: enter({ opacity: 0, scaleY: 0.96 }),
    glyph: '↻',
    toneVar: 'var(--color-sienna)',
  },
  harder: {
    label: '加点难度',
    hint: '你答得不错，提一格试试',
    container: enter({ opacity: 0, y: 6 }),
    glyph: '↑',
    toneVar: 'var(--color-moss)',
  },
  easier: {
    label: '放慢一点',
    hint: '退一步，把基础再垫实',
    container: enter({ opacity: 0, y: -6 }),
    glyph: '↓',
    toneVar: 'var(--color-sienna)',
  },
  summarize: {
    label: '收个尾',
    hint: '这块你已经稳了，收一下',
    container: enter({ opacity: 0, scaleY: 0.9 }),
    glyph: '✓',
    toneVar: 'var(--color-moss)',
  },
}

export function actionCue(action: string): ActionCue {
  return ACTION_CUE[(action as ActionKey) ?? 'explain'] ?? ACTION_CUE.explain
}
