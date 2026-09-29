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
        } else if (d.type === 'signal_close') {
            // исход живого сигнала: TP / SL / таймаут / разворот
            state.feed.push(d);
            state.feed = state.feed.slice(-60);
            renderFeed();
            loadLearned();
            if ($('ntf-toasts').checked && d.reason !== 'REFRESH') {
                const icon = { TP: '✅', SL: '❌', TIMEOUT: '⏱', REVERSED: '🔁' }[d.reason] || 'ℹ️';
                const pnl = d.pnl_pct == null ? '' : ` · <b style="color:var(--${d.pnl_pct >= 0 ? 'long' : 'short'})">${d.pnl_pct >= 0 ? '+' : ''}${d.pnl_pct}%</b>`;
                toast(`${icon} <b>${d.symbol}</b> ${d.side} ${d.timeframe} закрыт: ${d.reason}${pnl}
                       <br><span style="color:var(--muted)">за ${d.duration_min} мин · ${esc(d.strategy || '')}</span>`,
                    d.pnl_pct >= 0 ? 'long' : 'short', 8000);
            }
        } else if (d.type === 'signal') {
            state.feed.push(d);
            state.feed = state.feed.slice(-60);
            renderFeed();
            renderWatchlist(d);
            if (window.neuroFlash) window.neuroFlash(d.side === 'LONG' ? 'long' : 'short');
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
            <button class="rm" title="Убрать из watchlist" data-rm="${sym}">✕</button>
        </div>`;
    }).join('');
    box.querySelectorAll('.watch-card').forEach(el =>
        el.addEventListener('click', () => selectSymbol(el.dataset.symbol)));
    // кнопки «✕» — удалить монету из watchlist (сразу сохраняется)
    box.querySelectorAll('button.rm').forEach(btn =>
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const sym = btn.dataset.rm;
            try {
                const wl = ((state.config && state.config.live.watchlist) || []).filter(x => x !== sym);
                const res = await API.post('/api/config', { updates: { watchlist: wl.join(',') } });
                state.config = res.config;
                _wl = [...res.config.live.watchlist];
                renderWLEdit(_wl);
                renderWatchlist();
                toast(`🗑 ${sym} убран из watchlist`);
            } catch (err) {
                toast('❌ ' + err.message);
            }
        }));
}

function selectSymbol(sym) {
    state.symbol = sym;
    $('chart-symbol').firstChild.textContent = sym;
    document.querySelectorAll('.watch-card').forEach(x => x.classList.toggle('selected', x.dataset.symbol === sym));
    loadChart();
}

/* ---------- График (Lightweight Charts) ---------- */
let chart = null, candleSeries = null, volSeries = null, mainMarkers = null;
const TF_SEC = { '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800, '1h': 3600, '4h': 14400 };

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

function applyDealMarkers() {
    if (!candleSeries) return;
    const deals = (dnaState.dna && dnaState.dna.deals) || [];
    const tf = TF_SEC[state.tf] || 300;
    const marks = [];
    deals.filter(d => d.symbol === state.symbol).forEach(d => {
        const isLong = d.side === 'long';
        const tEnter = Math.floor((d.open_ts / 1000) / tf) * tf;
        const tExit = Math.floor((d.close_ts / 1000) / tf) * tf;
        marks.push({ time: tEnter, position: isLong ? 'belowBar' : 'aboveBar', color: d.pnl >= 0 ? '#00e0a4' : '#ff5c7a', shape: isLong ? 'arrowUp' : 'arrowDown', text: `мой вход (${d.r_multiple > 0 ? '+' : ''}${d.r_multiple}R)` });
        marks.push({ time: Math.max(tExit, tEnter), position: isLong ? 'aboveBar' : 'belowBar', color: 'rgba(139,92,246,.9)', shape: 'circle', text: `выход ${d.pnl >= 0 ? '+' : ''}${fmt(d.pnl, 2)}` });
    });
    marks.sort((a, b) => a.time - b.time);
    if (!mainMarkers) mainMarkers = LightweightCharts.createSeriesMarkers(candleSeries, []);
    mainMarkers.setMarkers(marks);
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
        // маркеры моих сделок по этой монете (из ДНК)
        applyDealMarkers();
        chart.timeScale().fitContent();
        const live = d.data_source === 'live' || d.data_source === 'bybit';
        $('chart-source').textContent = d.empty ? 'нет данных' : (live ? '📡 Bybit' : '⚠️ учебные данные');
        $('chart-source').style.color = live ? 'var(--cyan)' : 'var(--amber)';
    } catch (e) {
        $('chart-source').textContent = 'ошибка: ' + e.message;
    }
}

/* ---------- Лента сигналов ---------- */
function fitChip(s, compact) {
    const f = s.fit;
    if (!f || f.score == null) return '';
    const strong = f.score >= 60, weak = f.score < 42;
    const color = strong ? 'var(--long)' : weak ? 'var(--short)' : 'var(--amber)';
    const title = esc([...(f.warnings || []), ...(f.notes || [])].join('\n'));
    return `<span title="${title}" style="font-size:10px;font-weight:800;padding:3px 8px;border-radius:99px;border:1px solid ${color};color:${color}">🎯 ${f.score}</span>`;
}

function renderFeed() {
    const strongOnly = $('flt-strong') && $('flt-strong').checked;
    const feed = [...state.feed].reverse().filter(s =>
        !strongOnly || s.type === 'signal_close' || (s.fit && s.fit.score != null && s.fit.score >= 60));
    const html = feed.map(s => s.type === 'signal_close' ? closeCardHTML(s) : signalCardHTML(s)).join('');
    $('feed').innerHTML = html || '<div class="empty" style="grid-column:1/-1">Сигналов пока нет — сканер работает…</div>';
    const _closeLbl = { TP: '✅ TP', SL: '❌ SL', TIMEOUT: '⏱ тмаут', REVERSED: '🔁 разворот', REFRESH: '🔄 обновлён' };
    $('mini-feed').innerHTML = feed.slice(0, 20).map(s => {
        const isClose = s.type === 'signal_close';
        const mid = isClose
            ? `<span style="font-family:var(--mono)">${fmtPx(s.exit ?? s.entry)}</span>
               <span style="font-size:11px;color:var(--${(s.pnl_pct || 0) >= 0 ? 'long' : 'short'})">${_closeLbl[s.reason] || s.reason}${s.pnl_pct != null ? ' ' + (s.pnl_pct >= 0 ? '+' : '') + s.pnl_pct + '%' : ''}</span>`
            : `${fitChip(s)}
               <span style="font-family:var(--mono)">${fmtPx(s.price)}</span>
               <span style="color:var(--muted);font-size:11px">${esc(s.strategy)}${s.strategy_auto ? ' <span style="color:var(--cyan)">⚡авто</span>' : ''}</span>`;
        return `
        <div style="display:flex;gap:10px;align-items:center;padding:7px 2px;border-bottom:1px solid rgba(148,163,255,.06);font-size:12px;cursor:pointer${isClose ? ';opacity:.8' : ''}" onclick="selectSymbol('${s.symbol}')">
            <span class="side-pill ${s.side.toLowerCase()}">${s.side}</span>
            <b style="font-family:var(--mono)">${s.symbol}</b>
            ${mid}
            <span style="margin-left:auto;color:var(--muted);font-size:11px;font-family:var(--mono)">${fmtTime(s.ts)}</span>
        </div>`;
    }).join('') || '<div class="empty">Ждём сигналы…</div>';
    document.querySelectorAll('#feed .signal-card').forEach(el =>
        el.addEventListener('click', () => {
            document.querySelector('#nav button[data-page="terminal"]').click();
            selectSymbol(el.dataset.symbol);
        }));
}

function closeCardHTML(s) {
    const icon = { TP: '✅', SL: '❌', TIMEOUT: '⏱', REVERSED: '🔁', REFRESH: '🔄' }[s.reason] || 'ℹ️';
    const label = { TP: 'закрыт по Take-Profit', SL: 'закрыт по Stop-Loss',
                    TIMEOUT: 'таймаут удержания', REVERSED: 'развёрнут встречным сигналом',
                    REFRESH: 'обновлён (перевыпуск)' }[s.reason] || s.reason;
    const pnlCls = (s.pnl_pct || 0) >= 0 ? 'long' : 'short';
    return `
    <div class="signal-card ${(s.side || '').toLowerCase()}" data-symbol="${s.symbol}" style="opacity:.82">
        <div class="sc-head">
            <span class="sc-sym">${s.symbol} <span style="color:var(--muted);font-size:11px">${s.timeframe || ''}</span></span>
            <span class="side-pill ${(s.side || '').toLowerCase()}">${s.side}</span>
            <span class="sc-time">${fmtTime(s.ts)}</span>
        </div>
        <div class="sc-prices">
            <span><b>Вход</b>${fmtPx(s.entry)}</span>
            <span><b>Выход</b>${fmtPx(s.exit)}</span>
            <span><b>Итог</b><span style="color:var(--${pnlCls});font-weight:700">${s.pnl_pct == null ? '—' : (s.pnl_pct >= 0 ? '+' : '') + s.pnl_pct + '%'}</span></span>
        </div>
        <div class="sc-strat">${icon} ${label} · за ${s.duration_min} мин · ${esc(s.strategy || '')}</div>
    </div>`;
}

function signalCardHTML(s) {
    const confPct = Math.round((s.confidence || 0) * 100);
    return `
    <div class="signal-card ${s.side.toLowerCase()}" data-symbol="${s.symbol}">
        <div class="sc-head">
            <span class="sc-sym">${s.symbol}</span>
            <span class="side-pill ${s.side.toLowerCase()}">${s.side}</span>
            ${fitChip(s)}
            <span class="sc-time">${fmtTime(s.ts)}</span>
        </div>
        ${s.fit && s.fit.warnings && s.fit.warnings.length ? `<div class="sc-strat" style="color:var(--amber)">⚠️ ${esc(s.fit.warnings[0])}</div>` : ''}
        <div class="sc-row">
            <span><b>Вход</b>${fmtPx(s.price)}</span>
            <span><b>TP</b><span style="color:var(--long)">${fmtPx(s.tp)}</span></span>
            <span><b>SL</b><span style="color:var(--short)">${fmtPx(s.sl)}</span></span>
        </div>
        <div class="sc-strat">${esc(s.strategy)}${s.strategy_auto ? ' · <span class="auto">⚡ авто-подбор по графику</span>' : ''} · TF ${s.timeframe}${s.rr_free ? ' · <span style="color:var(--cyan)">TP/SL по ATR графика</span>' : ''}${s.risk_manual ? ' · <span style="color:var(--cyan)">🎛 ручной риск' + (s.hold_min ? ` · ⏱ ~${Math.round(s.hold_min)} мин` : '') + '</span>' : ''}${s.my_strategy && s.my_strategy.hits ? ` · <span style="color:#7de9c3">🎯 ваша стратегия ${s.my_strategy.hits}/${s.my_strategy.total}</span>` : ''}${s.learned && s.learned.note ? ` · <span style="color:var(--amber)" title="Правило из самообучения сканера">🧠 ${esc(s.learned.note)}</span>` : ''}</div>
        ${s.reason ? `<div class="sc-strat" style="opacity:.8">${esc(s.reason)}</div>` : ''}
        ${s.sizing ? `<div class="sc-strat" style="color:#9fd3ff">💵 ${s.sizing.qty} ${s.symbol.replace('USDT','')} ≈ $${s.sizing.notional} (маржа $${s.sizing.margin} @${s.sizing.leverage}x) · риск −$${s.sizing.risk_usd} / цель +$${s.sizing.tp_usd}${s.sizing.fee ? ` · комиссия ≈$${s.sizing.fee}` : ''}${s.sizing.capped ? ' · ⚠️ урезано под 95% депозита' : ''}</div>` : ''}
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
let pfFromTs = 0, pfToTs = 0;   // ручной диапазон (мс) — приоритет над кнопками 30/90/180/365

document.querySelectorAll('#pf-period button').forEach(b =>
    b.addEventListener('click', () => {
        document.querySelectorAll('#pf-period button').forEach(x => x.classList.toggle('active', x === b));
        pfDays = parseInt(b.dataset.days);
        pfFromTs = pfToTs = 0;
        $('pf-from').value = ''; $('pf-to').value = '';
        $('pf-dates').classList.remove('active');
        loadPortfolio();
    }));

function pfApplyDates() {
    const f = $('pf-from').value, t = $('pf-to').value;
    if (!f || !t) return;
    const fts = new Date(f + 'T00:00:00').getTime();
    const tts = new Date(t + 'T23:59:59.999').getTime();
    if (!fts || !tts || fts >= tts) { toast('⚠️ Проверь диапазон дат: «с» должна быть раньше «по»'); return; }
    pfFromTs = fts; pfToTs = tts;
    pfDays = Math.max(1, Math.ceil((tts - fts) / 86400000));
    $('pf-dates').classList.add('active');
    document.querySelectorAll('#pf-period button').forEach(x => x.classList.remove('active'));
    loadPortfolio();
}
$('pf-from').addEventListener('change', pfApplyDates);
$('pf-to').addEventListener('change', pfApplyDates);
$('pf-dates-clear').addEventListener('click', () => {
    pfFromTs = pfToTs = 0;
    $('pf-from').value = ''; $('pf-to').value = '';
    $('pf-dates').classList.remove('active');
    const def = document.querySelector('#pf-period button[data-days="90"]') ||
                document.querySelector('#pf-period button');
    def && def.click();
});

function pfQuery() {
    const span = Math.max(1, Math.ceil(((pfToTs || Date.now()) - (pfFromTs || Date.now() - pfDays * 86400000)) / 86400000));
    const base = `days=${pfFromTs && pfToTs ? span : pfDays}`;
    return pfFromTs && pfToTs ? `${base}&from_ts=${pfFromTs}&to_ts=${pfToTs}` : base;
}

function pfLoader(show, text) {
    const el = $('pf-loading');
    if (!el) return;
    el.classList.toggle('hidden', !show);
    if (text) $('pf-loading-text').textContent = text;
}

async function loadPortfolio() {
    const empty = $('pf-empty'), content = $('pf-content');
    const full = !!loadPortfolio._full;
    pfLoader(true, full
        ? 'ПОЛНАЯ выгрузка с Bybit (365 дн = ~53 запроса, до 2 минут)…'
        : 'Обновляю сделки (инкрементально, обычно секунды)…');
    try {
        const d = await API.get(`/api/portfolio/overview?${pfQuery()}${full ? '&full=true' : ''}`);
        loadPortfolio._full = false;
        const st = d.stats;
        $('pf-source').textContent = d.source === 'bybit_api'
            ? '· данные: Bybit API (read-only)' + (d.cache && d.cache.mode === 'incremental'
                ? ` · кэш: +${d.cache.added} новых (всего ${d.cache.total})` : '')
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
            (d.recent || []).map(t => {
                const clickable = t.open_ts && t.close_ts;
                return `<tr class="${clickable ? 'deal-row' : ''}" ${clickable ? `data-symbol="${t.symbol}" data-side="${t.side}" data-open="${t.open_ts}" data-close="${t.close_ts}"` : ''} style="${clickable ? 'cursor:pointer' : ''}" title="${clickable ? 'Клик — график сделки с метками' : ''}">
                <td>${t.symbol}</td><td>${t.side || '—'}</td><td>${fmtPx(t.entry)}</td><td>${fmtPx(t.exit)}</td>
                 <td class="${t.pnl >= 0 ? 'pos' : 'neg'}">${t.pnl >= 0 ? '+' : ''}${fmt(t.pnl)}</td><td>${t.closed || '—'}</td></tr>`;
            }).join('');
        $('pf-recent').querySelectorAll('.deal-row').forEach(row =>
            row.addEventListener('click', () =>
                openDealModal(row.dataset.symbol, row.dataset.side, +row.dataset.open, +row.dataset.close)));

        // ДНК и персона подтягиваются параллельно
        loadDNA(false);
        loadPersona();
        loadMyStrategy();

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
    } finally {
        pfLoader(false);
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
    toast('⏳ Файл получен, загружаю на сервер…', '', 2000);
    try {
        const r = await API.post('/api/portfolio/csv', text, true);
        if (r.count > 0) {
            toast(`✅ CSV сохранён: распознано сделок: ${r.count}`);
            loadPortfolio();
        } else {
            toast(`⚠️ Файл загружен, но сделки не распознаны.<br><span style="color:var(--muted)">${esc(r.hint || 'Проверьте формат CSV')}</span>`, '', 8000);
        }
    } catch (e) {
        toast('❌ Загрузка CSV: ' + e.message);
    }
}

/* Диагностика Bybit API (сеть + read-only ключ) */
async function runBybitDiag() {
    const box = $('bybit-diag');
    box.innerHTML = '⏳ Диагностика…';
    await saveAllQuiet();
    try {
        const d = await API.get('/api/portfolio/diagnose');
        const hosts = (d.hosts || []).map(h =>
            `<div><span style="color:${h.ok ? 'var(--long)' : 'var(--short)'}">${h.ok ? '✓' : '✗'}</span> ${esc(h.host)} — ${esc(h.detail)}${h.latency_ms != null ? ` (${h.latency_ms} мс)` : ''}</div>`).join('');
        let key = '';
        if (d.key_check) {
            key = `<div style="margin-top:6px">Ключ: <span style="color:${d.key_check.ok ? 'var(--long)' : 'var(--short)'}">${d.key_check.ok ? '✓' : '✗'}</span> ${esc(d.key_check.detail || '')}</div>`;
        }
        if (d.time_offset_ms != null) {
            key += `<div style="color:var(--muted)">Рассинхрон часов с Bybit: скорректировано автоматически (${d.time_offset_ms} мс)</div>`;
        }
        const tips = (d.tips || []).map(t => `<div style="margin-top:6px;color:var(--amber)">💡 ${esc(t)}</div>`).join('');
        box.innerHTML = hosts + key + tips;
    } catch (e) {
        box.innerHTML = `<span style="color:var(--short)">ошибка: ${esc(e.message)}</span>`;
    }
}

/* ---------- ДНК торговли + AI-персона + график сделки ---------- */
let dnaState = { dna: null, mtime: 0 };
let personaES = null;

async function loadDNA(rebuild) {
    const box = $('dna-block');
    if (rebuild) box.innerHTML = '<div class="thinking"><span class="pulse"></span>Считаю контекст каждой сделки на графиках (MAE/MFE, индикаторы в момент входа) — до минуты…</div>';
    try {
        const d = await API.get(`/api/portfolio/dna?${pfQuery()}${rebuild ? '&rebuild=true' : ''}`);
        dnaState.dna = d.dna;
        renderDNA(d.dna, d.cached);
        loadChart(); // обновить маркеры сделок на терминале
    } catch (e) {
        if (rebuild) box.innerHTML = '';
    }
}

function renderDNA(dna, cached) {
    const box = $('dna-block');
    if (!dna) { box.innerHTML = ''; return; }
    const sq = dna.stop_quality || {};
    const bh = dna.behavior || {};
    const chip = (z, good) => `<span class="chip" style="border-color:${good ? 'rgba(0,224,164,.4)' : 'rgba(255,92,122,.4)'};${good ? 'color:var(--long)' : 'color:var(--short)'}" title="winrate ${z.winrate}% · ${z.trades} сделок · PnL ${z.pnl}">${esc(z.zone)} · ${z.winrate}%</span>`;
    const builtAt = new Date(dna.built_at * 1000).toLocaleString('ru-RU');
    box.innerHTML = `
    <div class="review-block" style="border-color:rgba(34,211,238,.3)">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
            <h4 style="margin:0">🧬 ДНК вашей торговли</h4>
            <span class="hint" style="margin:0">построена ${builtAt} · сделок: ${dna.total_trades}${dna.enriched ? `, на графиках разобрано: ${dna.enriched}` : ''}${cached ? ' · из кэша' : ''}</span>
            <div class="spacer"></div>
            <button class="btn btn-sm" id="dna-rebuild-btn">⟳ Пересчитать</button>
        </div>
        <div style="margin-top:10px">
            <div class="hint" style="margin-bottom:4px">💪 Сильные зоны</div>
            <div>${(dna.strengths || []).map(z => chip(z, true)).join('') || '<span class="hint">пока не выявлены</span>'}</div>
            <div class="hint" style="margin:8px 0 4px">⚠️ Слабые зоны</div>
            <div>${(dna.weaknesses || []).map(z => chip(z, false)).join('') || '<span class="hint">пока не выявлены</span>'}</div>
        </div>
        <div style="display:flex;gap:22px;flex-wrap:wrap;margin-top:12px;font-size:12px;font-family:var(--mono)">
            ${sq.avg_capture != null ? `<span><b style="color:var(--muted);font-weight:500;font-size:10px;text-transform:uppercase">Забираете движения</b><br>${sq.avg_capture}%</span>` : ''}
            ${sq.avg_mae_winners != null ? `<span><b style="color:var(--muted);font-weight:500;font-size:10px;text-transform:uppercase">MAE прибыльных</b><br>${sq.avg_mae_winners}%</span>` : ''}
            ${sq.avg_mae_losers != null ? `<span><b style="color:var(--muted);font-weight:500;font-size:10px;text-transform:uppercase">MAE убыточных</b><br>${sq.avg_mae_losers}%</span>` : ''}
            ${sq.avg_entry_pctile_losers != null ? `<span><b style="color:var(--muted);font-weight:500;font-size:10px;text-transform:uppercase">Процентиль входа (лужеи/виннеры)</b><br>${sq.avg_entry_pctile_losers} / ${sq.avg_entry_pctile_winners ?? '—'}</span>` : ''}
            ${bh.revenge_trades ? `<span><b style="color:var(--muted);font-weight:500;font-size:10px;text-transform:uppercase">Реванш-сделок (&lt;30м после стопа)</b><br>${bh.revenge_trades}${bh.revenge_winrate != null ? ` · winrate ${bh.revenge_winrate}%` : ''}</span>` : ''}
        </div>
    </div>`;
    $('dna-rebuild-btn').addEventListener('click', () => loadDNA(true));
}

/* AI-персона: все агенты анализируют ДНК */
async function loadPersona() {
    try {
        const d = await API.get('/api/portfolio/persona');
        if (d.persona) renderPersona(d.persona);
    } catch (e) { }
}

function runPersonaBuild() {
    const box = $('pf-persona');
    if (!dnaState.dna) {
        toast('Сначала постройте ДНК (кнопка «🧬 ДНК торговли» → «⟳ Пересчитать» при необходимости)');
        loadDNA(true);
        return;
    }
    if (personaES) personaES.close();
    box.innerHTML = '<div class="review-block"><h4>🔮 Агенты изучают вашу торговлю…</h4><div id="persona-progress"></div></div>';
    const es = new EventSource('/api/portfolio/persona/build');
    personaES = es;
    es.addEventListener('progress', e => {
        const p = JSON.parse(e.data);
        const row = document.getElementById('persona-progress');
        if (!row) return;
        const stTxt = { working: '⏳ думает…', done: '✅', error: '⚠️ ' + (p.detail || 'ошибка'), skipped: '⏸ выключен', no_key: '🔑 нет ключа' }[p.status] || p.status;
        box.querySelector('#persona-progress').insertAdjacentHTML('beforeend',
            `<div style="font-size:12px;margin:4px 0"><b>${esc(p.name)}</b> <span style="color:var(--muted)">${stTxt}</span>
             ${p.verdict && p.verdict.tokens_est ? `<span class="hint" style="display:inline">≈${p.verdict.tokens_est} ток.</span>` : ''}</div>`);
    });
    es.addEventListener('persona', e => {
        renderPersona(JSON.parse(e.data));
    });
    es.addEventListener('error', e => {
        try { const d = JSON.parse(e.data); box.insertAdjacentHTML('beforeend', `<div class="empty" style="color:var(--short)">⚠️ ${esc(d.message || '')}</div>`); } catch { }
        es.close();
    });
    es.addEventListener('done', () => { es.close(); personaES = null; });
    es.onerror = () => { es.close(); personaES = null; };
}

function renderPersona(p) {
    const box = $('pf-persona');
    const m = p.merged || {};
    const li = arr => (arr || []).map(x => `<li>${esc(x)}</li>`).join('');
    const built = p.built_at ? new Date(p.built_at * 1000).toLocaleString('ru-RU') : '';
    const roles = Object.keys(p.by_role || {}).length;
    box.innerHTML = `
    <div class="review-block" style="border-color:rgba(139,92,246,.45)">
        <div style="display:flex;align-items:center;gap:10px">
            <h4 style="margin:0">🔮 Персона вашей торговли</h4>
            <span class="hint" style="margin:0">${roles} агентов · ${built}</span>
            <div class="spacer"></div>
            <button class="btn btn-sm" id="persona-rebuild-btn">⟳ Пересобрать</button>
        </div>
        ${m.essence ? `<div style="font-size:13px;line-height:1.6;margin-top:10px"><i>${esc(m.essence)}</i></div>` : ''}
        <div class="pf-cols" style="margin-top:12px">
            <div>
                <h4>💪 Сильные стороны</h4><ul>${li(m.strengths)}</ul>
                <h4 style="margin-top:10px">⚠️ Слабые места</h4><ul>${li(m.weaknesses)}</ul>
            </div>
            <div>
                <h4>🎯 Какие сигналы вам подходят</h4><ul>${li(m.signal_hints)}</ul>
                <h4 style="margin-top:10px">⛔ Чего избегать</h4><ul>${li(m.avoid)}</ul>
            </div>
        </div>
        <div class="hint" style="margin-top:10px">Эта персона автоматически добавляется в промпт каждого агента при «Совете агентов» — сигналы оцениваются с учётом вашего стиля.</div>
    </div>`;
    $('persona-rebuild-btn').addEventListener('click', runPersonaBuild);
}

/* ---------- Модалка графика сделки ---------- */
let dealChartObj = null, dealSeries = null, dealVolSeries = null, dealMarkers = null;

function openDealModal(symbol, side, openTs, closeTs) {
    $('deal-modal').classList.add('open');
    $('deal-title').textContent = `📌 ${symbol} · ${side}`;
    $('deal-sub').textContent = '';
    $('deal-info').innerHTML = '<div class="thinking"><span class="pulse"></span>Загружаю график периода сделки…</div>';
    const el = $('deal-chart');
    el.innerHTML = '';
    if (dealChartObj) { dealChartObj.remove(); dealChartObj = null; }

    API.get(`/api/portfolio/deal-chart?symbol=${symbol}&side=${side}&open_ts=${openTs}&close_ts=${closeTs}`)
        .then(d => {
            dealChartObj = LightweightCharts.createChart(el, {
                width: el.clientWidth, height: 340,
                layout: { background: { type: LightweightCharts.ColorType.Solid, color: 'transparent' }, textColor: '#8a93b8', fontFamily: '"JetBrains Mono", monospace', fontSize: 11 },
                grid: { vertLines: { color: 'rgba(148,163,255,.06)' }, horzLines: { color: 'rgba(148,163,255,.06)' } },
                rightPriceScale: { borderColor: 'rgba(148,163,255,.15)' },
                timeScale: { borderColor: 'rgba(148,163,255,.15)', timeVisible: true },
            });
            dealSeries = dealChartObj.addSeries(LightweightCharts.CandlestickSeries, {
                upColor: '#00e0a4', downColor: '#ff5c7a',
                borderUpColor: '#00e0a4', borderDownColor: '#ff5c7a',
                wickUpColor: 'rgba(0,224,164,.7)', wickDownColor: 'rgba(255,92,122,.7)',
            });
            dealSeries.setData(d.candles);
            const longSide = side.toLowerCase() === 'buy' || side.toLowerCase() === 'long';
            const eC = d.candles[d.entry_idx], xC = d.candles[d.exit_idx];
            dealMarkers = LightweightCharts.createSeriesMarkers(dealSeries, [
                { time: eC.time, position: longSide ? 'belowBar' : 'aboveBar', color: '#22d3ee', shape: longSide ? 'arrowUp' : 'arrowDown', text: `вход ${fmtPx(d.entry_px)}` },
                { time: xC.time, position: longSide ? 'aboveBar' : 'belowBar', color: '#8b5cf6', shape: 'circle', text: `выход ${fmtPx(d.exit_px)}` },
            ]);
            dealSeries.createPriceLine({ price: d.entry_px, color: '#22d3ee', lineStyle: LightweightCharts.LineStyle.Dashed, lineWidth: 1, title: 'вход' });
            dealSeries.createPriceLine({ price: d.mfe_px, color: 'rgba(0,224,164,.8)', lineStyle: LightweightCharts.LineStyle.Dotted, lineWidth: 1, title: 'MFE (макс. в плюс)' });
            dealSeries.createPriceLine({ price: d.mae_px, color: 'rgba(255,92,122,.8)', lineStyle: LightweightCharts.LineStyle.Dotted, lineWidth: 1, title: 'MAE (макс. против)' });
            dealChartObj.timeScale().fitContent();
            dealChartObj.timeScale().setVisibleRange({
                from: d.candles[0].time, to: d.candles[d.candles.length - 1].time,
            });

            const ind = d.indicators || {};
            const hold = d.candles[d.exit_idx].time - d.candles[d.entry_idx].time;
            $('deal-sub').textContent = `${d.tf} · RSI при входе ${ind.rsi ?? '—'} · режим: ${d.regime}`;
            $('deal-info').innerHTML = `
                <div style="display:flex;gap:22px;flex-wrap:wrap;font-size:12px;font-family:var(--mono)">
                    <span><b style="color:var(--muted);font-weight:500;font-size:10px;text-transform:uppercase">RSI при входе</b><br>${ind.rsi ?? '—'}</span>
                    <span><b style="color:var(--muted);font-weight:500;font-size:10px;text-transform:uppercase">ATR%</b><br>${ind.atr_pct ?? '—'}</span>
                    <span><b style="color:var(--muted);font-weight:500;font-size:10px;text-transform:uppercase">Объём ×ср</b><br>${ind.volume_ratio ?? '—'}</span>
                    <span><b style="color:var(--muted);font-weight:500;font-size:10px;text-transform:uppercase">Режим рынка</b><br>${esc(d.regime)}</span>
                    <span><b style="color:var(--muted);font-weight:500;font-size:10px;text-transform:uppercase">Удержание</b><br>${Math.round(hold / 3600 * 10) / 10} ч</span>
                </div>`;
        })
        .catch(e => {
            $('deal-info').innerHTML = `<div class="empty" style="color:var(--short)">⚠️ ${esc(e.message)}</div>`;
        });
}

$('deal-close').addEventListener('click', () => {
    $('deal-modal').classList.remove('open');
    if (dealChartObj) { dealChartObj.remove(); dealChartObj = null; }
});
$('deal-modal').addEventListener('click', e => {
    if (e.target === $('deal-modal')) {
        $('deal-modal').classList.remove('open');
        if (dealChartObj) { dealChartObj.remove(); dealChartObj = null; }
    }
});

/* AI-разбор торговли */
function runAIReview() {
    const box = $('pf-review');
    box.innerHTML = '<div class="review-block"><div class="thinking"><span class="pulse"></span>💼 Аналитик разбирает вашу историю сделок…</div></div>';
    if (state.reviewES) state.reviewES.close();
    const es = new EventSource(`/api/portfolio/ai-review?${pfQuery()}`);
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
        buildSbtCfg();
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
    // мульти-таймфреймы сканера (чипы)
    const tfs = cfg.live.signal_timeframes || [cfg.live.timeframe || '5m'];
    document.querySelectorAll('#set-tf-chips input').forEach(i => { i.checked = tfs.includes(i.dataset.tf); });
    $('set-min-conf').value = cfg.live.min_confidence;
    $('set-max-trades').value = cfg.rules.max_trades_per_day;
    // ручной риск-фактор
    const mr = cfg.live.manual_risk || {};
    if ($('set-risk-tp')) $('set-risk-tp').value = mr.tp_pct || 0;
    if ($('set-risk-sl')) $('set-risk-sl').value = mr.sl_pct || 0;
    if ($('set-risk-hold')) $('set-risk-hold').value = mr.hold_min || 0;
    const rc = cfg.live.risk_calc || {};
    if ($('set-deposit')) $('set-deposit').value = rc.deposit_usd ?? 1000;
    if ($('set-risk-pct')) $('set-risk-pct').value = rc.risk_per_trade_pct ?? 3;
    if ($('set-leverage')) $('set-leverage').value = rc.leverage ?? 20;
    $('set-min-tp').value = cfg.rules.min_tp_pct;
    $('set-max-hold').value = cfg.rules.max_hold_hours;
    $('set-sl').value = cfg.rules.default_sl_pct;
    $('ntf-sound').checked = cfg.live.notify_sound;
    $('ntf-toasts').checked = cfg.live.notify_toasts;
    const rr = $('set-rr-binding'); if (rr) rr.checked = cfg.live.rr_binding !== false;

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
    // таймфреймы: собираем отмеченные чипы
    const tfs = [...document.querySelectorAll('#set-tf-chips input:checked')].map(i => i.dataset.tf);
    const tfList = tfs.length ? tfs : ['5m'];
    u.signal_timeframes = tfList.join(',');
    u.default_timeframe = tfList[0];
    u.min_confidence = parseFloat(v('set-min-conf')) || 0.55;
    u.signal_strategy = v('set-signal-strategy') || 'auto';
    const mt = parseInt(v('set-max-trades'));          // 0 = без лимита (важно не заменять дефолтом)
    u.max_trades_per_day = isNaN(mt) ? 0 : mt;
    // ручной риск-фактор
    u.manual_tp_pct = parseFloat(v('set-risk-tp')) || 0;
    u.manual_sl_pct = parseFloat(v('set-risk-sl')) || 0;
    u.manual_hold_min = parseFloat(v('set-risk-hold')) || 0;
    u.deposit_usd = parseFloat(v('set-deposit')) || 1000;
    u.risk_per_trade_pct = parseFloat(v('set-risk-pct')) || 0;
    u.leverage = parseFloat(v('set-leverage')) || 20;
    u.min_tp_pct = parseFloat(v('set-min-tp')) || 2;
    u.max_hold_hours = parseFloat(v('set-max-hold')) || 4;
    u.default_sl_pct = parseFloat(v('set-sl')) || 1;
    u.notify_sound = $('ntf-sound').checked ? 'true' : 'false';
    u.notify_toasts = $('ntf-toasts').checked ? 'true' : 'false';
    const rr = $('set-rr-binding'); if (rr) u.rr_binding = rr.checked ? 'true' : 'false';
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
// двойной клик по ⟳ = ПОЛНАЯ перезагрузка истории мимо кэша
$('pf-reload').addEventListener('dblclick', () => { loadPortfolio._full = true; loadPortfolio(); });
$('pf-ai-btn').addEventListener('click', runAIReview);
$('pf-dna-btn').addEventListener('click', () => {
    loadPortfolio().then(() => loadDNA(true));
});
$('pf-persona-btn').addEventListener('click', runPersonaBuild);

/* ---------- 🧠 Обучение сканера ---------- */
async function loadLearned() {
    const box = $('learn-body');
    if (!box) return;
    try {
        const d = await API.get('/api/signals/learned');
        const rules = d.rules || {};
        const syms = Object.keys(rules);
        if (!syms.length) {
            box.innerHTML = 'Статистика появится после первых закрытых сигналов (TP/SL/таймаут/разворот).';
            return;
        }
        const icon = { block: '⛔', tighten: '🧱', boost: '🚀', watch: '👀' };
        box.innerHTML = `<div class="hint" style="margin-bottom:6px">Разобрано исходов: ${d.outcomes} · правила: ≥${d.thresholds.hard_sample} и wr<${d.thresholds.block_wr}% → блок · ≥${d.thresholds.min_sample} и wr<${d.thresholds.tighten_wr}% → порог ↑ · wr≥${d.thresholds.boost_wr}% → порог ↓</div>` +
            syms.map(sym => `<div style="margin:4px 0"><b style="font-family:var(--mono);font-size:12px">${sym}</b>: ` +
                rules[sym].map(r =>
                    `<span class="ms-chip" title="выборка ${r.sample}, Σ ${r.total_pnl_pct}%">${icon[r.action] || ''} ${r.side} · ${esc(r.stype)} · wr ${r.winrate}% <span style="color:var(--muted)">${r.action === 'block' ? '— глушится' : r.action === 'tighten' ? '— порог ↑' : r.action === 'boost' ? '— порог ↓' : '— наблюдаем'}</span></span>`
                ).join('') + '</div>').join('');
    } catch (e) { /* тихо */ }
}
$('learn-refresh') && $('learn-refresh').addEventListener('click', loadLearned);
$('learn-curator') && $('learn-curator').addEventListener('click', async () => {
    const b = $('learn-curator');
    b.disabled = true; b.textContent = '⏳ gpt-6-astra разбирает…';
    try {
        const d = await API.post('/api/signals/ai-curator', {});
        loadLearned();
        const n = (d.applied || []).length;
        toast(`🤖 Куратор (${d.model}): ${n ? n + ' правок' : 'без правок'}${d.summary ? ' — ' + esc(d.summary) : ''}`, 'long', 12000);
    } catch (e) {
        toast('❌ ' + e.message);
    } finally {
        b.disabled = false; b.textContent = '🤖 AI-куратор';
    }
});
$('learn-reset') && $('learn-reset').addEventListener('click', async () => {
    try { await API.post('/api/signals/learned/reset', {}); loadLearned(); toast('🧠 Статистика обучения стёрта'); }
    catch (e) { toast('❌ ' + e.message); }
});
loadLearned();


function buildSbtCfg() {
    const box = $('sbt-symbols');
    if (!box) return;
    const wl = (state.config && state.config.live && state.config.live.watchlist) || [];
    box.innerHTML = wl.map(s =>
        `<label class="chk"><input type="checkbox" data-sym="${s}" checked>${s.replace('USDT', '')}</label>`).join('');
}

$('sbt-toggle') && $('sbt-toggle').addEventListener('click', () =>
    $('sbt-cfg').classList.toggle('hidden'));

function sbtStatsHtml(agg) {
    const ex = agg.exits || {};
    return `<div class="pf-stats" style="grid-template-columns:repeat(auto-fit,minmax(120px,1fr))">
        ${[['Сделок', agg.trades || 0, ''],
        ['Winrate', (agg.winrate ?? '—') + '%', agg.winrate >= 50 ? 'pos' : 'neg'],
        ['Суммарный PnL', (agg.total_pnl_pct >= 0 ? '+' : '') + (agg.total_pnl_pct ?? '—') + '%', agg.total_pnl_pct >= 0 ? 'pos' : 'neg'],
        ['Средний PnL', (agg.avg_pnl_pct ?? '—') + '%', agg.avg_pnl_pct >= 0 ? 'pos' : 'neg'],
        ['Profit factor', agg.profit_factor ?? '—', (agg.profit_factor || 0) >= 1.2 ? 'pos' : ''],
        ['Ср. длит., мин', agg.avg_duration_min ?? '—', ''],
        ['Выходы TP/SL/тмаут', `${ex.TP || 0}/${ex.SL || 0}/${ex.timeout || 0}`, '']]
        .map(c => `<div class="stat-card"><b>${c[0]}</b><div class="v ${c[2]}">${c[1]}</div></div>`).join('')}
    </div>`;
}

function sbtRows(rows, cols) {
    return '<table class="tbl">' +
        '<tr>' + cols.map(c => `<th>${c[0]}</th>`).join('') + '</tr>' +
        rows.map(r => '<tr>' + cols.map((c, i) =>
            `<td class="${c[2] ? c[2](r) : ''}">${c[1](r)}</td>`).join('') + '</tr>').join('') +
        '</table>';
}

async function runSignalsBacktest() {
    const btn = $('sbt-run'), body = $('sbt-body');
    const symbols = [...document.querySelectorAll('#sbt-symbols input:checked')].map(i => i.dataset.sym);
    const tfs = [...document.querySelectorAll('#sbt-tfs input:checked')].map(i => i.dataset.tf);
    const days = parseInt($('sbt-days').value) || 7;
    if (!symbols.length) { toast('⚠️ Выбери хотя бы одну монету'); return; }
    if (!tfs.length) { toast('⚠️ Выбери хотя бы один таймфрейм'); return; }
    btn.disabled = true; btn.textContent = '⏳ считаю…';
    body.style.display = '';
    body.innerHTML = '<div class="empty">Скачиваю историю и прогоняю сканер по барам… (1m — до минуты)</div>';
    try {
        const d = await API.post('/api/signals/backtest', { symbols, tfs, days });
        const p = d.params || {};
        let html = `<div class="hint" style="margin-bottom:8px">Режим: ${esc(p.strategy_mode || '')} · порог ${p.min_confidence} · риск: ${esc(p.risk || '')} · данные: реальные свечи Bybit</div>`;
        html += sbtStatsHtml(d.overall || {});
        const pc = [['Монета', r => r.symbol], ['TF', r => r.tf], ['Дн', r => r.days],
                    ['Сделок', r => r.trades], ['WR', r => (r.winrate ?? '—') + '%', r => r.winrate >= 50 ? 'pos' : 'neg'],
                    ['PnL %', r => r.total_pnl_pct ?? '—', r => r.total_pnl_pct >= 0 ? 'pos' : 'neg'],
                    ['PF', r => r.profit_factor ?? '—'],
                    ['TP/SL/тм', r => { const e = r.exits || {}; return `${e.TP || 0}/${e.SL || 0}/${e.timeout || 0}`; }]];
        if ((d.per_pair || []).length) {
            html += '<h4 style="margin:12px 0 6px">По монетам и графикам</h4>' + sbtRows(d.per_pair, pc);
        }
        const sc = [['Стратегия', r => esc(r.strategy)], ['Сделок', r => r.trades],
                    ['WR', r => (r.winrate ?? '—') + '%', r => r.winrate >= 50 ? 'pos' : 'neg'],
                    ['PnL %', r => r.total_pnl_pct ?? '—', r => r.total_pnl_pct >= 0 ? 'pos' : 'neg'],
                    ['PF', r => r.profit_factor ?? '—']];
        if ((d.by_strategy || []).length) {
            html += '<h4 style="margin:12px 0 6px">По стратегиям (что реально срабатывало)</h4>' + sbtRows(d.by_strategy, sc);
        }
        const tc = [['Монета', r => r.symbol], ['TF', r => r.tf], ['Сторона', r => r.side],
                    ['Стратегия', r => esc(r.strategy)], ['Выход', r => r.reason],
                    ['PnL %', r => (r.pnl_pct >= 0 ? '+' : '') + r.pnl_pct, r => r.pnl_pct >= 0 ? 'pos' : 'neg'],
                    ['Мин', r => r.duration_min], ['Увер.', r => r.conf]];
        if ((d.recent_trades || []).length) {
            html += '<h4 style="margin:12px 0 6px">Последние виртуальные сделки</h4>' + sbtRows(d.recent_trades.slice(0, 15), tc);
        }
        (d.notes || []).forEach(n => { html += `<div class="hint" style="margin-top:6px">ℹ️ ${esc(n)}</div>`; });
        (d.errors || []).forEach(e => { html += `<div class="hint" style="margin-top:6px;color:var(--amber)">⚠️ ${esc(e)}</div>`; });
        if (!(d.overall || {}).trades) html += '<div class="empty" style="margin-top:10px">За период сканер не дал ни одного сигнала выше порога — попробуй больше дней, другой TF или ниже порог уверенности.</div>';
        body.innerHTML = html;
        toast(`📈 Бэктест готов: ${(d.overall || {}).trades || 0} вирт. сделок`);
    } catch (e) {
        body.innerHTML = `<div class="empty" style="color:var(--amber)">⚠️ ${esc(e.message)}</div>`;
    } finally {
        btn.disabled = false; btn.textContent = '▶ Прогнать';
    }
}
$('sbt-run') && $('sbt-run').addEventListener('click', runSignalsBacktest);


function renderMyStrategy(ms) {
    const box = $('mystrat-body');
    if (!box) return;
    if (!ms || ms.empty) { box.classList.add('hidden'); box.innerHTML = ''; return; }
    box.classList.remove('hidden');
    const symChips = (ms.symbols || []).map(s => `<span class="ms-chip">${s}</span>`).join('');
    const rules = (ms.rules || []).map(r => `<li>${esc(r)}</li>`).join('');
    const avoid = (ms.avoid && ms.avoid.length)
        ? `<div class="ms-avoid"><b>Избегать:</b> ${ms.avoid.map(esc).join(' · ')}</div>` : '';
    box.innerHTML = `
        <div class="ms-head">🎯 ${esc(ms.name)} <span class="badge">подключена к сканеру</span>
            <span style="color:var(--muted);font-size:11px;font-weight:400">· выведена из ${ms.base_trades} сделок за ${ms.days} дн.</span></div>
        <div style="margin-top:6px">${symChips}</div>
        <ul>${rules}</ul>
        ${avoid}`;
}

async function loadMyStrategy() {
    try {
        const ms = await API.get('/api/portfolio/my-strategy');
        renderMyStrategy(ms);
    } catch (e) { /* молча: блок просто останется скрытым */ }
}

$('pf-mystrat-btn').addEventListener('click', async () => {
    const b = $('pf-mystrat-btn');
    b.disabled = true; b.textContent = '⏳ вывожу…';
    try {
        const ms = await API.post(`/api/portfolio/my-strategy?${pfQuery()}`, {});
        renderMyStrategy(ms);
        toast('🎯 Стратегия выведена из ваших сделок и подключена к сканеру');
    } catch (e) {
        toast('❌ ' + e.message);
    } finally {
        b.disabled = false; b.textContent = '🎯 Моя стратегия';
    }
});

$('diag-bybit-btn').addEventListener('click', runBybitDiag);
$('flt-strong').addEventListener('change', renderFeed);
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
    // SaaS-гейт: проверяем сессию/подписку ДО подключения каналов
    try {
        const me = await fetch('/api/auth/me');
        if (me.ok) {
            const m = await me.json();
            if (m.enabled) {
                if (!m.sub_active) { location = '/pay.html'; return; }
                // кабинет (дни доступа/ключи Bybit) — быстрая ссылка из шапки
                const acc = document.createElement('a');
                acc.href = '/pay.html'; acc.className = 'btn'; acc.id = 'acc-btn';
                acc.title = `Подписка: осталось ${m.days_left} дн. Кабинет: ключи Bybit, продление`;
                acc.textContent = `👤 ${m.days_left}д`;
                const gear = document.querySelector('.header-right, header .btn, .topbar');
                (gear && gear.parentElement ? gear.parentElement : document.body).appendChild(acc);
                acc.style.cssText = 'position:fixed;top:14px;right:96px;z-index:999;padding:7px 14px;font-size:13px;text-decoration:none';
            }
        } else if (me.status === 401) { location = '/landing.html'; return; }
    } catch (_) { /* сеть недоступна — продолжаем в обычном режиме */ }
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
