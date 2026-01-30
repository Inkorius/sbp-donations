import { defineConfig } from 'vite'

export default defineConfig({
  root: '.',
  publicDir: 'public',
  server: {
    port: 5173,
    host: true
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // Важно для GitHub Pages
    rollupOptions: {
      input: {
        main: './index.html',
        login: './login.html',
        donate: './donate.html',
        dashboard: './dashboard.html'
        confirm: './confirm.html'
      }
    }
  },
  // Эта настройка критически важна для GitHub Pages
  base: '/sbp-donations/'
})