/* ============ TradingAgents — фронтенд-логика ============ */

const state = {
    symbols: [],
    strategies: [],
    currentSymbol: 'BTCUSDT',
    currentTF: '5m',
    rules: {},
    live: {},
    tvWidget: null,
    config: null,
    es: null, // текущий EventSource (стриминг-анализ)
    tvLoading: false,
    dataSource: 'unknown',
    // раздел «Монета»
    coin: {
        symbol: 'BTCUSDT',
        tf: '15m',
        allCoins: [],      // полный список (для фильтра)
        single: null,      // состояние движка 1-на-1
    },
};

/* ---------- Навигация по вкладкам ---------- */
function initNav() {
    document.querySelectorAll('.nav-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
            btn.classList.add('active');
            document.getElementById('panel-' + btn.dataset.tab).classList.add('active');
            if (btn.dataset.tab === 'backtest') redrawEquity();
            if (btn.dataset.tab === 'dashboard') resizeChart();
            if (btn.dataset.tab === 'coin') loadCoinChart();
            if (btn.dataset.tab === 'settings') loadSettings();
        });
    });
}

/* ---------- Инициализация ---------- */
async function init() {
    initNav();
    try {
        const health = await API.get('/api/health');
        state.rules = health.rules || {};
        setDemoBadge(health.demo_mode);
    } catch (e) { log('system', 'Ошибка подключения к backend: ' + e.message); }

    await Promise.all([loadSymbols(), loadStrategies()]);
    renderRules();
    renderFooterAgents();
    initChart();
    refreshTickers();
    initLive();
    initAssistant();
    initCoinTab();
    refreshDataBadge();

    connectWS(onWsEvent, (s) => { state.live = s || {}; renderLive(); });
    setInterval(refreshTickers, 30000);
    setInterval(refreshLiveState, 10000);
    setInterval(refreshDataBadge, 60000);
    setInterval(refreshSingleState, 15000);
}

/* ---------- Бейдж источника данных (Bybit / учебные) ---------- */
async function refreshDataBadge() {
    try {
        const s = await API.get('/api/market/status');
        state.dataSource = s.source;
        const b = document.getElementById('data-badge');
        if (s.bybit_ok) {
            b.textContent = '📡 Bybit Live';
            b.className = 'badge data-src ok';
            b.title = `Реальные данные с ${s.host || 'api.bybit.com'}`;
        } else {
            b.textContent = '⚠️ Учебные данные';
            b.className = 'badge data-src warn';
            b.title = 'Bybit API недоступен с этого компьютера — показаны учебные данные. '
                + (s.last_error ? ('Причина: ' + s.last_error) : '')
                + ' Проверьте интернет/VPN и нажмите «Проверить Bybit» в Настройках.';
        }
    } catch (e) {}
}

function setDemoBadge(demo) {
    const b = document.getElementById('demo-badge');
    if (demo) { b.textContent = 'AI DEMO'; b.className = 'badge'; }
    else { b.textContent = 'LIVE · AI'; b.className = 'badge live'; }
}

/* ---------- Символы (все монеты с суточным объёмом >= $1M) ---------- */
async function loadSymbols() {
    let symbols = [];
    try {
        // все монеты с объёмом > $1M за сутки
        const data = await API.get('/api/tickers?all=true');
        symbols = (data.tickers || []).map(t => t.symbol);
    } catch (e) {}
    if (!symbols.length) {
        try {
            const d = await API.get('/api/symbols');
            symbols = d.symbols || [];
        } catch (e) {}
    }
    state.symbols = symbols;
    // гарантируем присутствие LSKUSDT в списках, даже если кэш ещё старый
    if (!symbols.includes('LSKUSDT')) symbols.push('LSKUSDT');

    const sel = document.getElementById('chart-symbol');
    sel.innerHTML = symbols.map(s => `<option value="${s}">${s}</option>`).join('');
    if (symbols.length) state.currentSymbol = symbols[0];
    sel.value = state.currentSymbol;
    sel.addEventListener('change', () => { state.currentSymbol = sel.value; switchSymbol(); });

    const opts = symbols.map(s => `<option value="${s}">${s}</option>`).join('');
    document.getElementById('bt-symbol').innerHTML = opts;
    document.getElementById('opt-symbol').innerHTML = opts;
}

/* ---------- Стратегии ---------- */
async function loadStrategies() {
    try {
        const data = await API.get('/api/strategies');
        state.strategies = data.strategies || [];
        renderStrategiesList();
        const typeSel = document.getElementById('st-type');
        typeSel.innerHTML = state.strategies.map(s => `<option value="${s.type}">${s.name}</option>`).join('');
        document.getElementById('bt-strategy').innerHTML = state.strategies.map(s => `<option value="${s.type}">${s.name}</option>`).join('');
        typeSel.addEventListener('change', fillStrategyForm);
        fillStrategyForm();
    } catch (e) { log('system', 'Ошибка загрузки стратегий: ' + e.message); }
}

function renderStrategiesList() {
    const box = document.getElementById('strategies-list');
    box.innerHTML = state.strategies.map(s => `
        <div class="strategy-item" data-type="${s.type}">
            <h3>${s.name}</h3>
            <p>${s.description}</p>
            <div class="strategy-tags">
                <span class="tag">TP ${s.tp_pct}%</span>
                <span class="tag">SL ${s.sl_pct}%</span>
                <span class="tag">hold ≤ ${s.max_hold_hours}h</span>
                ${Object.entries(s.params).map(([k, v]) => `<span class="tag">${k}=${v}</span>`).join('')}
            </div>
        </div>
    `).join('');
    box.querySelectorAll('.strategy-item').forEach(el => {
        el.addEventListener('click', () => {
            box.querySelectorAll('.strategy-item').forEach(x => x.classList.remove('selected'));
            el.classList.add('selected');
            document.getElementById('st-type').value = el.dataset.type;
            fillStrategyForm();
        });
    });
}

function currentStrategyDef() {
    const t = document.getElementById('st-type').value;
    return state.strategies.find(s => s.type === t) || {};
}

function fillStrategyForm() {
    const def = currentStrategyDef();
    if (!def.type) return;
    document.getElementById('st-tp').value = def.tp_pct ?? 2;
    document.getElementById('st-sl').value = def.sl_pct ?? 1;
    document.getElementById('st-hold').value = def.max_hold_hours ?? 4;
    document.getElementById('st-params').value = JSON.stringify(def.params || {}, null, 2);
}

/* =========================================================================
   НАСТРОЙКИ (ключи, модели, правила) — редактирование из браузера
   ========================================================================= */
const ROLE_META = {
    strategist: { icon: '🧠', name: 'Ведущий стратег' },
    momentum: { icon: '⚡', name: 'Аналитик моментума' },
    contrarian: { icon: '🦅', name: 'Контрариан / сентимент' },
    macro: { icon: '💎', name: 'Макро-контекст' },
    risk: { icon: '🛡️', name: 'Риск-менеджер' },
};

async function loadSettings() {
    try {
        const cfg = await API.get('/api/config');
        state.config = cfg;
        renderSettings(cfg);
    } catch (e) {
        document.getElementById('settings-body').innerHTML =
            `<div class="empty" style="color:var(--red)">Ошибка загрузки настроек: ${e.message}</div>`;
    }
}

