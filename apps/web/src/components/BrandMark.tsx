/**
 * 品牌标识。
 *
 * 形状是一枚**书签**。书签是"拾知"的视觉签名：它出现在待复习卡片
 * 从边缘探出的那一小截暖色标签上，语义上说得通 ——
 * **"我帮你夹在这里了"** = 学习伙伴记得你上次停在哪。
 *
 * 一个形状同时承担品牌识别与产品功能，比再画一个抽象图标更省力也更结实。
 *
 * 颜色一律走设计令牌（`currentColor` / CSS 变量），**不写字面色值** ——
 * 否则切到暗色主题时这枚标记会变成纸上的一块墨点。
 */

import { motion } from 'motion/react'

export function BrandMark({ size = 24 }: { size?: number }) {
  const height = size * 1.3
  return (
    <svg
      width={size}
      height={height}
      viewBox="0 0 20 26"
      fill="none"
      role="img"
      aria-label="拾知"
      className="shrink-0"
    >
      {/* 书签主体：顶部平齐、底部一个 V 形缺口 */}
      <path
        d="M0 2a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v22l-10-5.5L0 24V2Z"
        fill="var(--color-moss)"
      />
      {/* 内嵌的一小条暖色，暗示"夹住的那一页" */}
      <rect x="6" y="5" width="8" height="1.6" rx="0.8" fill="var(--color-paper-raised)" opacity="0.92" />
      <rect x="6" y="9" width="5" height="1.6" rx="0.8" fill="var(--color-sienna)" />
    </svg>
  )
}

/**
 * 启动过渡。
 *
 * 确认登录态的那几十毫秒里显示它。**不用转圈** ——
 * 转圈是"系统在忙"的语言，这里是"我在准备你的书桌"，用品牌标记更贴。
 * 呼吸幅度压得很小，避免一闪一闪地抢注意力。
 */
export function BrandMarkSplash() {
  return (
    <motion.div
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
      className="flex items-center gap-3"
    >
      <motion.span
        animate={{ opacity: [0.6, 1, 0.6] }}
        transition={{ duration: 2.4, repeat: Infinity, ease: 'easeInOut' }}
        className="inline-flex"
      >
        <BrandMark size={22} />
      </motion.span>
      <span className="font-serif text-base text-ink-2">拾知</span>
    </motion.div>
  )
}
