(function () {
    const STORAGE_KEY = 'sbp_lang';
    const dict = {
        ru: {
            'brand.title': 'SBP Donations',
            'brand.tagline': 'Приём донатов через СБП без посредников',
            'nav.login': 'Вход для стримеров',
            'nav.home': 'На главную',
            'home.how': 'Как это работает',
            'home.step1': 'Стример регистрируется и загружает QR-код СБП',
            'home.step2': 'Стример получает ссылку для донатов',
            'home.step3': 'Зритель переходит по ссылке, сканирует QR и переводит деньги',
            'home.step4': 'Зритель уведомляет стримера через форму',
            'home.step5': 'Стример видит донаты в панели управления',
            'home.status': 'Статус',
            'home.status.api': 'Свой сервер и база данных работают',
            'home.status.auth': 'Регистрация и вход',
            'home.status.site': 'Сайт снова открывается с этой страницы, не из /docs',
            'home.status.obs': 'OBS-оверлей — следующий шаг',
            'login.title': 'Вход',
            'login.registerTitle': 'Регистрация',
            'login.email': 'Email',
            'login.password': 'Пароль',
            'login.submit': 'Войти',
            'login.register': 'Зарегистрироваться',
            'login.toRegister': 'Нет аккаунта? Зарегистрироваться',
            'login.toLogin': 'Уже есть аккаунт? Войти',
            'login.processing': 'Обработка...',
            'login.ok': 'Вход выполнен. Переходим в панель...',
            'login.registered': 'Аккаунт создан. Переходим в панель...',
            'dash.title': 'Панель управления',
            'dash.logout': 'Выйти',
            'dash.profile': 'Ваш профиль',
            'dash.refresh': 'Обновить профиль',
            'dash.total': 'Всего донатов',
            'dash.amount': 'Общая сумма',
            'dash.pending': 'Ожидают подтверждения',
            'dash.qr': 'Загрузить QR-код СБП',
            'dash.qr.hint': 'Изображение QR-кода из банка (JPG, PNG, WebP, до 2 МБ)',
            'dash.qr.btn': 'Загрузить QR-код',
            'dash.link': 'Ваша ссылка для донатов',
            'dash.link.share': 'Отправьте эту ссылку зрителям',
            'dash.copy': 'Скопировать ссылку',
            'dash.copied': 'Скопировано',
            'dash.donations': 'Последние донаты',
            'dash.reload': 'Обновить список',
            'dash.confirmAll': 'Подтвердить все ожидающие',
            'dash.loading': 'Загрузка...',
            'donate.notify': 'Уведомить о переводе',
        },
        en: {
            'brand.title': 'SBP Donations',
            'brand.tagline': 'SBP donations without a middleman',
            'nav.login': 'Streamer sign in',
            'nav.home': 'Home',
            'home.how': 'How it works',
            'home.step1': 'The streamer signs up and uploads an SBP QR code',
            'home.step2': 'The streamer gets a donation link',
            'home.step3': 'The viewer opens the link, scans the QR code, and pays',
            'home.step4': 'The viewer notifies the streamer via the form',
            'home.step5': 'The streamer sees donations in the dashboard',
            'home.status': 'Status',
            'home.status.api': 'Your own server and database are running',
            'home.status.auth': 'Sign up and sign in',
            'home.status.site': 'The website is this page, not /docs',
            'home.status.obs': 'OBS overlay is next',
            'login.title': 'Sign in',
            'login.registerTitle': 'Create account',
            'login.email': 'Email',
            'login.password': 'Password',
            'login.submit': 'Sign in',
            'login.register': 'Create account',
            'login.toRegister': 'No account? Create one',
            'login.toLogin': 'Already have an account? Sign in',
            'login.processing': 'Please wait...',
            'login.ok': 'Signed in. Opening the dashboard...',
            'login.registered': 'Account created. Opening the dashboard...',
            'dash.title': 'Dashboard',
            'dash.logout': 'Sign out',
            'dash.profile': 'Your profile',
            'dash.refresh': 'Refresh profile',
            'dash.total': 'Total donations',
            'dash.amount': 'Total amount',
            'dash.pending': 'Waiting for confirmation',
            'dash.qr': 'Upload SBP QR code',
            'dash.qr.hint': 'Bank QR image (JPG, PNG, WebP, max 2 MB)',
            'dash.qr.btn': 'Upload QR code',
            'dash.link': 'Your donation link',
            'dash.link.share': 'Share this link with viewers',
            'dash.copy': 'Copy link',
            'dash.copied': 'Copied',
            'dash.donations': 'Recent donations',
            'dash.reload': 'Refresh list',
            'dash.confirmAll': 'Confirm all pending',
            'dash.loading': 'Loading...',
            'donate.notify': 'Notify about the transfer',
        },
    };

    function currentLang() {
        const stored = localStorage.getItem(STORAGE_KEY);
        if (stored === 'en' || stored === 'ru') return stored;
        return (navigator.language || 'ru').toLowerCase().startsWith('en') ? 'en' : 'ru';
    }

    function t(key) {
        const lang = currentLang();
        return (dict[lang] && dict[lang][key]) || (dict.ru[key] || key);
    }

    function apply() {
        const lang = currentLang();
        document.documentElement.lang = lang;
        document.querySelectorAll('[data-i18n]').forEach((el) => {
            const key = el.getAttribute('data-i18n');
            if (key) el.textContent = t(key);
        });
        document.querySelectorAll('[data-i18n-placeholder]').forEach((el) => {
            const key = el.getAttribute('data-i18n-placeholder');
            if (key) el.setAttribute('placeholder', t(key));
        });
        document.querySelectorAll('[data-set-lang]').forEach((btn) => {
            btn.classList.toggle('active', btn.getAttribute('data-set-lang') === lang);
        });
        document.dispatchEvent(new CustomEvent('language-changed', { detail: { lang } }));
    }

    function setLang(lang) {
        localStorage.setItem(STORAGE_KEY, lang);
        apply();
    }

    document.addEventListener('click', (event) => {
        const btn = event.target.closest('[data-set-lang]');
        if (!btn) return;
        setLang(btn.getAttribute('data-set-lang'));
    });

    window.sbpI18n = { t, apply, setLang, currentLang };
    document.addEventListener('DOMContentLoaded', apply);
})();
