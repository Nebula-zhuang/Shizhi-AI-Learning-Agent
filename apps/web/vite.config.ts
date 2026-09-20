import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],

  server: {
    port: 5173,
    strictPort: true,
    // 开发期把 /api 代理到后端，前端与后端同源 → 无需处理 CORS，
    // 也避免把后端地址写进前端代码。
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        // 大文件上传的两道超时。
        //
        // `timeout`    —— 代理**等待后端响应**的上限
        // `proxyTimeout` —— 代理**转发请求体**的上限
        //
        // 默认值对普通 JSON 请求够用，但 300MB 的文件在慢一点的盘上
        // 光"把请求体传完"就可能超过默认值，表现为传到一半莫名中断。
        // 这里放宽到 10 分钟 —— 它只是兜底阈值，正常上传几秒就结束了。
        timeout: 600_000,
        proxyTimeout: 600_000,
        // SSE 需要关闭代理缓冲，否则增量会被攒着一次性返回
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            proxyRes.headers['cache-control'] = 'no-cache, no-transform'
          })
        },
      },
    },
  },

  build: {
    outDir: 'dist',
    sourcemap: true,
    rollupOptions: {
      output: {
        // 按库分包。注意：**必须给 react 单独一个 chunk** ——
        // 否则 rollup 会把 react/react-dom/scheduler 归到它们被引用的那个
        // 手动 chunk 里（实测会让 vendor-radix 从 20KB 涨到 156KB，
        // 看起来像"Radix 很重"，其实是 React 被算进去了）。
        manualChunks(id: string) {
          if (!id.includes('node_modules')) return undefined
          if (/node_modules\/(react|react-dom|scheduler)\//.test(id)) return 'vendor-react'
          if (id.includes('motion')) return 'vendor-motion'
          if (id.includes('@radix-ui') || id.includes('@floating-ui')) return 'vendor-radix'
          return undefined
        },
      },
    },
  },
})