function renderSettings(cfg) {
    const box = document.getElementById('settings-body');
    const roles = cfg.roles.map(r => {
        const m = ROLE_META[r.key] || { icon: '🤖', name: r.key };
        return `
        <div class="role-row" data-role="${r.key}">
            <div>
                <div class="r-name">${m.icon} ${m.name}</div>
                <div class="r-model">${r.model}</div>
            </div>
            <input type="text" class="role-model" value="${escapeHtml(r.model)}" placeholder="модель (напр. gpt-6-astra)">
            <input type="password" class="role-key" value="${escapeHtml(r.api_key)}" placeholder="ключ (пусто = глобальный)">
            <span class="role-status wait">—</span>
        </div>`;
    }).join('');

    box.innerHTML = `
        <div class="settings-section">
            <h3>🔑 AI-подключение (endpoint)</h3>
            <div class="kv-row"><label>Base URL</label><input type="text" id="set-base-url" value="${escapeHtml(cfg.base_url)}"></div>
            <div class="kv-row"><label>Глобальный API-ключ</label><input type="password" id="set-global-key" value="${escapeHtml(cfg.global_api_key)}" placeholder="один ключ для всех моделей"></div>
            <p class="muted" style="margin-top:4px">Если ввести глобальный ключ — он подставится во все 5 моделей. Можно задать отдельный ключ для каждой роли ниже.</p>
            <p class="muted" style="margin-top:4px;background:rgba(108,140,255,.08);padding:8px 10px;border-radius:8px">
                💡 <b>Важно:</b> торговые <b>сигналы</b> считаются индикаторами математически и работают
                <b>без AI-ключа вообще</b> — поэтому сигналы уже появляются. Ключ нужен только для
                разделов «Анализ агентов» (ветки рассуждений) и «Ассистент». Вставили ключ → нажали «Сохранить» →
                сразу запустите «🤖 Анализ агентов» — всё заработает без перезапуска.
            </p>
        </div>
        <div class="settings-section">
            <h3>🤖 Модели по ролям</h3>
            ${roles}
        </div>
        <div class="settings-section">
            <h3>📈 Bybit (публичные данные)</h3>
            <div class="kv-row"><label>Соединение</label>
                <span id="bybit-conn" class="muted">не проверялось</span>
                <button id="ping-bybit-btn" class="btn btn-ghost btn-sm">🔍 Проверить Bybit</button>
            </div>
            <div class="kv-row"><label>Base URL</label><input type="text" id="set-bybit-url" value="${escapeHtml(cfg.bybit.base_url)}"></div>
            <div class="kv-row"><label>API Key</label><input type="password" id="set-bybit-key" value="${escapeHtml(cfg.bybit.api_key)}" placeholder="необязательно (нужен для реальных ордеров)"></div>
            <div class="kv-row"><label>API Secret</label><input type="password" id="set-bybit-secret" value="" placeholder="необязательно"></div>
            <p class="muted" style="margin-top:4px">Графики, тикеры и сигналы берутся из публичного API Bybit (ключи НЕ нужны).
            Если сверху справа бейдж «⚠️ Учебные данные» — Bybit недоступен с этого компьютера
            (интернет/VPN/файрвол), тогда показываются учебные данные. Нажмите «Проверить Bybit» после исправления.</p>
        </div>
        <div class="settings-section">
            <h3>🛡️ Правила торговли</h3>
            <div class="kv-row"><label>Макс. сделок в день</label><input type="number" id="set-max-trades" value="${cfg.rules.max_trades_per_day}" min="1"></div>
            <div class="kv-row"><label>Мин. Take-Profit, %</label><input type="number" id="set-min-tp" value="${cfg.rules.min_tp_pct}" step="0.1" min="0.1"></div>
            <div class="kv-row"><label>Макс. удержание, часов</label><input type="number" id="set-max-hold" value="${cfg.rules.max_hold_hours}" step="0.5" min="0.1"></div>
            <div class="kv-row"><label>Дефолт Stop-Loss, %</label><input type="number" id="set-sl" value="${cfg.rules.default_sl_pct}" step="0.1" min="0.1"></div>
            <div class="kv-row"><label>Порог уверенности (0-1)</label><input type="number" id="set-min-conf" value="${cfg.rules.min_confidence}" step="0.05" min="0" max="1"></div>
        </div>
    `;

    document.getElementById('test-models-btn').addEventListener('click', testModels);
    document.getElementById('save-settings-btn').addEventListener('click', saveSettings);
    const pingBtn = document.getElementById('ping-bybit-btn');
    if (pingBtn) pingBtn.addEventListener('click', pingBybit);
    refreshBybitConnLabel();
}

async function refreshBybitConnLabel() {
    try {
        const s = await API.get('/api/market/status');
        _bybitConnLabel(s);
    } catch (e) {}
}

function _bybitConnLabel(s) {
    const el = document.getElementById('bybit-conn');
    if (!el) return;
    if (s.bybit_ok) {
        el.innerHTML = `<span class="up">✓ Bybit отвечает</span> (${s.host || 'api.bybit.com'}${s.latency_ms != null ? ', ' + s.latency_ms + ' мс' : ''})`;
    } else {
        let html = `<span class="down">✗ недоступен</span> — показаны учебные данные`;
        if (s.results && s.results.length) {
            html += '<br>' + s.results.map(r =>
                `<span class="${r.ok ? 'up' : 'down'}">${r.ok ? '✓' : '✗'}</span> ` +
                `${r.host.replace('https://', '')} — ${escapeHtml(r.detail || 'ok')} (${r.latency_ms} мс)`
            ).join('<br>');
        } else if (s.last_error) {
            html += ' · <span class="muted">' + escapeHtml(s.last_error).slice(0, 90) + '</span>';
        }
        el.innerHTML = html;
    }
}

async function pingBybit() {
    const btn = document.getElementById('ping-bybit-btn');
    btn.disabled = true; btn.textContent = '⏳ Проверка…';
    try {
        const s = await API.post('/api/market/ping', {});
        _bybitConnLabel(s);
        refreshDataBadge();
        if (s.ok) {
            toast(`✅ Bybit доступен: ${s.host} (${s.latency_ms} мс). Реальные данные!`);
            refreshTickers(); loadCanvasCandles();
        } else {
            toast('❌ Все хосты Bybit недоступны. Возможный geo-block — попробуйте VPN.');
        }
    } catch (e) {
        toast('❌ Ошибка проверки: ' + e.message);
    } finally {
        btn.disabled = false; btn.textContent = '🔍 Проверить Bybit';
    }
}

async function saveSettings() {
    const btn = document.getElementById('save-settings-btn');
    btn.disabled = true; btn.textContent = '💾 Сохранение…';
    const updates = {};

    const baseUrl = val('set-base-url');
    const globalKey = val('set-global-key');
    const bybitKey = val('set-bybit-key');
    const bybitSecret = val('set-bybit-secret');
    const bybitUrl = val('set-bybit-url');

    if (baseUrl) updates.ai_base_url = baseUrl;
    if (globalKey && !globalKey.includes('…')) updates.ai_api_key = globalKey;
    if (bybitUrl) updates.bybit_base_url = bybitUrl;
    if (bybitKey && !bybitKey.includes('…')) updates.bybit_api_key = bybitKey;
    if (bybitSecret) updates.bybit_api_secret = bybitSecret;

    document.querySelectorAll('.role-row').forEach(row => {
        const role = row.dataset.role;
        const model = row.querySelector('.role-model').value.trim();
        const key = row.querySelector('.role-key').value.trim();
        if (model) updates[role + '_model'] = model;
        if (key && !key.includes('…')) updates[role + '_api_key'] = key;
    });

    updates.max_trades_per_day = parseInt(val('set-max-trades')) || 30;
    updates.min_tp_pct = parseFloat(val('set-min-tp')) || 2;
    updates.max_hold_hours = parseFloat(val('set-max-hold')) || 4;
    updates.default_sl_pct = parseFloat(val('set-sl')) || 1;
    updates.min_confidence = parseFloat(val('set-min-conf')) || 0.55;

    try {
        const res = await API.post('/api/config', { updates });
        state.config = res.config;
        setDemoBadge(res.config.demo_mode);
        log('system', `Настройки сохранены (${res.applied.length} параметров). Файл: .env`);
        toast('✅ Настройки сохранены и записаны в .env');
        await loadSettings();
    } catch (e) {
        toast('❌ Ошибка: ' + e.message);
    } finally {
        btn.disabled = false; btn.textContent = '💾 Сохранить';
    }
}

async function testModels() {
    const btn = document.getElementById('test-models-btn');
    btn.disabled = true; btn.textContent = '⏳ Проверка…';
    document.querySelectorAll('.role-status').forEach(s => { s.textContent = '…'; s.className = 'role-status wait'; });
    try {
        // сначала сохранить текущие значения, чтобы проверить именно их
        const res = await API.post('/api/config/test?role=all');
        const results = res.results;
        document.querySelectorAll('.role-row').forEach(row => {
            const role = row.dataset.role;
            const st = row.querySelector('.role-status');
            const r = results[role];
            if (r && r.ok) {
                st.textContent = `✓ ${r.latency_ms}мс`;
                st.className = 'role-status ok';
            } else {
                st.textContent = '✗ нет ключа / ошибка';
                st.className = 'role-status err';
                if (r && r.error) st.title = r.error;
            }
        });
        if (res.demo_mode) toast('Все ключи пусты — сейчас работает DEMO-режим');
        else toast('Проверка завершена. Зелёный ✓ = модель доступна');
    } catch (e) {
        toast('Ошибка: ' + e.message);
    } finally {
        btn.disabled = false; btn.textContent = '🔌 Проверить все модели';
    }
}

