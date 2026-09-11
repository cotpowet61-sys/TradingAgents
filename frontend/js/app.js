/* ============ TradingAgents — фронтенд-логика ============ */

const state = {
    symbols: [],
    strategies: [],
    currentSymbol: 'BTCUSDT',
    currentTF: '5m',
    rules: {},
    live: {},
    tvWidget: null,
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
        });
    });
}

/* ---------- Инициализация (загрузка конфига) ---------- */
async function init() {
    initNav();
    try {
        const health = await API.get('/api/health');
        state.rules = health.rules || {};
        document.getElementById('demo-badge').textContent = health.demo_mode ? 'DEMO' : 'LIVE';
        document.getElementById('demo-badge').style.display = health.demo_mode ? '' : 'none';
    } catch (e) { log('system', 'Ошибка подключения к backend: ' + e.message); }

    await Promise.all([loadSymbols(), loadStrategies(), loadSettings()]);
    renderRules();
    renderFooterAgents();
    initChart();
    initTickers();
    initLive();

    // WebSocket
    connectWS(onWsEvent, (s) => { state.live = s || {}; renderLive(); });
    setInterval(refreshTickers, 30000);
    setInterval(refreshLiveState, 10000);
}

/* ---------- Символы ---------- */
async function loadSymbols() {
    try {
        const data = await API.get('/api/symbols');
        state.symbols = data.symbols || [];
        const sel = document.getElementById('chart-symbol');
        sel.innerHTML = state.symbols.map(s => `<option value="${s}">${s}</option>`).join('');
        sel.value = state.currentSymbol;
        sel.addEventListener('change', () => { state.currentSymbol = sel.value; switchSymbol(); });
        // заполнить select бэктеста
        document.getElementById('bt-symbol').innerHTML = state.symbols.map(s => `<option value="${s}">${s}</option>`).join('');
    } catch (e) { log('system', 'Ошибка загрузки символов: ' + e.message); }
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

/* ---------- Настройки ---------- */
async function loadSettings() {
    const box = document.getElementById('settings-body');
    const health = await API.get('/api/health').catch(() => ({}));
    const strategies = await API.get('/api/strategies').catch(() => ({ strategies: [] }));
    const rules = health.rules || {};
    box.innerHTML = `
        <div class="rules-grid">
            ${Object.entries(rules).map(([k, v]) => `
                <div class="metric"><div class="m-label">${k}</div><div class="m-value" style="font-size:16px">${v}</div></div>
            `).join('')}
        </div>
        <h3 style="margin:16px 0 8px">Подключённые модели (5 ролей)</h3>
        <div class="agents-row">
            ${[
                { icon: '🧠', name: 'Ведущий стратег', model: 'STRATEGIST_MODEL' },
                { icon: '⚡', name: 'Аналитик моментума', model: 'MOMENTUM_MODEL' },
                { icon: '🦅', name: 'Контрариан', model: 'CONTRARIAN_MODEL' },
                { icon: '💎', name: 'Макро', model: 'MACRO_MODEL' },
                { icon: '🛡️', name: 'Риск-менеджер', model: 'RISK_MODEL' },
            ].map(a => `
                <div class="agent-card">
                    <div class="a-head"><span>${a.icon}</span><span class="a-name">${a.name}</span></div>
                    <div class="a-model">env: ${a.model}</div>
                </div>
            `).join('')}
        </div>
        <p class="muted" style="margin-top:16px">
            Модели настраиваются в файле <b>.env</b> (см. .env.example). Без ключей система
            работает в DEMO-режиме на детерминированных ответах. Провайдеры: OpenRouter,
            OpenAI, Anthropic, xAI, Google Gemini (OpenAI-совместимый API).
        </p>
    `;
}

/* ---------- Dashboard: тикеры ---------- */
async function refreshTickers() {
    try {
        const data = await API.get('/api/tickers');
        renderTickers(data.tickers || []);
    } catch (e) { /* тихо */ }
}

function renderTickers(tickers) {
    const box = document.getElementById('coins-list');
    box.innerHTML = tickers.map(t => {
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

/* ---------- Chart (TradingView + fallback) ---------- */
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

    if (window.TradingView) {
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
                toolbar_bg: '#121826',
                enable_publishing: false,
                hide_side_toolbar: false,
                allow_symbol_change: false,
            });
        } catch (e) { enableFallbackChart(); }
    } else {
        enableFallbackChart();
    }
    // Если через 4 сек TradingView не отрисовался — включаем fallback
    setTimeout(() => {
        const tvEl = document.getElementById('tradingview-chart');
        if (!tvEl || !tvEl.querySelector('iframe')) enableFallbackChart();
    }, 4000);

    document.getElementById('analyze-btn').addEventListener('click', runAnalysis);
}

