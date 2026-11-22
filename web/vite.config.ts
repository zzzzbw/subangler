import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// 构建产物直接输出到后端的 static 目录，实现单端口托管。
const ROOT = dirname(fileURLToPath(import.meta.url))
const OUT_DIR = resolve(ROOT, '../src/subangler/static')

export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    outDir: OUT_DIR,
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
        },
      },
    },
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8765',
    },
  },
})