function val(id) { const el = document.getElementById(id); return el ? el.value.trim() : ''; }

/* =========================================================================
   DASHBOARD: тикеры
   ========================================================================= */
async function refreshTickers() {
    try {
        const data = await API.get('/api/tickers?all=true');
        renderTickers(data.tickers || []);
    } catch (e) {}
}

function renderTickers(tickers) {
    const box = document.getElementById('coins-list');
    // показываем топ-200 по объёму (список уже отсортирован)
    const shown = tickers.slice(0, 200);
    let header = '';
    if (tickers.length > shown.length) {
        header = `<div class="muted" style="padding:8px 12px">Показаны топ-${shown.length} из ${tickers.length} монет (объём ≥ $1M/сут)</div>`;
    }
    const items = shown.map(t => {
        const cls = t.change24h >= 0 ? 'up' : 'down';
        const sign = t.change24h >= 0 ? '+' : '';
        return `
            <div class="coin-item ${t.symbol === state.currentSymbol ? 'selected' : ''}" data-symbol="${t.symbol}">
                <div>
                    <div class="coin-name">${t.symbol}</div>
                    <div class="coin-change ${cls}">${sign}${fmt(t.change24h)}%</div>
                </div>
                <div class="coin-last">${fmtPrice(t.last)}</div>
            </div>`;
    }).join('');
    box.innerHTML = header + items;
    box.querySelectorAll('.coin-item').forEach(el => {
        el.addEventListener('click', () => {
            state.currentSymbol = el.dataset.symbol;
            document.getElementById('chart-symbol').value = state.currentSymbol;
            box.querySelectorAll('.coin-item').forEach(x => x.classList.remove('selected'));
            el.classList.add('selected');
            switchSymbol();
        });
    });
}

/* =========================================================================
   CHART: по умолчанию собственный canvas-график (всегда работает),
   TradingView — опционально по кнопке.
   ========================================================================= */
let chartMode = 'canvas'; // 'canvas' | 'tv'

function initChart() {
    const tfGroup = document.getElementById('tf-group');
    ['1m', '3m', '5m', '15m', '30m', '1h', '4h'].forEach(tf => {
        const b = document.createElement('button');
        b.className = 'tf-btn' + (tf === state.currentTF ? ' active' : '');
        b.textContent = tf;
        b.addEventListener('click', () => {
            state.currentTF = tf;
            tfGroup.querySelectorAll('.tf-btn').forEach(x => x.classList.remove('active'));
            b.classList.add('active');
            switchSymbol();
        });
        tfGroup.appendChild(b);
    });

    // переключение режима графика
    document.getElementById('candles-toggle').addEventListener('click', showCanvasChart);
    document.getElementById('tv-toggle').addEventListener('click', showTradingView);
    document.getElementById('analyze-btn').addEventListener('click', runAnalysis);

    // по умолчанию — canvas
    showCanvasChart();

    // авто-обновление canvas-графика
    setInterval(() => { if (chartMode === 'canvas') loadCanvasCandles(); }, 30000);
}

function setModeButtons() {
    document.getElementById('candles-toggle').classList.toggle('active', chartMode === 'canvas');
    document.getElementById('tv-toggle').classList.toggle('active', chartMode === 'tv');
}

function showCanvasChart() {
    chartMode = 'canvas';
    setModeButtons();
    document.getElementById('tradingview-chart').style.display = 'none';
    document.getElementById('chart-fallback').style.display = '';
    loadCanvasCandles();
}

function showTradingView() {
    chartMode = 'tv';
    setModeButtons();
    // уже создан — просто показываем
    if (state.tvWidget) {
        document.getElementById('tradingview-chart').style.display = '';
        document.getElementById('chart-fallback').style.display = 'none';
        return;
    }
    // загружаем скрипт TradingView лениво (только по запросу пользователя)
    if (!window.TradingView) {
        if (state.tvLoading) return;
        state.tvLoading = true;
        const s = document.createElement('script');
        s.src = 'https://s3.tradingview.com/tv.js';
        s.onload = () => { state.tvLoading = false; createTvWidget(); };
        s.onerror = () => {
            state.tvLoading = false;
            toast('TradingView недоступен (нет доступа к s3.tradingview.com). Используем свой график.');
            chartMode = 'canvas';
            setModeButtons();
            showCanvasChart();
        };
        document.head.appendChild(s);
        return;
    }
    createTvWidget();
}

function createTvWidget() {
    try {
        state.tvWidget = new TradingView.widget({
            container_id: 'tradingview-chart',
            autosize: true,
            symbol: `BYBIT:${state.currentSymbol}`,
            interval: state.currentTF,
            timezone: 'Etc/UTC',
            theme: 'dark',
            style: '1',
            locale: 'ru',
            toolbar_bg: '#131928',
            enable_publishing: false,
            hide_side_toolbar: false,
            allow_symbol_change: false,
        });
        document.getElementById('tradingview-chart').style.display = '';
        document.getElementById('chart-fallback').style.display = 'none';
    } catch (e) {
        toast('Ошибка TradingView. Используем свой график.');
        chartMode = 'canvas';
        setModeButtons();
        showCanvasChart();
    }
}

async function loadCanvasCandles() {
    try {
        const data = await API.get(`/api/klines/${state.currentSymbol}?interval=${state.currentTF}&limit=150`);
        drawCandles(document.getElementById('candles-canvas'), data.candles || [],
            { symbol: state.currentSymbol, interval: state.currentTF });
        const src = document.getElementById('chart-data-src');
        if (src) {
            const isBB = (data.data_source || state.dataSource) === 'bybit';
            src.textContent = isBB ? '📡 Bybit' : '⚠️ учебные';
            src.className = 'badge data-src ' + (isBB ? 'ok' : 'warn');
        }
    } catch (e) {}
}

function switchSymbol() {
    if (chartMode === 'canvas') {
        loadCanvasCandles();
    } else if (state.tvWidget) {
        try { state.tvWidget.setSymbol(`BYBIT:${state.currentSymbol}`, state.currentTF); } catch (e) {}
    }
}

function resizeChart() {
    if (chartMode === 'canvas') loadCanvasCandles();
}

/* =========================================================================
   МУЛЬТИАГЕНТНЫЙ АНАЛИЗ (стриминг + дерево размышлений)
   ========================================================================= */
const AGENT_ORDER = ['strategist', 'momentum', 'contrarian', 'macro', 'risk'];

/* Цели рендера дерева (дашборд / вкладка «Монета») */
let treeTarget = { rootId: 'tree-root', labelId: 'consensus-symbol' };

function runAnalysis() {
    startAnalysis({
        symbol: state.currentSymbol,
        strategy: document.getElementById('bt-strategy').value,
        timeframe: state.currentTF,
        btn: document.getElementById('analyze-btn'),
        bodyId: 'consensus-body',
        labelId: 'consensus-symbol',
        rootId: 'tree-root',
    });
}

