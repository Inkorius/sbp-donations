import { createClient } from '@supabase/supabase-js'

// Определяем окружение
const isLocalhost = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';

// Ваши реальные ключи (одинаковые для всех окружений)
const supabaseUrl = 'https://rvbduwlfnhepnknrhtaz.supabase.co';
const supabaseKey = 'sb_publishable_F6obQL6SYTdh-Sx4dbifWQ_svWD3mOj';

// Проверяем, что ключи есть
if (!supabaseUrl || !supabaseKey) {
  console.error('Supabase URL or Key is missing!');
}

export const supabase = createClient(supabaseUrl, supabaseKey);