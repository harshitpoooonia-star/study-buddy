import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

export default defineConfig({
  base: '/dash/',
  plugins: [svelte()],
  build: { outDir: 'dist', emptyOutDir: true, target: 'es2020' },
  server: { proxy: { '/dash/api': 'http://127.0.0.1:8770', '/dash/ics': 'http://127.0.0.1:8770' } },
})
