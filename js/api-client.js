(function () {
    const TOKEN_KEY = 'sbp_token';

    function siteBase() {
        return window.location.hostname.endsWith('github.io') ? '/sbp-donations' : '';
    }

    function apiBase() {
        const host = window.location.hostname;
        if (host === 'localhost' || host === '127.0.0.1') return 'http://127.0.0.1:8080';
        if (host.endsWith('github.io')) return 'https://api.sbpdonation.ru';
        return '';
    }

    function token() {
        return localStorage.getItem(TOKEN_KEY);
    }

    function setToken(value) {
        if (value) localStorage.setItem(TOKEN_KEY, value);
        else localStorage.removeItem(TOKEN_KEY);
    }

    async function request(path, options = {}) {
        const headers = Object.assign({}, options.headers || {});
        const isForm = options.body instanceof FormData;
        if (!isForm && !headers['Content-Type']) {
            headers['Content-Type'] = 'application/json';
        }
        if (token()) headers.Authorization = 'Bearer ' + token();
        headers['Accept-Language'] = (window.sbpI18n && window.sbpI18n.currentLang()) || 'ru';

        const response = await fetch(apiBase() + path, Object.assign({}, options, { headers }));
        const text = await response.text();
        let data = null;
        try {
            data = text ? JSON.parse(text) : null;
        } catch (err) {
            data = { detail: text };
        }
        if (!response.ok) {
            const detail = data && data.detail;
            const message = Array.isArray(detail)
                ? (detail[0] && (detail[0].msg || JSON.stringify(detail[0])))
                : (detail || data.message || ('HTTP ' + response.status));
            const error = new Error(message);
            error.status = response.status;
            error.data = data;
            throw error;
        }
        return data;
    }

    window.sbpApi = {
        siteBase,
        apiBase,
        token,
        setToken,
        request,
        page(name) {
            return siteBase() + '/' + name;
        },
    };
})();
