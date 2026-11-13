(function () {
    const STORAGE_KEY = 'sbp_lang';
    const labels = {
        ru: {
            try: 'Попробовать',
            execute: 'Выполнить',
            cancel: 'Отмена',
            clear: 'Очистить',
            responses: 'Ответы',
            parameters: 'Параметры',
            body: 'Тело запроса',
            server: 'Ответ сервера',
            responseBody: 'Тело ответа',
            headers: 'Заголовки ответа',
            code: 'Код',
            details: 'Подробности',
            noParams: 'Нет параметров',
            default: 'По умолчанию',
            example: 'Пример',
            schema: 'Схема',
            available: 'Доступные методы',
            filter: 'Фильтр',
            banner: 'Это не сайт для стримеров. Это техническая страница API. Сайт: ',
            home: 'главная страница',
        },
        en: {
            try: 'Try it out',
            execute: 'Execute',
            cancel: 'Cancel',
            clear: 'Clear',
            responses: 'Responses',
            parameters: 'Parameters',
            body: 'Request body',
            server: 'Server response',
            responseBody: 'Response body',
            headers: 'Response headers',
            code: 'Code',
            details: 'Details',
            noParams: 'No parameters',
            default: 'Default',
            example: 'Example Value',
            schema: 'Schema',
            available: 'Available authorizations',
            filter: 'Filter by tag',
            banner: 'This is API documentation, not the streamer website. Website: ',
            home: 'home page',
        },
    };

    function lang() {
        const stored = localStorage.getItem(STORAGE_KEY);
        if (stored === 'en' || stored === 'ru') return stored;
        return (navigator.language || 'ru').toLowerCase().startsWith('en') ? 'en' : 'ru';
    }

    function setLang(next) {
        localStorage.setItem(STORAGE_KEY, next);
        translate();
    }

    function replaceText(root, from, to) {
        if (!from || from === to) return;
        root.querySelectorAll('button, span, label, h3, h4, p, small, td, th, a').forEach((el) => {
            if (el.children.length === 0 && el.textContent.trim() === from) {
                el.textContent = to;
            }
        });
    }

    function translate() {
        const current = lang();
        const t = labels[current];
        const en = labels.en;
        const root = document.querySelector('.swagger-ui') || document.body;
        const pairs = [
            [en.try, t.try],
            [en.execute, t.execute],
            [en.cancel, t.cancel],
            [en.clear, t.clear],
            [en.responses, t.responses],
            [en.parameters, t.parameters],
            [en.body, t.body],
            [en.server, t.server],
            [en.responseBody, t.responseBody],
            [en.headers, t.headers],
            [en.code, t.code],
            [en.details, t.details],
            [en.noParams, t.noParams],
            [en.example, t.example],
            [en.schema, t.schema],
            [labels.ru.try, t.try],
            [labels.ru.execute, t.execute],
            [labels.ru.server, t.server],
            [labels.ru.body, t.body],
            [labels.ru.details, t.details],
            [labels.ru.code, t.code],
        ];
        pairs.forEach(([from, to]) => replaceText(root, from, to));
        document.querySelectorAll('[data-set-lang]').forEach((btn) => {
            btn.classList.toggle('active', btn.getAttribute('data-set-lang') === current);
        });
        const bannerText = document.getElementById('sbp-docs-banner-text');
        if (bannerText) {
            bannerText.innerHTML = t.banner + '<a href="/">' + t.home + '</a>';
        }
    }

    function mountBar() {
        if (document.getElementById('sbp-docs-bar')) return;
        const bar = document.createElement('div');
        bar.id = 'sbp-docs-bar';
        bar.innerHTML =
            '<div id="sbp-docs-banner-text"></div>' +
            '<div class="sbp-lang"><button type="button" data-set-lang="ru">RU</button>' +
            '<button type="button" data-set-lang="en">EN</button></div>';
        document.body.insertBefore(bar, document.body.firstChild);
        bar.addEventListener('click', (event) => {
            const btn = event.target.closest('[data-set-lang]');
            if (btn) setLang(btn.getAttribute('data-set-lang'));
        });
    }

    const style = document.createElement('style');
    style.textContent =
        '#sbp-docs-bar{display:flex;justify-content:space-between;gap:16px;align-items:center;padding:12px 20px;background:#4b2e83;color:#fff;font-family:Arial,sans-serif}' +
        '#sbp-docs-bar a{color:#fff;font-weight:bold}' +
        '.sbp-lang button{margin-left:6px;padding:6px 10px;border:0;border-radius:4px;cursor:pointer}' +
        '.sbp-lang button.active{background:#ffd54f}';
    document.head.appendChild(style);

    const ready = setInterval(() => {
        if (document.querySelector('.swagger-ui')) {
            clearInterval(ready);
            mountBar();
            translate();
            const ui = document.querySelector('.swagger-ui');
            new MutationObserver(translate).observe(ui, { childList: true, subtree: true });
        }
    }, 200);
})();
