import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // 开发时由 Vite 转发 API 请求，让前后端沿用同一相对路径。
      '/api': 'http://localhost:3000',
    },
  },
})
