/* ==========================================================================
   TradeVision Portal — клиентская логика
   ========================================================================== */
'use strict';

/* ---------- API ---------- */
const API = {
    async get(p) {
        const r = await fetch(p);
        if (!r.ok) throw new Error(`${r.status} ${(await r.json().catch(() => ({}))).detail || r.statusText}`);
        return r.json();
    },
    async post(p, body, raw) {
        const r = await fetch(p, {
            method: 'POST',
            headers: raw ? { 'Content-Type': 'text/plain' } : { 'Content-Type': 'application/json' },
            body: raw ? body : JSON.stringify(body || {}),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.detail || `${r.status}`);
        return d;
    },
};

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmtPx = n => (n == null || isNaN(n)) ? '—' : Math.abs(n) >= 1000 ? n.toLocaleString('ru-RU', { maximumFractionDigits: 2 })
    : Math.abs(n) >= 1 ? n.toLocaleString('ru-RU', { maximumFractionDigits: 4 }) : Number(n).toPrecision(5);
const fmt = (n, d = 2) => (n == null || isNaN(n)) ? '—' : Number(n).toLocaleString('ru-RU', { minimumFractionDigits: d, maximumFractionDigits: d });
const fmtTime = ts => ts ? new Date(ts).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—';

const ROLE_ICONS = { strategist: '🧠', momentum: '⚡', contrarian: '🦅', macro: '💎', risk: '🛡️', portfolio: '💼' };
const ROLE_NAMES = { strategist: 'Ведущий стратег', momentum: 'Аналитик моментума', contrarian: 'Контрариан', macro: 'Макро-контекст', risk: 'Риск-менеджер', portfolio: 'Аналитик сделок' };

const state = {
    config: null,
    symbol: 'BTCUSDT',
    tf: '5m',
    prices: {},
    feed: [],
    models: [],
    councilES: null,
    reviewES: null,
};

/* ---------- Тосты + звук ---------- */
function toast(msg, cls = '', ms = 4200) {
    const el = document.createElement('div');
    el.className = 'toast ' + cls;
    el.innerHTML = msg;
    $('toasts').appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .4s'; setTimeout(() => el.remove(), 420); }, ms);
}

let audioCtx = null;
function blip(side) {
    if (!$('ntf-sound') || !$('ntf-sound').checked) return;
    try {
        audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
        const o = audioCtx.createOscillator();
        const g = audioCtx.createGain();
        o.connect(g); g.connect(audioCtx.destination);
        const f = side === 'LONG' ? [620, 880] : [520, 340];
        o.frequency.setValueAtTime(f[0], audioCtx.currentTime);
        o.frequency.exponentialRampToValueAtTime(f[1], audioCtx.currentTime + 0.16);
        g.gain.setValueAtTime(0.0001, audioCtx.currentTime);
        g.gain.exponentialRampToValueAtTime(0.12, audioCtx.currentTime + 0.03);
        g.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + 0.35);
        o.type = 'sine';
        o.start(); o.stop(audioCtx.currentTime + 0.4);
    } catch (e) { /* нет аудио-контекста */ }
}

/* ---------- Навигация ---------- */
document.querySelectorAll('#nav button').forEach(b => {
    b.addEventListener('click', () => {
        document.querySelectorAll('#nav button').forEach(x => x.classList.toggle('active', x === b));
        document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.id === 'page-' + b.dataset.page));
        if (b.dataset.page === 'portfolio') loadPortfolio();
        if (b.dataset.page === 'settings') loadSettings();
        if (b.dataset.page === 'terminal') setTimeout(() => chartApplySize(), 60);
    });
});

/* ---------- WebSocket ---------- */
function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    const conn = $('conn');
    ws.onopen = () => { conn.className = 'conn online'; $('conn-text').textContent = 'онлайн'; };
    ws.onclose = () => {
        conn.className = 'conn'; $('conn-text').textContent = 'офлайн — реконнект…';
        setTimeout(connectWS, 3000);
    };
    ws.onerror = () => ws.close();
    ws.onmessage = e => {
        let d; try { d = JSON.parse(e.data); } catch { return; }
        if (d.type === 'hello') {
            state.prices = d.prices || {};
            state.feed = d.feed || [];
            renderWatchlist(); renderFeed();
        } else if (d.type === 'prices') {
            state.prices = d.prices || state.prices;
            renderWatchlist();
        } else if (d.type === 'signal') {
            state.feed.push(d);
            state.feed = state.feed.slice(-60);
            renderFeed();
            renderWatchlist(d);
            if ($('ntf-toasts').checked) {
                toast(`<b>${d.symbol}</b> → <b style="color:var(--${d.side === 'LONG' ? 'long' : 'short'})">${d.side}</b><br>
                       <span style="color:var(--muted)">${esc(d.strategy)} · conf ${fmt(d.confidence * 100, 0)}% · ${fmtPx(d.price)}</span>`,
                    d.side === 'LONG' ? 'long' : 'short', 7000);
            }
            blip(d.side);
        }
    };
}