async function startAnalysis(cfg) {
    const btn = cfg.btn;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Анализ…';
    setAgentState('all', 'thinking');

    // закрыть предыдущий стрим
    if (state.es) { state.es.close(); state.es = null; }

    const { symbol, strategy, timeframe } = cfg;
    treeTarget = { rootId: cfg.rootId, labelId: cfg.labelId };

    const body = document.getElementById(cfg.bodyId);
    document.getElementById(cfg.labelId).textContent = `${symbol} · ${timeframe}`;
    body.innerHTML = `<div class="tree-wrap"><div class="tree-root" id="${cfg.rootId}"></div></div>`;

    // используем SSE (fallback на обычный POST, если EventSource недоступен)
    const url = `/api/analyze/stream?symbol=${symbol}&strategy=${strategy}&timeframe=${timeframe}`;
    const fin = () => finishAnalysis(btn);
    if (typeof EventSource !== 'undefined') {
        state.es = new EventSource(url);
        state.es.addEventListener('snapshot', e => onSnapshot(JSON.parse(e.data)));
        state.es.addEventListener('agent', e => onAgentVerdict(JSON.parse(e.data)));
        state.es.addEventListener('consensus', e => onConsensus(JSON.parse(e.data)));
        state.es.addEventListener('error', e => { try { onStreamError(JSON.parse(e.data)); } catch (_) {} });
        state.es.addEventListener('done', fin);
        state.es.onerror = () => { fin(); if (state.es) state.es.close(); state.es = null; };
    } else {
        try {
            const data = await API.post('/api/analyze', { symbol, strategy, timeframe });
            onSnapshot({ symbol, timeframe, price: data.price, features: data.features, strategy: data.strategy });
            data.agents.forEach(onAgentVerdict);
            onConsensus({ consensus: data.consensus, signal: data.signal });
        } catch (e) { log('error', 'Ошибка анализа: ' + e.message); }
        fin();
    }
}

function finishAnalysis(btn) {
    if (btn) { btn.disabled = false; btn.innerHTML = '🤖 Анализ агентов'; }
    setAgentState('all', 'active');
    if (state.es) { state.es.close(); state.es = null; }
}

function onSnapshot(d) {
    const root = document.getElementById(treeTarget.rootId);
    if (!root) return;
    document.getElementById(treeTarget.labelId).textContent =
        `${d.symbol} · ${d.timeframe} · цена ${fmtPrice(d.price)} · стратегия: ${d.strategy.name}`;
    const sig = d.strategy_signal;
    root.insertAdjacentHTML('beforeend', `
        <div class="tree-branch">
            <div class="tree-agent" style="opacity:.75">
                <span class="t-icon">📊</span>
                <div>
                    <div class="t-name">Индикаторная стратегия · <span class="muted">${d.strategy.name}</span></div>
                    <div class="a-verdict" style="margin-top:4px">
                        <span class="pill ${sig && sig.side === 'LONG' ? 'long' : sig && sig.side === 'SHORT' ? 'short' : 'neutral'}">${sig ? sig.side : '—'}</span>
                        <span class="muted">${sig && sig.reason ? sig.reason : ''}</span>
                    </div>
                </div>
            </div>
        </div>
        <div class="thinking-indicator"><span class="pulse"></span>5 агентов размышляют…</div>
    `);
}

