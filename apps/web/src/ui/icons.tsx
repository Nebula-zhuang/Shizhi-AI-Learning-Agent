/**
 * 图标集。
 *
 * **手写 SVG，不引图标库。** 理由不是"省一个依赖"，是控制力：
 * 这套界面的图标要统一为 1.5px 描边、圆头圆角、24 网格 ——
 * 混进任何第三方图标集都会在细节上和正文的字体重量打架（这是最常见的"廉价感"来源）。
 *
 * 统一约定：
 *   · viewBox 24×24，描边 1.5，`stroke-linecap/join: round`
 *   · **默认不填充**，颜色继承 `currentColor`（所以图标永远跟着文字颜色走）
 *   · 尺寸由 className 的 h-/w- 控制，默认 1em 跟随字号
 */

import type { SVGProps } from 'react'

type IconProps = SVGProps<SVGSVGElement> & { size?: number | string }

function Icon({ children, size = '1em', ...rest }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...rest}
    >
      {children}
    </svg>
  )
}

/* ------------------------------------------------------------------ 导航 */

export const IconHome = (p: IconProps) => (
  <Icon {...p}>
    <path d="M3.5 10.2 12 3.6l8.5 6.6" />
    <path d="M5.4 9v9.4a1.6 1.6 0 0 0 1.6 1.6h10a1.6 1.6 0 0 0 1.6-1.6V9" />
    <path d="M9.6 20v-5.4h4.8V20" />
  </Icon>
)

export const IconSpark = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 3.2v4.1M12 16.7v4.1M4.9 12h4.1M15 12h4.1" />
    <path d="M12 8.4a3.6 3.6 0 0 0 3.6 3.6A3.6 3.6 0 0 0 12 15.6 3.6 3.6 0 0 0 8.4 12 3.6 3.6 0 0 0 12 8.4Z" />
  </Icon>
)

export const IconLibrary = (p: IconProps) => (
  <Icon {...p}>
    <path d="M4.6 4.4h5.2a1.6 1.6 0 0 1 1.6 1.6v13.6a1.3 1.3 0 0 0-1.3-1.3H4.6Z" />
    <path d="M19.4 4.4h-5.2A1.6 1.6 0 0 0 12.6 6v13.6a1.3 1.3 0 0 1 1.3-1.3h5.5Z" />
  </Icon>
)

export const IconMap = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="6.4" cy="7" r="2.4" />
    <circle cx="17.6" cy="6.6" r="2.4" />
    <circle cx="12" cy="17.4" r="2.4" />
    <path d="M8.6 8.2 10.4 15M15.6 8.2 13.7 15M8.8 6.9h6.4" />
  </Icon>
)

export const IconUser = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="12" cy="8.4" r="3.6" />
    <path d="M4.8 20a7.2 7.2 0 0 1 14.4 0" />
  </Icon>
)

/* ------------------------------------------------------------------ 动作 */

export const IconArrowRight = (p: IconProps) => (
  <Icon {...p}>
    <path d="M4.8 12h14M13.2 6.6 18.6 12l-5.4 5.4" />
  </Icon>
)

export const IconArrowUpRight = (p: IconProps) => (
  <Icon {...p}>
    <path d="M7.2 16.8 16.8 7.2M9.4 7.2h7.4v7.4" />
  </Icon>
)

export const IconChevronRight = (p: IconProps) => (
  <Icon {...p}>
    <path d="m9.4 5.6 6.4 6.4-6.4 6.4" />
  </Icon>
)

export const IconChevronDown = (p: IconProps) => (
  <Icon {...p}>
    <path d="m5.6 9.4 6.4 6.4 6.4-6.4" />
  </Icon>
)

export const IconCheck = (p: IconProps) => (
  <Icon {...p}>
    <path d="m5 12.8 4.6 4.6L19 6.6" />
  </Icon>
)

export const IconClose = (p: IconProps) => (
  <Icon {...p}>
    <path d="M6.4 6.4 17.6 17.6M17.6 6.4 6.4 17.6" />
  </Icon>
)

export const IconPlus = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 5.4v13.2M5.4 12h13.2" />
  </Icon>
)

export const IconSearch = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="11" cy="11" r="6.2" />
    <path d="m15.6 15.6 4.2 4.2" />
  </Icon>
)

export const IconSend = (p: IconProps) => (
  <Icon {...p}>
    <path d="M4.6 11.6 20 4.4l-7.2 15.4-1.6-6.6-6.6-1.6Z" />
  </Icon>
)