/* ---------- Watchlist ---------- */
function renderWatchlist(flash) {
    const box = $('watchlist');
    const wl = (state.config && state.config.live.watchlist) || [];
    $('watch-count').textContent = wl.length ? `· ${wl.length}` : '';
    if (!wl.length) { box.innerHTML = '<div class="empty">Список пуст — добавьте монеты в Настройках</div>'; return; }
    box.innerHTML = wl.map(sym => {
        const p = state.prices[sym];
        const chg = p ? p.change24h : 0;
        return `
        <div class="watch-card ${sym === state.symbol ? 'selected' : ''}" data-symbol="${sym}">
            ${flash && flash.symbol === sym ? `<span class="watch-flash ${flash.side.toLowerCase()}"></span>` : ''}
            <div>
                <div class="sym">${sym}</div>
                <div class="chg ${chg >= 0 ? 'up' : 'down'}">${chg >= 0 ? '+' : ''}${fmt(chg)}%</div>
            </div>
            <div class="px">${p ? fmtPx(p.last) : '—'}</div>
        </div>`;
    }).join('');
    box.querySelectorAll('.watch-card').forEach(el =>
        el.addEventListener('click', () => selectSymbol(el.dataset.symbol)));
}

function selectSymbol(sym) {
    state.symbol = sym;
    $('chart-symbol').firstChild.textContent = sym;
    document.querySelectorAll('.watch-card').forEach(x => x.classList.toggle('selected', x.dataset.symbol === sym));
    loadChart();
}

/* ---------- График (Lightweight Charts) ---------- */
let chart = null, candleSeries = null, volSeries = null;

function chartInit() {
    const el = $('tv-chart');
    chart = LightweightCharts.createChart(el, {
        layout: {
            background: { type: LightweightCharts.ColorType.Solid, color: 'transparent' },
            textColor: '#8a93b8',
            fontFamily: '"JetBrains Mono", monospace',
            fontSize: 11,
        },
        grid: {
            vertLines: { color: 'rgba(148,163,255,.06)' },
            horzLines: { color: 'rgba(148,163,255,.06)' },
        },
        crosshair: {
            vertLine: { color: 'rgba(139,92,246,.5)', labelBackgroundColor: '#8b5cf6' },
            horzLine: { color: 'rgba(139,92,246,.5)', labelBackgroundColor: '#8b5cf6' },
        },
        rightPriceScale: { borderColor: 'rgba(148,163,255,.15)' },
        timeScale: { borderColor: 'rgba(148,163,255,.15)', timeVisible: true, secondsVisible: false },
    });
    candleSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {
        upColor: '#00e0a4', downColor: '#ff5c7a',
        borderUpColor: '#00e0a4', borderDownColor: '#ff5c7a',
        wickUpColor: 'rgba(0,224,164,.7)', wickDownColor: 'rgba(255,92,122,.7)',
    });
    volSeries = chart.addSeries(LightweightCharts.HistogramSeries, {
        priceFormat: { type: 'volume' },
        priceScaleId: '',
        color: 'rgba(139,92,246,.35)',
    });
    chart.priceScale('').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    new ResizeObserver(chartApplySize).observe(el);
}

function chartApplySize() {
    const el = $('tv-chart');
    if (chart && el.clientWidth > 0) chart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
}

async function loadChart() {
    if (!chart) return;
    $('chart-source').textContent = 'загрузка…';
    try {
        const d = await API.get(`/api/klines/${state.symbol}?interval=${state.tf}&limit=300`);
        const rows = (d.candles || []).map(c => ({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close }));
        const vols = (d.candles || []).map(c => ({
            time: c.time, value: c.volume,
            color: c.close >= c.open ? 'rgba(0,224,164,.3)' : 'rgba(255,92,122,.3)',
        }));
        candleSeries.setData(rows);
        volSeries.setData(vols);
        chart.timeScale().fitContent();
        const live = d.data_source === 'live' || d.data_source === 'bybit';
        $('chart-source').textContent = d.empty ? 'нет данных' : (live ? '📡 Bybit' : '⚠️ учебные данные');
        $('chart-source').style.color = live ? 'var(--cyan)' : 'var(--amber)';
    } catch (e) {
        $('chart-source').textContent = 'ошибка: ' + e.message;
    }
}

