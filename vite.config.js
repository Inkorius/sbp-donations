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
    rollupOptions: {
      input: {
        main: './index.html',
        login: './login.html',
        donate: './donate.html',
        dashboard: './dashboard.html',
        confirm: './confirm.html'
      }
    }
  },
  base: '/sbp-donations/',
  // Указываем Vite обрабатывать все HTML файлы
  plugins: [
    {
      name: 'html-transform',
      transformIndexHtml(html) {
        // Добавляем CDN скрипт Supabase в каждый HTML файл
        return html.replace(
          '</head>',
          `
          <!-- Supabase CDN -->
          <script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.39.7/+esm"></script>
          </head>`
        );
      }
    }
  ]
})