function onAgentVerdict(a) {
    const root = document.getElementById(treeTarget.rootId);
    if (!root) return;
    // убираем индикатор загрузки
    root.querySelectorAll('.thinking-indicator').forEach(el => el.remove());
    const steps = (a.thinking || []).map(s => `<div class="t-step">${escapeHtml(s)}</div>`).join('');
    root.insertAdjacentHTML('beforeend', `
        <div class="tree-branch">
            <div class="tree-agent">
                <span class="t-icon">${a.icon}</span>
                <div style="flex:1">
                    <div style="display:flex;justify-content:space-between;align-items:center">
                        <span class="t-name">${a.name}</span>
                        <span class="muted" style="font-size:10px;font-family:var(--mono)">${a.demo ? 'DEMO' : a.model}</span>
                    </div>
                    <div class="a-verdict" style="margin-top:4px">
                        <span class="pill ${a.side === 'LONG' ? 'long' : a.side === 'SHORT' ? 'short' : 'neutral'}">${a.side}</span>
                        <span>conf ${fmt(a.confidence * 100, 0)}%</span>
                        ${a.tokens_est ? `<span class="muted" style="font-size:10px">≈${a.tokens_est.total} ток.</span>` : ''}
                    </div>
                    ${steps ? `<div class="t-steps">${steps}</div>` : ''}
                    ${a.reason ? `<div class="a-reason">${escapeHtml(a.reason).slice(0, 200)}</div>` : ''}
                </div>
            </div>
        </div>
    `);
    root.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

function onConsensus(d) {
    const root = document.getElementById(treeTarget.rootId);
    if (!root) return;
    const cons = d.consensus, sig = d.signal;
    const confPct = Math.round(cons.confidence * 100);
    root.insertAdjacentHTML('beforeend', `
        <div class="tree-consensus">
            <div class="consensus-verdict" style="margin-bottom:8px">
                <div class="verdict-badge ${cons.side}">${cons.side}</div>
                <div>
                    <div class="muted">Уверенность консенсуса</div>
                    <div style="font-size:20px;font-weight:700;font-family:var(--mono)">${confPct}%</div>
                    <div class="conf-bar" style="width:200px"><div style="width:${confPct}%"></div></div>
                </div>
                <div>
                    <div class="muted">Действие</div>
                    <div class="pill ${sig.action === 'BUY' ? 'long' : sig.action === 'SELL' ? 'short' : 'neutral'}" style="font-size:14px">${sig.action || 'WAIT'}</div>
                </div>
                <div style="flex:1" class="muted">${escapeHtml(sig.reason || '')}</div>
            </div>
        </div>
    `);
}

function onStreamError(d) {
    const root = document.getElementById(treeTarget.rootId);
    if (root) root.insertAdjacentHTML('beforeend', `<div class="empty" style="color:var(--red)">Ошибка: ${escapeHtml(d.message || '')}</div>`);
    log('error', 'Ошибка стриминга: ' + (d.message || ''));
}

/* =========================================================================
   BACKTEST
   ========================================================================= */
let lastBacktest = null;

async function runBacktest(e) {
    e.preventDefault();
    const btn = e.target.querySelector('button[type=submit]');
    btn.disabled = true; btn.textContent = '⏳ Расчёт…';
    try {
        let params = {};
        try { params = JSON.parse(document.getElementById('bt-params').value || '{}'); } catch { throw new Error('Некорректный JSON параметров'); }
        const data = await API.post('/api/backtest', {
            symbol: document.getElementById('bt-symbol').value,
            strategy: document.getElementById('bt-strategy').value,
            timeframe: document.getElementById('bt-timeframe').value,
            params,
            tp_pct: parseFloat(document.getElementById('bt-tp').value),
            sl_pct: parseFloat(document.getElementById('bt-sl').value),
            max_hold_hours: parseFloat(document.getElementById('bt-hold').value),
            start_date: (document.getElementById('bt-start') || {}).value || null,
            end_date: (document.getElementById('bt-end') || {}).value || null,
        });
        lastBacktest = data;
        renderBacktest(data);
        log('system', `Бэктест ${data.symbol} (${data.strategy}): ${data.total_trades} сделок, PnL ${fmt(data.total_pnl_pct)}%`);
    } catch (err) {
        document.getElementById('backtest-results').innerHTML = `<div class="empty" style="color:var(--red)">${err.message}</div>`;
    } finally {
        btn.disabled = false; btn.textContent = '▶ Запустить бэктест';
    }
}

function _srcBadge(source) {
    return source === 'bybit'
        ? '<span class="badge data-src ok" style="margin-left:6px">📡 Bybit</span>'
        : '<span class="badge data-src warn" style="margin-left:6px">⚠️ учебные данные</span>';
}

function renderBacktest(d) {
    const res = document.getElementById('backtest-results');
    res.innerHTML = `
        <div class="muted" style="margin-bottom:10px">${d.symbol} · ${d.timeframe} · ${d.strategy}${d.params ? ' · ' + Object.entries(d.params).map(([k,v])=>k+'='+v).join(', ') : ''}${d.start_date ? ` · период ${d.start_date} → ${d.end_date || 'сегодня'}` : ' · последние 24ч'}${_srcBadge(d.data_source)}</div>
        <div class="metrics-grid">
            ${metric('Сделок', d.total_trades)}
            ${metric('Win rate', fmt(d.win_rate) + '%', d.win_rate >= 50)}
            ${metric('Сумм. PnL', fmt(d.total_pnl_pct) + '%', d.total_pnl_pct >= 0)}
            ${metric('Profit factor', d.profit_factor === null ? '∞' : fmt(d.profit_factor))}
            ${metric('Max drawdown', fmt(d.max_drawdown_pct) + '%', false)}
            ${metric('Ср. профит', fmt(d.avg_win_pct) + '%', true)}
            ${metric('Ср. убыток', fmt(d.avg_loss_pct) + '%', false)}
            ${metric('Ср. удержание', fmt(d.avg_hold_hours, 2) + 'ч')}
            ${metric('Sharpe', fmt(d.sharpe, 2))}
        </div>
        <div class="metrics-grid" style="margin-top:10px">
            ${metric('Старт $1000 → конец', '$' + fmt(d.end_1000, 0), d.end_1000 >= 1000)}
            ${metric('Доходность периода', (d.return_pct >= 0 ? '+' : '') + fmt(d.return_pct) + '%', d.return_pct >= 0)}
            ${metric('Начальный капитал', '$' + fmt(d.initial_capital, 0))}
            ${metric('Конечный капитал', '$' + fmt(d.final_equity, 0), d.final_equity >= d.initial_capital)}
        </div>`;
    drawEquity(document.getElementById('equity-canvas'), d.equity_curve || []);
    drawCandlesWithTrades(document.getElementById('trades-canvas'), d.candles || [], d.trades || []);
    renderTradesTable(d.trades || []);
}

function metric(label, value, positive) {
    const cls = positive === undefined ? '' : positive ? 'pos' : 'neg';
    return `<div class="metric"><div class="m-label">${label}</div><div class="m-value ${cls}">${value}</div></div>`;
}

function renderTradesTable(trades) {
    const box = document.getElementById('trades-table');
    if (!trades.length) { box.innerHTML = '<div class="empty">Сделок нет</div>'; return; }
    const rows = trades.slice().reverse().slice(0, 200).map(t => `
        <tr>
            <td>${t.id}</td>
            <td><span class="pill ${t.side === 'LONG' ? 'long' : 'short'}">${t.side}</span></td>
            <td>${fmtTime(t.entry_ts)}</td>
            <td>${fmtPrice(t.entry)}</td>
            <td>${fmtPrice(t.exit)}</td>
            <td>${fmt(t.hold_hours, 2)}ч</td>
            <td><span class="pill ${t.exit_reason.toLowerCase()}">${t.exit_reason}</span></td>
            <td class="${t.pnl_pct >= 0 ? 'up' : 'down'}">${t.pnl_pct >= 0 ? '+' : ''}${fmt(t.pnl_pct)}%</td>
        </tr>`).join('');
    box.innerHTML = `<table class="data">
        <thead><tr><th>#</th><th>Сторона</th><th>Вход</th><th>Цена входа</th><th>Цена выхода</th><th>Длит.</th><th>Причина</th><th>PnL</th></tr></thead>
        <tbody>${rows}</tbody></table>`;
}

/* =========================================================================
   БЭКТЕСТ ПО ВСЕМ СТРАТЕГИЯМ (сравнение в один клик)
   ========================================================================= */
async function runBacktestAll() {
    const btn = document.getElementById('backtest-all-btn');
    btn.disabled = true; btn.textContent = '⏳ Прогон 23 стратегий…';
    try {
        const payload = {
            symbol: document.getElementById('bt-symbol').value,
            timeframe: document.getElementById('bt-timeframe').value,
            tp_pct: parseFloat(document.getElementById('bt-tp').value),
            sl_pct: parseFloat(document.getElementById('bt-sl').value),
            max_hold_hours: parseFloat(document.getElementById('bt-hold').value),
            start_date: (document.getElementById('bt-start') || {}).value || null,
            end_date: (document.getElementById('bt-end') || {}).value || null,
        };
        const data = await API.post('/api/backtest/all', payload);
        renderBacktestAll(data);
        log('system', `Бэктест всех стратегий для ${data.symbol}: лучшая → ${data.best ? data.best.name : '—'} (win rate ${data.best ? fmt(data.best.win_rate) : 0}%)`);
    } catch (err) {
        document.getElementById('backtest-results').innerHTML = `<div class="empty" style="color:var(--red)">${err.message}</div>`;
    } finally {
        btn.disabled = false; btn.textContent = '⚡ Все 23 стратегии';
    }
}

function renderBacktestAll(d) {
    const res = document.getElementById('backtest-results');
    if (!d.results || !d.results.length) {
        res.innerHTML = '<div class="empty">Нет результатов</div>';
        return;
    }
    const best = d.best;
    const rows = d.results.map((s, i) => {
        const isBest = best && s.type === best.type;
        return `
        <tr style="${isBest ? 'background:rgba(52,211,153,.08)' : ''}">
            <td>${isBest ? '🏆' : i + 1}</td>
            <td>${s.name} ${isBest ? '<span class="pill long">лучшая</span>' : ''}</td>
            <td class="${s.win_rate >= 55 ? 'up' : 'down'}">${fmt(s.win_rate)}%</td>
            <td class="${s.pnl_pct >= 0 ? 'up' : 'down'}">${fmt(s.pnl_pct)}%</td>
            <td>${s.trades}</td>
            <td>${s.profit_factor === null ? '∞' : fmt(s.profit_factor)}</td>
            <td>${fmt(s.max_drawdown)}%</td>
            <td class="${s.end_1000 >= 1000 ? 'up' : 'down'}">$${fmt(s.end_1000, 0)}</td>
            <td><button class="btn btn-ghost btn-sm" data-bt="${s.type}">Открыть</button></td>
        </tr>`;
    }).join('');

    res.innerHTML = `
        <div class="muted" style="margin-bottom:10px">${d.symbol} · ${d.timeframe} · TP ${d.tp_pct}% / SL ${d.sl_pct}% · ${d.candles} свечей${d.start_date ? ` · ${d.start_date} → ${d.end_date || 'сегодня'}` : ' · последние 24ч'}${_srcBadge(d.data_source)}</div>
        <div class="metrics-grid" style="margin-bottom:12px">
            ${metric('Лучшая стратегия', best ? best.name : '—')}
            ${metric('Win rate', best ? fmt(best.win_rate) + '%' : '—', best && best.win_rate >= 50)}
            ${metric('$1000 → конец', best ? '$' + fmt(best.end_1000, 0) : '—', best && best.end_1000 >= 1000)}
            ${metric('Стратегий всего', d.results.length)}
        </div>
        <div class="table-wrap">
            <table class="data">
                <thead><tr><th>#</th><th>Стратегия</th><th>Win rate</th><th>PnL</th><th>Сделок</th><th>PF</th><th>DD</th><th>$1000 →</th><th></th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
        </div>`;

    res.querySelectorAll('[data-bt]').forEach(b => {
        b.addEventListener('click', () => {
            document.getElementById('bt-strategy').value = b.dataset.bt;
            toast(`Стратегия "${b.dataset.bt}" подставлена в форму. Нажмите «▶ Запустить бэктест» для деталей.`);
        });
    });
}

function redrawEquity() {
    if (lastBacktest) drawEquity(document.getElementById('equity-canvas'), lastBacktest.equity_curve || []);
}

function saveStrategy(e) {
    e.preventDefault();
    document.getElementById('bt-strategy').value = document.getElementById('st-type').value;
    document.getElementById('bt-tp').value = document.getElementById('st-tp').value;
    document.getElementById('bt-sl').value = document.getElementById('st-sl').value;
    document.getElementById('bt-hold').value = document.getElementById('st-hold').value;
    document.getElementById('bt-params').value = document.getElementById('st-params').value;
    document.querySelector('.nav-btn[data-tab="backtest"]').click();
    log('system', `Стратегия "${currentStrategyDef().name}" готова к бэктесту`);
}

/* =========================================================================
   ОПТИМИЗАЦИЯ (подбор лучших стратегий под монету)
   ========================================================================= */
async function runOptimize() {
    const btn = document.getElementById('optimize-btn');
    const status = document.getElementById('optimize-status');
    btn.disabled = true; btn.textContent = '⏳ Прогон всех стратегий…';
    status.textContent = 'прогоняю 23 стратегии…';
    try {
        const data = await API.post('/api/optimize', {
            symbol: document.getElementById('opt-symbol').value,
            timeframe: document.getElementById('opt-timeframe').value,
        });
        renderOptimize(data);
        status.textContent = `готово: отобрано ${data.top_strategies.length} стратегий`;
        log('system', `Оптимизация ${data.symbol}: лучшие стратегии → ${data.top_strategies.join(', ')} (используются в live)`);
    } catch (e) {
        status.textContent = 'ошибка';
        log('error', 'Ошибка оптимизации: ' + e.message);
    } finally {
        btn.disabled = false; btn.textContent = '⚡ Подобрать лучшие стратегии';
    }
}

function renderOptimize(d) {
    const box = document.getElementById('optimize-results');
    if (d.error) { box.innerHTML = `<div class="empty" style="color:var(--red)">${d.error}</div>`; return; }

    const selected = d.selected || [];
    const topRow = selected.map(s => `
        <div class="strategy-item" style="margin-bottom:6px;cursor:pointer" data-type="${s.type}">
            <h3>${s.name} <span class="pill long">✓ отобрано</span></h3>
            <div style="display:flex;gap:14px;margin-top:6px;font-family:var(--mono);font-size:12px">
                <span class="up">win rate ${fmt(s.win_rate)}%</span>
                <span>PnL ${fmt(s.pnl_pct)}%</span>
                <span>сделок ${s.trades}</span>
                <span class="muted">score ${s.score}</span>
            </div>
        </div>`).join('');

    // таблица всех стратегий (сортированы)
    const rows = (d.strategies || []).slice(0, 23).map(s => `
        <tr class="${s.selected ? '' : ''}">
            <td>${s.selected ? '✅' : ''} ${s.name}</td>
            <td class="${s.win_rate >= 55 ? 'up' : ''}">${fmt(s.win_rate)}%</td>
            <td class="${s.pnl_pct >= 0 ? 'up' : 'down'}">${fmt(s.pnl_pct)}%</td>
            <td>${s.trades}</td>
            <td>${s.profit_factor === null ? '∞' : fmt(s.profit_factor)}</td>
            <td>${fmt(s.max_drawdown)}%</td>
            <td>${fmt(s.score)}</td>
            <td><button class="btn btn-ghost btn-sm" data-bt="${s.type}">Бэктест</button></td>
        </tr>`).join('');

    box.innerHTML = `
        ${selected.length ? `<div class="muted" style="margin-bottom:8px">Эти стратегии теперь используются в live-режиме для ${d.symbol} (авто-подбор):</div>` + `<div class="agents-row" style="grid-template-columns:repeat(auto-fill,minmax(240px,1fr))">${topRow}</div>` : `<div class="empty">Не нашлось стратегий, прошедших фильтры (сделок ≥ ${d.filters.min_trades}, win rate ≥ ${d.filters.min_win_rate}%)</div>`}
        <div class="table-wrap" style="margin-top:12px">
            <table class="data">
                <thead><tr><th>Стратегия</th><th>Win rate</th><th>PnL</th><th>Сделок</th><th>PF</th><th>DD</th><th>Score</th><th></th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
        </div>`;

    // клик по стратегии → подставить в бэктест
    box.querySelectorAll('[data-bt]').forEach(b => {
        b.addEventListener('click', () => {
            document.getElementById('bt-strategy').value = b.dataset.bt;
            document.querySelector('.nav-btn[data-tab="backtest"]').click();
            document.querySelector('.nav-btn[data-tab="backtest"]').scrollIntoView();
            log('system', `Стратегия ${b.dataset.bt} подставлена в бэктест`);
        });
    });
}

/* =========================================================================
   LIVE
   ========================================================================= */
function renderLive() {
    const s = state.live || {};
    const stats = document.getElementById('live-stats');
    stats.innerHTML = `
        ${metric('Сделок сегодня', `${s.day_trades || 0} / ${s.day_limit || 30}`)}
        ${metric('Закрыто сигналов', s.closed_count || 0)}
        ${metric('Win rate', (s.win_rate || 0) + '%', (s.win_rate || 0) >= 50)}
        ${metric('Ср. PnL', fmt(s.avg_pnl) + '%', (s.avg_pnl || 0) >= 0)}
        ${metric('Интервал скана', (s.scan_interval || 0) + 'с')}`;

    const active = (s.active || []).map(renderSignalCard).join('');
    document.getElementById('active-signals').innerHTML = active || '<div class="empty">Нет активных сигналов</div>';

    const hist = (s.history || []).slice().reverse().map(sg => `
        <tr>
            <td>${fmtTime(sg.opened_at)}</td>
            <td>${sg.symbol}</td>
            <td><span class="pill ${sg.side === 'LONG' ? 'long' : 'short'}">${sg.side}</span></td>
            <td>${fmtPrice(sg.entry)}</td>
            <td>${sg.status === 'CLOSED' ? fmtPrice(sg.exit_price) : '—'}</td>
            <td>${sg.status === 'CLOSED' ? (sg.pnl_pct >= 0 ? '+' : '') + fmt(sg.pnl_pct) + '%' : '—'}</td>
            <td>${sg.status === 'CLOSED' ? `<span class="pill ${sg.exit_reason.toLowerCase()}">${sg.exit_reason}</span>` : '<span class="pill neutral">OPEN</span>'}</td>
        </tr>`).join('');
    document.getElementById('signal-history').innerHTML =
        `<table class="data"><thead><tr><th>Время</th><th>Символ</th><th>Сторона</th><th>Вход</th><th>Выход</th><th>PnL</th><th>Статус</th></tr></thead><tbody>${hist}</tbody></table>`;
}

function renderSignalCard(sg) {
    return `
        <div class="signal-card open">
            <div class="s-head">
                <span class="s-symbol">${sg.symbol}</span>
                <span class="pill ${sg.side === 'LONG' ? 'long' : 'short'}">${sg.side}</span>
                <span class="muted">conf ${fmt(sg.confidence * 100, 0)}%</span>
            </div>
            <div class="s-levels">
                <span>Вход: ${fmtPrice(sg.entry)}</span>
                <span class="up">TP: ${fmtPrice(sg.tp)} (${sg.tp_pct}%)</span>
                <span class="down">SL: ${fmtPrice(sg.sl)} (${sg.sl_pct}%)</span>
            </div>
        </div>`;
}

function renderRules() {
    const r = state.rules || {};
    document.getElementById('rules-grid').innerHTML = `
        ${metric('Макс. сделок / день', r.max_trades_per_day ?? 30)}
        ${metric('Мин. TP', (r.min_tp_pct ?? 2) + '%')}
        ${metric('Макс. удержание', (r.max_hold_hours ?? 4) + 'ч')}
        ${metric('Дефолт SL', (r.default_sl_pct ?? 1) + '%')}`;
}

async function refreshLiveState() {
    try {
        const s = await API.get('/api/live/state');
        state.live = s;
        renderLive();
    } catch (e) {}
}

function initLive() { refreshLiveState(); }

/* =========================================================================
   РАЗДЕЛ «МОНЕТА»: список Bybit (по алфавиту), график, анализ, live 1-на-1
   ========================================================================= */
function initCoinTab() {
    // таймфреймы графика монеты
    const tfGroup = document.getElementById('coin-tf-group');
    ['1m', '3m', '5m', '15m', '30m', '1h', '4h'].forEach(tf => {
        const b = document.createElement('button');
        b.className = 'tf-btn' + (tf === state.coin.tf ? ' active' : '');
        b.textContent = tf;
        b.addEventListener('click', () => {
            state.coin.tf = tf;
            tfGroup.querySelectorAll('.tf-btn').forEach(x => x.classList.remove('active'));
            b.classList.add('active');
            loadCoinChart();
        });
        tfGroup.appendChild(b);
    });

    // стратегии в «Live 1-на-1» (auto + все 23)
    const slSt = document.getElementById('sl-strategy');
    state.strategies.forEach(s => {
        const o = document.createElement('option');
        o.value = s.type; o.textContent = s.name;
        slSt.appendChild(o);
    });

    document.getElementById('coin-filter').addEventListener('input', renderCoinList);
    document.getElementById('coin-refresh').addEventListener('click', () => loadCoinList(true));
    document.getElementById('coin-analyze-btn').addEventListener('click', runCoinAnalysis);
    document.getElementById('single-start').addEventListener('click', singleStart);
    document.getElementById('single-apply').addEventListener('click', singleApply);
    document.getElementById('single-stop').addEventListener('click', singleStop);

    loadCoinList();
    refreshSingleState();
    setInterval(() => {
        if (document.getElementById('panel-coin').classList.contains('active')) loadCoinChart();
    }, 30000);
    setInterval(() => {
        if (document.getElementById('panel-coin').classList.contains('active')) loadCoinList();
    }, 60000); // список монет обновляется с Bybit
}

async function loadCoinList(force) {
    try {
        const data = await API.get('/api/tickers?all=true');
        let tickers = data.tickers || [];
        if (!tickers.find(t => t.symbol === 'LSKUSDT')) {
            tickers.push({ symbol: 'LSKUSDT', last: 0, change24h: 0, volume24h: 0 });
        }
        // ПО АЛФАВИТУ (не по объёму)
        tickers.sort((a, b) => a.symbol.localeCompare(b.symbol));
        state.coin.allCoins = tickers;
        renderCoinList();
    } catch (e) {}
}

function renderCoinList() {
    const box = document.getElementById('coin-alpha-list');
    const q = (document.getElementById('coin-filter').value || '').trim().toUpperCase();
    const list = state.coin.allCoins.filter(t => !q || t.symbol.includes(q));
    if (!list.length) { box.innerHTML = '<div class="empty">Ничего не найдено</div>'; return; }
    box.innerHTML = list.map(t => {
        const cls = t.change24h >= 0 ? 'up' : 'down';
        const sign = t.change24h >= 0 ? '+' : '';
        return `
            <div class="coin-item ${t.symbol === state.coin.symbol ? 'selected' : ''}" data-symbol="${t.symbol}">
                <div>
                    <div class="coin-name">${t.symbol}</div>
                    <div class="coin-change ${cls}">${sign}${fmt(t.change24h)}%</div>
                </div>
                <div class="coin-last">${t.last ? fmtPrice(t.last) : '—'}</div>
            </div>`;
    }).join('');
    box.querySelectorAll('.coin-item').forEach(el => {
        el.addEventListener('click', () => selectCoin(el.dataset.symbol));
    });
}

function selectCoin(symbol) {
    state.coin.symbol = symbol;
    document.getElementById('coin-symbol-label').textContent = symbol;
    document.querySelectorAll('#coin-alpha-list .coin-item').forEach(x =>
        x.classList.toggle('selected', x.dataset.symbol === symbol));
    loadCoinChart();
}

async function loadCoinChart() {
    if (!state.coin.symbol) return;
    document.getElementById('coin-symbol-label').textContent = state.coin.symbol;
    try {
        const data = await API.get(`/api/klines/${state.coin.symbol}?interval=${state.coin.tf}&limit=150`);
        drawCandles(document.getElementById('coin-canvas'), data.candles || [],
            { symbol: state.coin.symbol, interval: state.coin.tf });
    } catch (e) {}
}

function runCoinAnalysis() {
    const strategy = document.getElementById('sl-strategy').value || 'auto';
    startAnalysis({
        symbol: state.coin.symbol,
        strategy: strategy,
        timeframe: state.coin.tf,
        btn: document.getElementById('coin-analyze-btn'),
        bodyId: 'coin-consensus-body',
        labelId: 'coin-consensus-symbol',
        rootId: 'coin-tree-root',
    });
}

/* ---------- Live 1-на-1 ---------- */
function _singleConfig() {
    let params = {};
    try { params = JSON.parse(document.getElementById('sl-params').value || '{}'); }
    catch { toast('Некорректный JSON параметров — применены без параметров'); }
    return {
        symbol: state.coin.symbol,
        timeframe: document.getElementById('sl-timeframe').value,
        strategy_type: document.getElementById('sl-strategy').value,
        params,
        tp_pct: parseFloat(document.getElementById('sl-tp').value),
        sl_pct: parseFloat(document.getElementById('sl-sl').value),
        max_hold_hours: parseFloat(document.getElementById('sl-hold').value),
        max_trades_per_day: parseInt(document.getElementById('sl-max-trades').value),
        position_size_pct: parseFloat(document.getElementById('sl-pos').value),
    };
}

async function singleStart() {
    try {
        const cfg = _singleConfig();
        const r = await API.post('/api/live/single/start', cfg);
        state.coin.single = r.state;
        state.coin.syncForm = true;   // форму можно перезаписать свежим конфигом
        renderSingleState();
        toast(`🟢 Live по ${cfg.symbol} запущен (${cfg.timeframe})`);
        log('system', `Live 1-на-1: ${cfg.symbol} ${cfg.timeframe} стратегия=${cfg.strategy_type}`);
    } catch (e) { toast('❌ ' + e.message); }
}

async function singleApply() {
    try {
        const cfg = _singleConfig();
        const r = await API.post('/api/live/single/config', cfg);
        state.coin.single = r.state;
        state.coin.syncForm = true;
        renderSingleState();
        toast(`✔ Настройки применены онлайн (${r.applied.join(', ')})`);
        log('system', `Live 1-на-1: обновлены настройки → ${r.applied.join(', ')}`);
    } catch (e) { toast('❌ ' + e.message); }
}

async function singleStop() {
    try {
        const r = await API.post('/api/live/single/stop', {});
        state.coin.single = r.state;
        renderSingleState();
        toast('■ Live по монете остановлен');
    } catch (e) { toast('❌ ' + e.message); }
}

async function refreshSingleState() {
    try {
        const s = await API.get('/api/live/single/state');
        state.coin.single = s;
        renderSingleState();
    } catch (e) {}
}

function renderSingleState() {
    const s = state.coin.single;
    if (!s) return;
    const pill = document.getElementById('single-run-pill');
    if (s.running) {
        pill.textContent = `🟢 ${s.config.symbol} · ${s.config.timeframe}`;
        pill.className = 'pill long';
    } else {
        pill.textContent = 'выкл';
        pill.className = 'pill neutral';
    }
    // перезаписываем поля формы конфигом сервера только после явного
    // «Старт»/«Применить» (иначе перетирали бы то, что пользователь вводит)
    if (s.config && state.coin.syncForm) {
        state.coin.syncForm = false;
        const c = s.config;
        _setVal('sl-tp', c.tp_pct); _setVal('sl-sl', c.sl_pct);
        _setVal('sl-hold', c.max_hold_hours); _setVal('sl-max-trades', c.max_trades_per_day);
        _setVal('sl-pos', c.position_size_pct);
        _setVal('sl-timeframe', c.timeframe);
        _setVal('sl-strategy', c.strategy_type);
        if (c.params && Object.keys(c.params).length) {
            document.getElementById('sl-params').value = JSON.stringify(c.params, null, 2);
        }
    }
    // последний осмотр
    const ls = document.getElementById('single-last');
    if (s.last_scan && s.last_scan.ok) {
        ls.innerHTML =
            `Последний осмотр: ${s.last_scan.strategy_name || s.last_scan.strategy} → ` +
            `<b class="${s.last_scan.side === 'LONG' ? 'up' : s.last_scan.side === 'SHORT' ? 'down' : ''}">${s.last_scan.side}</b>` +
            ` · conf ${fmt((s.last_scan.confidence || 0) * 100, 0)}% · ${fmtPrice(s.last_scan.price)}` +
            `<br>${escapeHtml(s.last_scan.reason || '')}` +
            `<br>Сделок сегодня: ${s.day_trades}/${s.day_limit} · win rate ${s.win_rate}%`;
    } else if (s.last_scan) {
        ls.textContent = 'Осмотр: ' + (s.last_scan.error || '…');
    }
    // сигналы этой сессии
    const box = document.getElementById('single-signals-list');
    const acts = s.active || [];
    const hist = (s.history || []).filter(x => x.status === 'CLOSED').slice(-8).reverse();
    if (!acts.length && !hist.length) {
        box.innerHTML = '<div class="empty">Сигналов пока нет</div>';
        return;
    }
    box.innerHTML =
        acts.map(sg => `
            <div class="signal-card open" style="padding:9px;margin-bottom:7px">
                <div class="s-head"><span class="s-symbol" style="font-size:13px">${sg.symbol}</span>
                    <span class="pill ${sg.side === 'LONG' ? 'long' : 'short'}">${sg.side}</span>
                    <span class="pill neutral" style="font-size:9px">LIVE</span>
                    <span class="muted">${fmt(sg.confidence * 100, 0)}%</span></div>
                <div class="s-levels" style="font-size:11px">
                    <span>Вход ${fmtPrice(sg.entry)}</span>
                    <span class="up">TP ${fmtPrice(sg.tp)}</span>
                    <span class="down">SL ${fmtPrice(sg.sl)}</span>
                </div>
            </div>`).join('') +
        hist.map(sg => `
            <div class="signal-card" style="padding:9px;margin-bottom:7px;opacity:.8">
                <div class="s-head"><span class="s-symbol" style="font-size:13px">${sg.symbol}</span>
                    <span class="pill ${sg.side === 'LONG' ? 'long' : 'short'}">${sg.side}</span>
                    <span class="muted">${sg.exit_reason}</span>
                    <span class="${sg.pnl_pct >= 0 ? 'up' : 'down'}">${sg.pnl_pct >= 0 ? '+' : ''}${fmt(sg.pnl_pct)}%</span></div>
            </div>`).join('');
}

function _setVal(id, v) {
    const el = document.getElementById(id);
    if (el && v !== undefined && v !== null) el.value = v;
}

/* =========================================================================
   WEBSOCKET события
   ========================================================================= */
function onWsEvent(data) {
    const src1v1 = data.signal && data.signal.source === 'single' || data.source === 'single';
    if (data.type === 'signal') {
        log('signal', `${src1v1 ? '1-на-1 · ' : ''}${data.signal.symbol} → ${data.signal.action} (${data.signal.side}, conf ${fmt(data.signal.confidence * 100, 0)}%)`, data.signal.side);
        if (!src1v1) renderLiveMini(data.signal);
        refreshLiveState();
        refreshSingleState();
    } else if (data.type === 'signal_close') {
        log('system', `${src1v1 ? '1-на-1 · ' : ''}${data.signal.symbol}: сигнал закрыт (${data.signal.exit_reason}, ${fmt(data.signal.pnl_pct)}%)`);
        refreshLiveState();
        refreshSingleState();
    } else if (data.type === 'system') {
        log(data.level || 'system', data.message);
    }
}

/* =========================================================================
   КОДОВЫЙ АССИСТЕНТ (агент, пишущий код)
   ========================================================================= */
function initAssistant() {
    const roleSel = document.getElementById('as-role');
    if (!roleSel) return;
    roleSel.innerHTML = [
        ['strategist', '🧠 gpt-6-astra (стратег)'],
        ['momentum', '⚡ gpt-5.6-sol (моментум)'],
        ['contrarian', '🦅 grok-4.5 (контрариан)'],
        ['macro', '💎 gemini-2.5-pro (макро)'],
        ['risk', '🛡️ claude-fable-5 (риск)'],
    ].map(([k, n]) => `<option value="${k}">${n}</option>`).join('');

    document.getElementById('assistant-form').addEventListener('submit', runAssistant);
    document.getElementById('as-save').addEventListener('click', saveAssistantCode);
}

async function runAssistant(e) {
    e.preventDefault();
    const btn = e.target.querySelector('button[type=submit]');
    const out = document.getElementById('as-output');
    btn.disabled = true; btn.textContent = '⏳ Генерация…';
    out.innerHTML = '<div class="empty"><span class="spinner"></span> Модель пишет код…</div>';
    try {
        const data = await API.post('/api/code/assist', {
            request: document.getElementById('as-request').value.trim(),
            task: document.getElementById('as-task').value,
            role: document.getElementById('as-role').value,
        });
        lastCode = data.code || '';
        document.getElementById('as-meta').textContent =
            `${data.model}${data.demo ? ' · DEMO' : ''} · ≈${(data.tokens_est?.prompt || 0) + (data.tokens_est?.completion || 0)} токенов`;
        // выводим код в pre
        const pre = document.createElement('pre');
        pre.style.cssText = 'white-space:pre-wrap;word-wrap:break-word;font-family:var(--mono);font-size:12px;line-height:1.5;color:var(--text);max-height:60vh;overflow:auto';
        pre.textContent = data.code;
        out.innerHTML = '';
        out.appendChild(pre);
        log('system', `Ассистент (${data.model}) сгенерировал код: ${data.code.length} символов`);
    } catch (err) {
        out.innerHTML = `<div class="empty" style="color:var(--red)">${err.message}</div>`;
    } finally {
        btn.disabled = false; btn.textContent = '⚡ Сгенерировать код';
    }
}

async function saveAssistantCode() {
    const fn = document.getElementById('as-filename').value.trim();
    if (!fn) { toast('Укажите путь файла'); return; }
    if (!lastCode) { toast('Сначала сгенерируйте код'); return; }
    try {
        const res = await API.post('/api/code/save', { filename: fn, content: lastCode });
        toast(`✅ Сохранено: ${res.path}`);
        log('system', `Код сохранён в ${res.path}`);
    } catch (e) {
        toast('❌ ' + e.message);
    }
}

/* =========================================================================
   Журнал
   ========================================================================= */
const MAX_LOG = 200;
function log(kind, message, side) {
    const box = document.getElementById('agents-log');
    if (!box) return;
    const entry = document.createElement('div');
    const cls = kind === 'signal' ? `signal ${side === 'SHORT' ? 'side-short' : ''}` : kind;
    entry.className = 'log-entry ' + cls;
    entry.innerHTML = `<span class="log-time">${new Date().toLocaleTimeString('ru-RU')}</span> <span class="log-agent">[${kind.toUpperCase()}]</span> ${escapeHtml(message)}`;
    box.insertBefore(entry, box.firstChild);
    while (box.children.length > MAX_LOG) box.lastChild.remove();
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

/* ---------- Мини-список сигналов ---------- */
const miniSignals = [];
function renderLiveMini(signal) {
    miniSignals.unshift(signal);
    if (miniSignals.length > 10) miniSignals.pop();
    document.getElementById('live-mini-list').innerHTML = miniSignals.map(sg => `
        <div class="signal-card" style="padding:9px;margin-bottom:7px">
            <div class="s-head"><span class="s-symbol" style="font-size:13px">${sg.symbol}</span>
            <span class="pill ${sg.side === 'LONG' ? 'long' : 'short'}">${sg.side}</span>
            <span class="muted">${fmt(sg.confidence * 100, 0)}%</span></div>
        </div>`).join('');
}

/* ---------- Футер: статусы агентов ---------- */
function renderFooterAgents() {
    const agents = [
        { icon: '⚡', name: 'Scanner' },
        { icon: '🧠', name: 'GPT-6 Astra' },
        { icon: '🦅', name: 'Grok 4.5' },
        { icon: '💎', name: 'Gemini 2.5 Pro' },
        { icon: '🛡️', name: 'Claude Fable 5' },
    ];
    document.getElementById('agents-footer').innerHTML = agents.map(a => `
        <div class="agent-pill" data-agent="${a.name}">
            <span>${a.icon}</span><span>${a.name}</span><span class="dot active"></span>
        </div>`).join('');
}

function setAgentState(which, st) {
    document.querySelectorAll('#agents-footer .agent-pill').forEach(el => {
        const dot = el.querySelector('.dot');
        if (which === 'all' || el.dataset.agent === which) dot.className = 'dot ' + st;
    });
}

/* ---------- Toast ---------- */
let lastCode = '';

let toastEl = null;
function toast(msg) {
    if (!toastEl) {
        toastEl = document.createElement('div');
        toastEl.style.cssText = 'position:fixed;bottom:24px;right:24px;z-index:999;background:var(--panel-solid);border:1px solid var(--border-2);color:var(--text);padding:12px 18px;border-radius:12px;font-size:13px;box-shadow:var(--shadow);transition:opacity .3s;';
        document.body.appendChild(toastEl);
    }
    toastEl.textContent = msg;
    toastEl.style.opacity = '1';
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { toastEl.style.opacity = '0'; }, 3500);
}

/* ---------- Старт ---------- */
document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('backtest-form').addEventListener('submit', runBacktest);
    document.getElementById('backtest-all-btn').addEventListener('click', runBacktestAll);
    document.getElementById('strategy-form').addEventListener('submit', saveStrategy);
    document.getElementById('refresh-tickers').addEventListener('click', refreshTickers);
    document.getElementById('optimize-btn').addEventListener('click', runOptimize);
    init();
});