function enableFallbackChart() {
    document.getElementById('tradingview-chart').style.display = 'none';
    document.getElementById('chart-fallback').style.display = '';
    loadFallbackCandles();
}

async function loadFallbackCandles() {
    try {
        const data = await API.get(`/api/klines/${state.currentSymbol}?interval=${state.currentTF}&limit=120`);
        drawCandles(document.getElementById('candles-canvas'), data.candles || []);
    } catch (e) { /* тихо */ }
}

function switchSymbol() {
    if (state.tvWidget && window.TradingView) {
        try {
            state.tvWidget.setSymbol(`BYBIT:${state.currentSymbol}`, state.currentTF);
        } catch (e) {}
    }
    if (document.getElementById('chart-fallback').style.display !== 'none') loadFallbackCandles();
}

function resizeChart() {
    if (document.getElementById('chart-fallback').style.display !== 'none') loadFallbackCandles();
}

/* ---------- Мультиагентный анализ ---------- */
async function runAnalysis() {
    const btn = document.getElementById('analyze-btn');
    btn.disabled = true;
    btn.textContent = '⏳ Анализ…';
    setAgentState('all', 'thinking');
    try {
        const data = await API.post('/api/analyze', {
            symbol: state.currentSymbol,
            strategy: document.getElementById('bt-strategy').value,
            timeframe: state.currentTF,
        });
        renderConsensus(data);
        log('system', `Анализ ${state.currentSymbol} завершён (консенсус: ${data.consensus.side})`);
    } catch (e) {
        log('error', 'Ошибка анализа: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = '🤖 Анализ агентов';
        setAgentState('all', 'active');
    }
}

function renderConsensus(data) {
    document.getElementById('consensus-symbol').textContent =
        `${data.symbol} · ${data.timeframe} · цена ${fmtPrice(data.price)} · стратегия: ${data.strategy.name}`;

    const cons = data.consensus;
    const sig = data.signal;
    const confPct = Math.round(cons.confidence * 100);

    const html = `
        <div class="consensus-verdict">
            <div class="verdict-badge ${cons.side}">${cons.side}</div>
            <div>
                <div class="muted">Уверенность консенсуса</div>
                <div style="font-size:22px;font-weight:700;font-family:var(--mono)">${confPct}%</div>
                <div class="conf-bar" style="width:220px"><div style="width:${confPct}%"></div></div>
            </div>
            <div>
                <div class="muted">Действие</div>
                <div class="pill ${sig.action === 'BUY' ? 'long' : sig.action === 'SELL' ? 'short' : ''}" style="font-size:14px">${sig.action || 'WAIT'}</div>
            </div>
            <div style="flex:1" class="muted">${sig.reason || ''}</div>
        </div>
        <div class="agents-row">
            ${data.agents.map(a => `
                <div class="agent-card">
                    <div class="a-head"><span>${a.icon}</span><span class="a-name">${a.name}</span></div>
                    <div class="a-model">${a.demo ? '[DEMO] ' : ''}${a.model}</div>
                    <div class="a-verdict">
                        <span class="pill ${a.side === 'LONG' ? 'long' : a.side === 'SHORT' ? 'short' : ''}">${a.side}</span>
                        conf ${fmt(a.confidence * 100, 0)}%
                    </div>
                    <div class="a-reason">${(a.reason || '').slice(0, 140)}</div>
                </div>
            `).join('')}
        </div>`;
    document.getElementById('consensus-body').innerHTML = html;
}

/* ---------- Backtest ---------- */
let lastBacktest = null;

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('backtest-form').addEventListener('submit', runBacktest);
    document.getElementById('strategy-form').addEventListener('submit', saveStrategy);
});

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
            limit: parseInt(document.getElementById('bt-limit').value) || 500,
            params,
            tp_pct: parseFloat(document.getElementById('bt-tp').value),
            sl_pct: parseFloat(document.getElementById('bt-sl').value),
            max_hold_hours: parseFloat(document.getElementById('bt-hold').value),
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