export const IconRefresh = (p: IconProps) => (
  <Icon {...p}>
    <path d="M20 12a8 8 0 1 1-2.6-5.9" />
    <path d="M20.2 4.4v4.4h-4.4" />
  </Icon>
)

export const IconTrash = (p: IconProps) => (
  <Icon {...p}>
    <path d="M4.8 6.8h14.4M9.4 6.8V5.2a1.2 1.2 0 0 1 1.2-1.2h2.8a1.2 1.2 0 0 1 1.2 1.2v1.6" />
    <path d="M6.8 6.8 7.7 19a1.4 1.4 0 0 0 1.4 1.3h5.8a1.4 1.4 0 0 0 1.4-1.3l.9-12.2" />
  </Icon>
)

export const IconUpload = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 15.6V4.8M7.8 8.8 12 4.6l4.2 4.2" />
    <path d="M4.8 15.2v3.2a1.6 1.6 0 0 0 1.6 1.6h11.2a1.6 1.6 0 0 0 1.6-1.6v-3.2" />
  </Icon>
)

export const IconFile = (p: IconProps) => (
  <Icon {...p}>
    <path d="M13.6 3.8H7.4a1.6 1.6 0 0 0-1.6 1.6v13.2a1.6 1.6 0 0 0 1.6 1.6h9.2a1.6 1.6 0 0 0 1.6-1.6V8.4Z" />
    <path d="M13.6 3.8v4.6h4.6" />
  </Icon>
)

export const IconClock = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="8.2" />
    <path d="M12 7.4V12l3 1.8" />
  </Icon>
)

export const IconBookmark = (p: IconProps) => (
  <Icon {...p}>
    <path d="M6.4 4.4h11.2a1.2 1.2 0 0 1 1.2 1.2v14.6a.5.5 0 0 1-.78.4L12 16.3l-6.02 4.3a.5.5 0 0 1-.78-.4V5.6a1.2 1.2 0 0 1 1.2-1.2Z" />
  </Icon>
)

export const IconLightbulb = (p: IconProps) => (
  <Icon {...p}>
    <path d="M9.4 17.6h5.2M10.2 20.4h3.6" />
    <path d="M12 3.6a6 6 0 0 1 3.4 10.9c-.5.4-.8 1-.8 1.6v.3H9.4v-.3c0-.6-.3-1.2-.8-1.6A6 6 0 0 1 12 3.6Z" />
  </Icon>
)

export const IconMoon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M20 13.4A8.2 8.2 0 0 1 10.6 4a8.2 8.2 0 1 0 9.4 9.4Z" />
  </Icon>
)

export const IconSun = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="4.2" />
    <path d="M12 2.8v2.2M12 19v2.2M4.5 12H2.3M21.7 12h-2.2M6.7 6.7 5.1 5.1M18.9 18.9l-1.6-1.6M17.3 6.7l1.6-1.6M5.1 18.9l1.6-1.6" />
  </Icon>
)

export const IconCompass = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="8.4" />
    <path d="m15.4 8.6-2 5.2-5.2 2 2-5.2Z" />
  </Icon>
)

export const IconShield = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 3.4 5 6v5.6c0 4 2.9 7.4 7 8.9 4.1-1.5 7-4.9 7-8.9V6Z" />
    <path d="m9.2 12 2 2 3.6-3.8" />
  </Icon>
)

export const IconAlert = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="8.4" />
    <path d="M12 7.8v4.8M12 16.1h.01" />
  </Icon>
)

export const IconInbox = (p: IconProps) => (
  <Icon {...p}>
    <path d="M4.4 13.6 6.6 5.4a1.6 1.6 0 0 1 1.5-1.2h7.8a1.6 1.6 0 0 1 1.5 1.2l2.2 8.2" />
    <path d="M4.4 13.6h4l1 2.2h5.2l1-2.2h4v4.4a1.6 1.6 0 0 1-1.6 1.6H6a1.6 1.6 0 0 1-1.6-1.6Z" />
  </Icon>
)

export const IconLayers = (p: IconProps) => (
  <Icon {...p}>
    <path d="m12 3.6 8 4-8 4-8-4Z" />
    <path d="m4 12.4 8 4 8-4M4 16.8l8 4 8-4" />
  </Icon>
)

export const IconPause = (p: IconProps) => (
  <Icon {...p}>
    <path d="M9.4 5.6v12.8M14.6 5.6v12.8" />
  </Icon>
)
