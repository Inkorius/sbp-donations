// supabase-client.js - CDN версия для GitHub Pages
const { createClient } = window.supabase;

// Определяем окружение
const isLocalhost = window.location.hostname === 'localhost' || 
                    window.location.hostname === '127.0.0.1' ||
                    window.location.hostname === '';

let supabaseUrl, supabaseKey;

if (isLocalhost) {
  // Локальная разработка
  supabaseUrl = import.meta.env?.VITE_SUPABASE_URL || 
                'https://rvbduwlfnhepnknrhtaz.supabase.co';
  supabaseKey = import.meta.env?.VITE_SUPABASE_ANON_KEY || 
                'sb_publishable_F6obQL6SYTdh-Sx4dbifWQ_svWD3mOj';
} else {
  // Продакшен (GitHub Pages)
  supabaseUrl = 'https://rvbduwlfnhepnknrhtaz.supabase.co';
  supabaseKey = 'sb_publishable_F6obQL6SYTdh-Sx4dbifWQ_svWD3mOj';
}

export const supabase = createClient(supabaseUrl, supabaseKey);