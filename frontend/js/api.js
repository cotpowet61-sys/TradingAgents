/* Единый слой API-запросов + WebSocket (относительные URL = работает через прокси). */

const API = {
    async get(path) {
        const r = await fetch(path);
        if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
        return r.json();
    },
    async post(path, body) {
        const r = await fetch(path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {}),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.detail || `${r.status} ${r.statusText}`);
        return data;
    },
};

/* WebSocket с авто-реконнектом */
function connectWS(onEvent, onState) {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => {
        document.getElementById('conn-status').className = 'conn-status online';
        document.getElementById('conn-status').textContent = '● онлайн';
    };
    ws.onmessage = (e) => {
        let data;
        try { data = JSON.parse(e.data); } catch { return; }
        if (data.type === 'hello' && onState) onState(data.state);
        if (onEvent) onEvent(data);
    };
    ws.onclose = () => {
        const el = document.getElementById('conn-status');
        el.className = 'conn-status offline';
        el.textContent = '● офлайн (переподключение…)';
        setTimeout(() => connectWS(onEvent, onState), 3000);
    };
    ws.onerror = () => ws.close();
    return ws;
}

/* Форматирование чисел */
function fmt(n, d = 2) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    return Number(n).toLocaleString('ru-RU', { minimumFractionDigits: d, maximumFractionDigits: d });
}
function fmtPrice(n) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    if (Math.abs(n) >= 1000) return n.toLocaleString('ru-RU', { maximumFractionDigits: 2 });
    if (Math.abs(n) >= 1) return n.toLocaleString('ru-RU', { maximumFractionDigits: 4 });
    return n.toPrecision(5);
}
function fmtTime(ts) {
    if (!ts) return '—';
    return new Date(ts).toLocaleTimeString('ru-RU');
}