/* ---------- Лента сигналов ---------- */
function renderFeed() {
    const feed = [...state.feed].reverse();
    const html = feed.map(s => signalCardHTML(s)).join('');
    $('feed').innerHTML = html || '<div class="empty" style="grid-column:1/-1">Сигналов пока нет — сканер работает…</div>';
    $('mini-feed').innerHTML = feed.slice(0, 20).map(s => `
        <div style="display:flex;gap:10px;align-items:center;padding:7px 2px;border-bottom:1px solid rgba(148,163,255,.06);font-size:12px;cursor:pointer" onclick="selectSymbol('${s.symbol}')">
            <span class="side-pill ${s.side.toLowerCase()}">${s.side}</span>
            <b style="font-family:var(--mono)">${s.symbol}</b>
            <span style="font-family:var(--mono)">${fmtPx(s.price)}</span>
            <span style="color:var(--muted);font-size:11px">${esc(s.strategy)}${s.strategy_auto ? ' <span style="color:var(--cyan)">⚡авто</span>' : ''}</span>
            <span style="margin-left:auto;color:var(--muted);font-size:11px;font-family:var(--mono)">${fmtTime(s.ts)}</span>
        </div>`).join('') || '<div class="empty">Ждём сигналы…</div>';
    document.querySelectorAll('#feed .signal-card').forEach(el =>
        el.addEventListener('click', () => {
            document.querySelector('#nav button[data-page="terminal"]').click();
            selectSymbol(el.dataset.symbol);
        }));
}

function signalCardHTML(s) {
    const confPct = Math.round((s.confidence || 0) * 100);
    return `
    <div class="signal-card ${s.side.toLowerCase()}" data-symbol="${s.symbol}">
        <div class="sc-head">
            <span class="sc-sym">${s.symbol}</span>
            <span class="side-pill ${s.side.toLowerCase()}">${s.side}</span>
            <span class="sc-time">${fmtTime(s.ts)}</span>
        </div>
        <div class="sc-row">
            <span><b>Вход</b>${fmtPx(s.price)}</span>
            <span><b>TP</b><span style="color:var(--long)">${fmtPx(s.tp)}</span></span>
            <span><b>SL</b><span style="color:var(--short)">${fmtPx(s.sl)}</span></span>
        </div>
        <div class="sc-strat">${esc(s.strategy)}${s.strategy_auto ? ' · <span class="auto">⚡ авто-подбор по графику</span>' : ''} · TF ${s.timeframe}</div>
        ${s.reason ? `<div class="sc-strat" style="opacity:.8">${esc(s.reason)}</div>` : ''}
        <div class="conf-bar"><i style="width:${confPct}%"></i></div>
    </div>`;
}

/* ---------- Совет агентов ---------- */
function openCouncil() { $('council-modal').classList.add('open'); }
function closeCouncil() {
    $('council-modal').classList.remove('open');
    if (state.councilES) { state.councilES.close(); state.councilES = null; }
}