function renderBacktest(d) {
    const res = document.getElementById('backtest-results');
    res.innerHTML = `
        <div class="muted" style="margin-bottom:10px">${d.symbol} · ${d.timeframe} · ${d.strategy} · ${d.params ? Object.entries(d.params).map(([k,v])=>k+'='+v).join(', ') : ''}</div>
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
        </div>`;
    drawEquity(document.getElementById('equity-canvas'), d.equity_curve || []);
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

function redrawEquity() {
    if (lastBacktest) drawEquity(document.getElementById('equity-canvas'), lastBacktest.equity_curve || []);
}

function saveStrategy(e) {
    e.preventDefault();
    // Переносим параметры из редактора в форму бэктеста и переключаемся
    document.getElementById('bt-strategy').value = document.getElementById('st-type').value;
    document.getElementById('bt-tp').value = document.getElementById('st-tp').value;
    document.getElementById('bt-sl').value = document.getElementById('st-sl').value;
    document.getElementById('bt-hold').value = document.getElementById('st-hold').value;
    document.getElementById('bt-params').value = document.getElementById('st-params').value;
    document.querySelector('.nav-btn[data-tab="backtest"]').click();
    log('system', `Стратегия "${currentStrategyDef().name}" готова к бэктесту`);
}

/* ---------- Live ---------- */
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
            <td>${sg.status === 'CLOSED' ? `<span class="pill ${sg.exit_reason.toLowerCase()}">${sg.exit_reason}</span>` : '<span class="pill" style="background:rgba(79,140,255,.2);color:#7aa8ff">OPEN</span>'}</td>
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
    } catch (e) { /* тихо */ }
}

function initLive() { refreshLiveState(); }

/* ---------- WebSocket события ---------- */
function onWsEvent(data) {
    if (data.type === 'signal') {
        log('signal', `${data.signal.symbol} → ${data.signal.action} (${data.signal.side}, conf ${fmt(data.signal.confidence * 100, 0)}%)`, data.signal.side);
        renderLiveMini(data.signal);
        refreshLiveState();
    } else if (data.type === 'signal_close') {
        log('system', `${data.signal.symbol}: сигнал закрыт (${data.signal.exit_reason}, ${fmt(data.signal.pnl_pct)}%)`);
        refreshLiveState();
    } else if (data.type === 'system') {
        log(data.level || 'system', data.message);
    }
}

/* ---------- Журнал ---------- */
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

/* ---------- Мини-список сигналов на дашборде ---------- */
const miniSignals = [];
function renderLiveMini(signal) {
    miniSignals.unshift(signal);
    if (miniSignals.length > 10) miniSignals.pop();
    document.getElementById('live-mini-list').innerHTML = miniSignals.map(sg => `
        <div class="signal-card" style="padding:8px;margin-bottom:6px">
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

function setAgentState(which, state) {
    document.querySelectorAll('#agents-footer .agent-pill').forEach(el => {
        const dot = el.querySelector('.dot');
        if (which === 'all' || el.dataset.agent === which) {
            dot.className = 'dot ' + state;
        }
    });
}

/* ---------- Старт ---------- */
document.addEventListener('DOMContentLoaded', init);
