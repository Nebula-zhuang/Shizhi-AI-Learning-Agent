/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端地址。留空则走 Vite 代理（开发期推荐） */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