function runCouncil() {
    openCouncil();
    const body = $('council-body');
    $('council-label').textContent = `${state.symbol} · ${state.tf}`;
    body.innerHTML = '<div class="thinking"><span class="pulse"></span>Готовим снимок рынка…</div>';
    if (state.councilES) state.councilES.close();

    const es = new EventSource(`/api/analyze/stream?symbol=${state.symbol}&timeframe=${state.tf}`);
    state.councilES = es;

    es.addEventListener('snapshot', e => {
        const d = JSON.parse(e.data);
        const auto = d.strategy_auto || {};
        const srcRu = { optimizer_profile: 'по сохранённым бэктестам', quick_backtest: 'быстрый бэктест по свежему графику', fallback: 'данных мало — универсальная' }[auto.source] || '';
        const cands = (auto.candidates || []).slice(0, 5).map((c, i) =>
            `<div>→ ${i + 1}) ${esc(c.name || c.type)} — сделок ${c.trades ?? 0}, winrate ${c.win_rate ?? '—'}%, PnL ${c.pnl_pct ?? '—'}%</div>`).join('');
        body.innerHTML = `
            <div class="agent-node" style="opacity:.8">
                <span class="a-icon">📊</span>
                <div style="flex:1">
                    <div class="a-name">Индикаторный контекст <span style="font-weight:500;color:var(--muted);font-size:11px">${esc(d.strategy.name)} · ⚡ авто-подбор (${srcRu})</span></div>
                    <div class="a-reason">Стратегия подобрана автоматически под свежий график. Агенты видят сравнение и опираются на собственный анализ данных — могут предложить и другую.</div>
                    ${cands ? `<div class="t-steps">${cands}</div>` : ''}
                </div>
            </div>
            <div class="thinking" id="council-thinking"><span class="pulse"></span>Агенты размышляют…</div>`;
    });

    es.addEventListener('agent', e => {
        const a = JSON.parse(e.data);
        const th = $('council-thinking'); if (th) th.remove();
        if (a.disabled) {
            body.insertAdjacentHTML('beforeend', `
                <div class="agent-node disabled">
                    <span class="a-icon">${a.icon}</span>
                    <div style="flex:1"><div class="a-name">${a.name}<span class="a-model">⏸ выключен — токены не тратятся</span></div></div>
                </div>`);
            return;
        }
        const steps = (a.thinking || []).map(s => `<div>→ ${esc(s)}</div>`).join('');
        body.insertAdjacentHTML('beforeend', `
            <div class="agent-node">
                <span class="a-icon">${a.icon}</span>
                <div style="flex:1">
                    <div class="a-name">${a.name}<span class="a-model">${a.demo ? 'DEMO' : esc(a.model)}${a.tokens_est && a.tokens_est.total ? ' · ≈' + a.tokens_est.total + ' ток.' : ''}</span></div>
                    <div class="a-verdict">
                        <span class="side-pill ${a.side === 'LONG' ? 'long' : a.side === 'SHORT' ? 'short' : ''}">${a.side}</span>
                        <span style="font-size:11px;color:var(--muted)">conf ${fmt((a.confidence || 0) * 100, 0)}%</span>
                    </div>
                    ${steps ? `<div class="t-steps">${steps}</div>` : ''}
                    ${a.reason ? `<div class="a-reason">${esc(a.reason)}</div>` : ''}
                </div>
            </div>`);
        body.scrollTop = body.scrollHeight;
    });

    es.addEventListener('consensus', e => {
        const th = $('council-thinking'); if (th) th.remove();
        const d = JSON.parse(e.data).consensus;
        const color = d.side === 'LONG' ? 'var(--long)' : d.side === 'SHORT' ? 'var(--short)' : 'var(--muted)';
        body.insertAdjacentHTML('beforeend', `
            <div class="consensus-block">
                <div style="font-size:24px">${d.side === 'LONG' ? '🚀' : d.side === 'SHORT' ? '🔻' : '⏸'}</div>
                <div>
                    <div style="font-weight:800">Консенсус: <span style="color:${color}">${d.side}</span> · уверенность ${fmt(d.confidence * 100, 0)}% · ${d.action === 'RIDE' ? '✅ сигнал' : '⏳ ждём'}</div>
                    <div style="font-size:11.5px;color:var(--muted);margin-top:3px">Взвешенный голос всех включённых агентов + индикаторной стратегии.</div>
                </div>
            </div>`);
        body.scrollTop = body.scrollHeight;
    });

    es.addEventListener('error', e => {
        try {
            const d = JSON.parse(e.data);
            body.insertAdjacentHTML('beforeend', `<div class="empty" style="color:var(--short)">⚠️ ${esc(d.message || 'ошибка стрима')}</div>`);
        } catch { /* ignore */ }
    });
    es.addEventListener('done', () => { es.close(); state.councilES = null; });
    es.onerror = () => {
        const th = $('council-thinking'); if (th) th.innerHTML = '⚠️ соединение прервано';
        es.close(); state.councilES = null;
    };
}

/* ---------- Мои сделки ---------- */
let pfChart = null, pfSeries = null, pfDays = 90;

document.querySelectorAll('#pf-period button').forEach(b =>
    b.addEventListener('click', () => {
        document.querySelectorAll('#pf-period button').forEach(x => x.classList.toggle('active', x === b));
        pfDays = parseInt(b.dataset.days);
        loadPortfolio();
    }));

