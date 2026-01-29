import { createClient } from '@supabase/supabase-js'

// Определяем окружение
const isLocalhost = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';

let supabaseUrl, supabaseKey;

if (isLocalhost) {
  // Локальная разработка - из .env
  supabaseUrl = import.meta.env.VITE_SUPABASE_URL;
  supabaseKey = import.meta.env.VITE_SUPABASE_ANON_KEY;
} else {
  // Продакшен (GitHub Pages) - ваши реальные ключи
  // ЗАМЕНИТЕ НИЖЕ НА ВАШИ РЕАЛЬНЫЕ КЛЮЧИ SUPABASE!
  supabaseUrl = 'https://rvbduwlfnhepnknrhtaz.supabase.co';
  supabaseKey = 'sb_publishable_F6obQL6SYTdh-Sx4dbifWQ_svWD3mOj';
}

// Проверяем, что ключи есть
if (!supabaseUrl || !supabaseKey) {
  console.error('Supabase URL or Key is missing!');
}

export const supabase = createClient(supabaseUrl, supabaseKey);