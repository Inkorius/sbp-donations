// js/supabase-cdn.js
// Версия для статического сайта (без Vite, без npm)

// Проверяем, не загружен ли уже Supabase
if (!window.supabaseClient) {
    // Ключи Supabase (ваши реальные ключи)
    const supabaseUrl = 'https://rvbduwlfnhepnknrhtaz.supabase.co';
    const supabaseKey = 'sb_publishable_F6obQL6SYTdh-Sx4dbifWQ_svWD3mOj';
    
    // Динамически загружаем Supabase из CDN
    (async function() {
        try {
            // Импортируем Supabase из CDN
            const { createClient } = await import('https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.39.7/+esm');
            
            // Создаем клиент
            window.supabaseClient = createClient(supabaseUrl, supabaseKey);
            
            // Глобальная переменная для обратной совместимости
            window.supabase = window.supabaseClient;
            
            console.log('Supabase loaded from CDN');
            
            // Вызываем событие, что Supabase загружен
            document.dispatchEvent(new Event('supabase-loaded'));
            
        } catch (error) {
            console.error('Failed to load Supabase:', error);
            // Показываем сообщение об ошибке
            document.body.innerHTML += `
                <div style="position: fixed; top: 0; left: 0; right: 0; background: #f44336; color: white; padding: 10px; text-align: center; z-index: 9999;">
                    Ошибка загрузки Supabase. Пожалуйста, обновите страницу.
                </div>
            `;
        }
    })();
}

// Функция для получения клиента (если нужно дождаться загрузки)
function getSupabase() {
    if (window.supabaseClient) {
        return Promise.resolve(window.supabaseClient);
    }
    
    return new Promise((resolve) => {
        document.addEventListener('supabase-loaded', () => {
            resolve(window.supabaseClient);
        });
    });
}