async function loadPortfolio() {
    const empty = $('pf-empty'), content = $('pf-content');
    try {
        const d = await API.get(`/api/portfolio/overview?days=${pfDays}`);
        const st = d.stats;
        $('pf-source').textContent = d.source === 'bybit_api'
            ? '· данные: Bybit API (read-only)'
            : '· данные: загруженный CSV';
        if (st.empty) { empty.style.display = ''; content.style.display = 'none'; return; }
        empty.style.display = 'none';
        content.style.display = '';

        const cards = [
            ['Сделок', st.count, ''], ['PnL за период', (st.total_pnl >= 0 ? '+' : '') + fmt(st.total_pnl) + ' USDT', st.total_pnl >= 0 ? 'pos' : 'neg'],
            ['Winrate', fmt(st.winrate, 1) + '%', st.winrate >= 50 ? 'pos' : 'neg'],
            ['Profit Factor', fmt(st.profit_factor), st.profit_factor >= 1.2 ? 'pos' : ''],
            ['Средний профит', '+' + fmt(st.avg_win), 'pos'], ['Средний убыток', fmt(st.avg_loss), 'neg'],
            ['Сделок/день', fmt(st.per_day, 1), ''], ['Ср. удержание, ч', st.avg_hold_hours != null ? fmt(st.avg_hold_hours, 1) : '—', ''],
        ];
        $('pf-stats').innerHTML = cards.map(c =>
            `<div class="stat-card"><b>${c[0]}</b><div class="v ${c[2]}">${c[1]}</div></div>`).join('');

        $('pf-patterns').innerHTML = (st.patterns || []).map(p => `<li>${esc(p)}</li>`).join('') || '<li>Явных паттернов не найдено.</li>';

        $('pf-symbols').innerHTML = '<tr><th>Монета</th><th>Сделок</th><th>Winrate</th><th>PnL</th></tr>' +
            (st.by_symbol || []).slice(0, 12).map(r =>
                `<tr><td>${r.symbol}</td><td>${r.trades}</td><td>${fmt(r.winrate, 1)}%</td><td class="${r.pnl >= 0 ? 'pos' : 'neg'}">${fmt(r.pnl)}</td></tr>`).join('');

        $('pf-recent').innerHTML = '<tr><th>Монета</th><th>Сторона</th><th>Вход</th><th>Выход</th><th>PnL</th><th>Закрыта</th></tr>' +
            (d.recent || []).map(t =>
                `<tr><td>${t.symbol}</td><td>${t.side || '—'}</td><td>${fmtPx(t.entry)}</td><td>${fmtPx(t.exit)}</td>
                 <td class="${t.pnl >= 0 ? 'pos' : 'neg'}">${t.pnl >= 0 ? '+' : ''}${fmt(t.pnl)}</td><td>${t.closed || '—'}</td></tr>`).join('');

        // эквити
        const el = $('equity-chart');
        el.innerHTML = '';
        pfChart = LightweightCharts.createChart(el, {
            width: el.clientWidth, height: 230,
            layout: { background: { type: LightweightCharts.ColorType.Solid, color: 'transparent' }, textColor: '#8a93b8', fontSize: 10 },
            grid: { vertLines: { color: 'rgba(148,163,255,.05)' }, horzLines: { color: 'rgba(148,163,255,.05)' } },
            rightPriceScale: { borderColor: 'rgba(148,163,255,.12)' },
            timeScale: { borderColor: 'rgba(148,163,255,.12)' },
        });
        pfSeries = pfChart.addSeries(LightweightCharts.AreaSeries, {
            lineColor: '#8b5cf6', topColor: 'rgba(139,92,246,.35)', bottomColor: 'rgba(139,92,246,0)', lineWidth: 2,
        });
        pfSeries.setData(st.equity || []);
        pfChart.timeScale().fitContent();
    } catch (e) {
        empty.style.display = '';
        content.style.display = 'none';
        empty.innerHTML = `<div style="text-align:center">
            <div style="color:var(--amber);margin-bottom:10px">⚠️ ${esc(e.message)}</div>
            <div>Подсказка: ключи вводятся в Настройках → «Bybit read-only», либо загрузите CSV:</div>
            <div class="dropzone" id="csv-drop" style="margin-top:14px">📂 Перетащите CSV-экспорт Bybit сюда</div>
        </div>`;
        bindDropzone();
    }
}

function bindDropzone() {
    const dz = $('csv-drop');
    if (!dz || dz._bound) return;
    dz._bound = true;
    dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('over'); });
    dz.addEventListener('dragleave', () => dz.classList.remove('over'));
    dz.addEventListener('drop', e => {
        e.preventDefault(); dz.classList.remove('over');
        const f = e.dataTransfer.files[0];
        if (f) f.text().then(uploadCSV);
    });
    dz.addEventListener('click', () => {
        const inp = document.createElement('input');
        inp.type = 'file'; inp.accept = '.csv,.txt';
        inp.onchange = () => inp.files[0] && inp.files[0].text().then(uploadCSV);
        inp.click();
    });
}
bindDropzone();

async function uploadCSV(text) {
    try {
        const r = await API.post('/api/portfolio/csv', text, true);
        toast(`✅ CSV сохранён: распознано сделок: ${r.count}`);
        loadPortfolio();
    } catch (e) {
        toast('❌ ' + e.message);
    }
}

/* AI-разбор торговли */
function runAIReview() {
    const box = $('pf-review');
    box.innerHTML = '<div class="review-block"><div class="thinking"><span class="pulse"></span>💼 Аналитик разбирает вашу историю сделок…</div></div>';
    if (state.reviewES) state.reviewES.close();
    const es = new EventSource(`/api/portfolio/ai-review?days=${pfDays}`);
    state.reviewES = es;
    es.addEventListener('review', e => {
        const r = JSON.parse(e.data);
        const li = arr => (arr || []).map(x => `<li>${esc(x)}</li>`).join('');
        box.innerHTML = `
        <div class="review-block">
            <div style="display:flex;align-items:center;gap:14px">
                <span class="score-ring">${r.score ?? '—'}</span>
                <div>
                    <h4>💼 AI-разбор вашей торговли <span style="font-size:10px;color:var(--muted);font-family:var(--mono)">${esc(r.model || '')} · ≈${r.tokens_est || '?'} ток.</span></h4>
                    <div style="font-size:12.5px;line-height:1.55">${esc(r.summary || '')}</div>
                </div>
            </div>
            <div class="pf-cols" style="margin-top:14px">
                <div>
                    <h4>💪 Сильные стороны</h4><ul>${li(r.strengths)}</ul>
                    <h4 style="margin-top:10px">⚠️ Повторяющиеся ошибки</h4><ul>${li(r.mistakes)}</ul>
                </div>
                <div>
                    <h4>🎯 Рекомендации</h4><ul>${li(r.recommendations)}</ul>
                    <h4 style="margin-top:10px">🚨 Риск для депозита</h4><ul>${li(r.watch_out)}</ul>
                    ${r.focus_symbol ? `<div style="margin-top:8px;font-size:12px">Фокус: <b style="font-family:var(--mono)">${esc(r.focus_symbol)}</b></div>` : ''}
                </div>
            </div>
        </div>`;
    });
    es.addEventListener('error', e => {
        try { const d = JSON.parse(e.data); box.innerHTML = `<div class="empty" style="color:var(--short)">⚠️ ${esc(d.message)}</div>`; } catch { }
        es.close();
    });
    es.addEventListener('done', () => es.close());
}

/* ---------- Настройки ---------- */
async function loadSettings() {
    try {
        const cfg = await API.get('/api/config');
        state.config = cfg;
        renderSettings(cfg);
        renderWatchlist();
        renderWLEdit(cfg.live.watchlist);
        $('env-path').textContent = cfg.env_path || 'portal/.env';
    } catch (e) {
        toast('❌ Настройки: ' + e.message);
    }
}

function renderSettings(cfg) {
    $('set-ai-url').value = cfg.ai_base_url;
    $('set-ai-key').value = cfg.global_api_key;
    $('set-bb-key').value = cfg.bybit.api_key;
    $('set-scan-interval').value = cfg.live.scan_interval_seconds;
    $('set-signal-tf').value = cfg.live.timeframe;
    $('set-min-conf').value = cfg.live.min_confidence;
    $('set-max-trades').value = cfg.rules.max_trades_per_day;
    $('set-min-tp').value = cfg.rules.min_tp_pct;
    $('set-max-hold').value = cfg.rules.max_hold_hours;
    $('set-sl').value = cfg.rules.default_sl_pct;
    $('ntf-sound').checked = cfg.live.notify_sound;
    $('ntf-toasts').checked = cfg.live.notify_toasts;

    const sel = $('set-signal-strategy');
    if (![...sel.options].some(o => o.value === cfg.live.signal_strategy)) {
        const o = document.createElement('option');
        o.value = cfg.live.signal_strategy; o.textContent = cfg.live.signal_strategy;
        sel.appendChild(o);
    }
    sel.value = cfg.live.signal_strategy || 'auto';

    $('roles-settings').innerHTML = cfg.roles.map(r => {
        const icon = ROLE_ICONS[r.key] || '🤖';
        const name = (cfg.role_meta && cfg.role_meta[r.key] && cfg.role_meta[r.key].name) || ROLE_NAMES[r.key] || r.key;
        return `
        <div class="role-row" data-role="${r.key}">
            <div>
                <div class="rr-name">${icon} ${esc(name)}</div>
                <div class="rr-sub role-status">—</div>
            </div>
            <select class="rr-model" data-current="${esc(r.model)}">
                <option value="${esc(r.model)}">${esc(r.model)}</option>
            </select>
            <div class="rr-foot">
                <label class="chk"><input type="checkbox" class="rr-enabled" ${r.enabled ? 'checked' : ''}> включён</label>
                <input type="password" class="rr-key" placeholder="свой ключ (пусто = глобальный)" style="flex:1;background:rgba(5,7,18,.6);border:1px solid var(--stroke);border-radius:9px;color:var(--text);padding:6px 9px;font-size:11px;font-family:var(--mono)">
            </div>
        </div>`;
    }).join('');
}

async function loadModelsList() {
    const btn = $('load-models-btn');
    btn.disabled = true; btn.textContent = '⏳ Загрузка…';
    await saveAllQuiet();
    try {
        const d = await API.get('/api/models');
        state.models = d.models;
        document.querySelectorAll('.rr-model').forEach(sel => {
            const cur = sel.dataset.current || sel.value;
            sel.innerHTML = d.models.map(m => `<option value="${esc(m)}" ${m === cur ? 'selected' : ''}>${esc(m)}</option>`).join('');
            if (!d.models.includes(cur)) {
                const o = document.createElement('option'); o.value = cur; o.textContent = cur + ' (не в списке)';
                o.selected = true; sel.appendChild(o);
            }
        });
        toast(`✅ Моделей доступно: ${d.models.length}`);
    } catch (e) {
        toast('❌ ' + e.message);
    } finally {
        btn.disabled = false; btn.textContent = '⬇ Загрузить список моделей';
    }
}

function collectUpdates() {
    const u = {};
    const v = id => $(id) ? $(id).value.trim() : '';
    if (v('set-ai-url')) u.ai_base_url = v('set-ai-url');
    const ak = v('set-ai-key'); if (ak && !ak.includes('…')) u.ai_api_key = ak;
    const bk = v('set-bb-key'); if (bk && !bk.includes('…')) u.bybit_api_key = bk;
    const bs = v('set-bb-secret'); if (bs) u.bybit_api_secret = bs;
    u.scan_interval_seconds = parseInt(v('set-scan-interval')) || 30;
    u.default_timeframe = v('set-signal-tf') || '5m';
    u.min_confidence = parseFloat(v('set-min-conf')) || 0.55;
    u.signal_strategy = v('set-signal-strategy') || 'auto';
    u.max_trades_per_day = parseInt(v('set-max-trades')) || 30;
    u.min_tp_pct = parseFloat(v('set-min-tp')) || 2;
    u.max_hold_hours = parseFloat(v('set-max-hold')) || 4;
    u.default_sl_pct = parseFloat(v('set-sl')) || 1;
    u.notify_sound = $('ntf-sound').checked ? 'true' : 'false';
    u.notify_toasts = $('ntf-toasts').checked ? 'true' : 'false';
    document.querySelectorAll('.role-row').forEach(row => {
        const role = row.dataset.role;
        u['model_' + role] = row.querySelector('.rr-model').value;
        u['en_' + role] = row.querySelector('.rr-enabled').checked ? 'true' : 'false';
        const k = row.querySelector('.rr-key').value.trim();
        if (k && !k.includes('…')) u['key_' + role.toLowerCase()] = k;
    });
    u.watchlist = _wl.join(',');
    return u;
}

let _wl = [];
function renderWLEdit(wl) {
    _wl = [...wl];
    $('wl-chips').innerHTML = _wl.map(s =>
        `<span class="chip">${s}<button data-sym="${s}">✕</button></span>`).join('') || '<span class="hint">список пуст</span>';
    $('wl-chips').querySelectorAll('button').forEach(b =>
        b.addEventListener('click', () => { _wl = _wl.filter(x => x !== b.dataset.sym); renderWLEdit(_wl); saveAllQuiet(); }));
}

function addToWatchlist(sym) {
    sym = (sym || '').trim().toUpperCase();
    if (!sym) return;
    if (!_wl.includes(sym)) _wl.push(sym);
    renderWLEdit(_wl);
    saveAllQuiet().then(() => toast(`✅ ${sym} добавлен в watchlist`));
}

let _saveLock = false;
async function saveAllQuiet() {
    if (_saveLock) return;
    _saveLock = true;
    try {
        const res = await API.post('/api/config', { updates: collectUpdates() });
        state.config = res.config;
    } catch (e) { console.warn('save', e); }
    finally { _saveLock = false; }
}

async function saveWithToast() {
    try {
        const res = await API.post('/api/config', { updates: collectUpdates() });
        state.config = res.config;
        toast(`✅ Сохранено (${res.applied.length} параметров) → portal/.env`);
        renderWatchlist();
    } catch (e) {
        toast('❌ ' + e.message);
    }
}

async function testModels() {
    const btn = $('test-models-btn');
    btn.disabled = true; btn.textContent = '⏳ Проверка…';
    await saveAllQuiet();
    try {
        const d = await API.get('/api/config/test?role=all');
        document.querySelectorAll('.role-row').forEach(row => {
            const st = row.querySelector('.role-status');
            const r = d.results[row.dataset.role];
            if (r.ok) { st.textContent = `✓ ${r.model} — ${r.detail}`; st.className = 'rr-sub role-status ok'; }
            else { st.textContent = `✗ ${r.model} — ${r.detail}`; st.className = 'rr-sub role-status bad'; }
        });
    } catch (e) { toast('❌ ' + e.message); }
    finally { btn.disabled = false; btn.textContent = '🔌 Проверить модели'; }
}

async function pingBybit() {
    const el = $('bybit-conn');
    el.textContent = 'проверка…';
    try {
        const s = await API.post('/api/market/ping', {});
        if (s.ok) {
            el.innerHTML = `<span style="color:var(--long)">✓ Bybit отвечает</span> (${(s.host || '').replace('https://', '')}, ${s.latency_ms} мс)`;
        } else {
            el.innerHTML = `<span style="color:var(--short)">✗ недоступен</span> — попробуйте VPN или другой хост` +
                (s.results ? '<br>' + s.results.map(r => `<span style="color:${r.ok ? 'var(--long)' : 'var(--short)'}">${r.ok ? '✓' : '✗'}</span> ${(r.host || '').replace('https://', '')}`).join('<br>') : '');
        }
        refreshDataBadge();
    } catch (e) { el.textContent = 'ошибка: ' + e.message; }
}

async function refreshDataBadge() {
    try {
        const s = await API.get('/api/market/status');
        const b = $('data-badge');
        if (s.source === 'live' || s.bybit_ok) {
            b.textContent = '📡 Bybit Live'; b.className = 'badge-src ok';
        } else {
            b.textContent = '⚠️ Учебные данные'; b.className = 'badge-src warn';
        }
    } catch (e) { }
}

/* ---------- Биндинги ---------- */
const TFS = ['1m', '3m', '5m', '15m', '30m', '1h', '4h'];
TFS.forEach(tf => {
    const b = document.createElement('button');
    b.textContent = tf;
    b.className = tf === state.tf ? 'active' : '';
    b.addEventListener('click', () => {
        state.tf = tf;
        $('tf-group').querySelectorAll('button').forEach(x => x.classList.toggle('active', x === b));
        loadChart();
    });
    $('tf-group').appendChild(b);
});

$('council-btn').addEventListener('click', runCouncil);
$('council-close').addEventListener('click', closeCouncil);
$('council-modal').addEventListener('click', e => { if (e.target === $('council-modal')) closeCouncil(); });
$('pf-reload').addEventListener('click', loadPortfolio);
$('pf-ai-btn').addEventListener('click', runAIReview);
$('test-models-btn').addEventListener('click', testModels);
$('load-models-btn').addEventListener('click', loadModelsList);
$('ping-bybit-btn').addEventListener('click', pingBybit);
$('save-ai-btn').addEventListener('click', saveWithToast);
$('save-bybit-btn').addEventListener('click', saveWithToast);
$('save-live-btn').addEventListener('click', saveWithToast);
$('wl-add').addEventListener('keydown', e => { if (e.key === 'Enter') { addToWatchlist($('wl-add').value); $('wl-add').value = ''; } });
$('add-symbol-btn').addEventListener('click', () => {
    const s = prompt('Монета с Bybit (напр. LSKUSDT):');
    if (s) addToWatchlist(s);
});
$('ntf-sound').addEventListener('change', saveAllQuiet);
$('ntf-toasts').addEventListener('change', saveAllQuiet);

/* ---------- Старт ---------- */
(async () => {
    chartInit();
    connectWS();
    await loadSettings();
    refreshDataBadge();
    loadChart();
    setInterval(loadChart, 60000);
    setInterval(refreshDataBadge, 60000);
    // возможности движка звука требуют жеста пользователя
    document.addEventListener('click', () => { if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume(); }, { once: true });
})();
