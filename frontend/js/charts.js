/* Профессиональные canvas-графики. Свечной график стилизован под официальный
   Bybit: цвета свечей Bybit, OHLC-легенда слева сверху, бейдж последней цены на
   правой оси, тонкая сетка, кроссхейр при наведении. */

/* Фирменные цвета Bybit */
const BB = {
    up: '#2ebd85',        // зелёная свеча
    down: '#f6465d',      // красная свеча
    bg: 'transparent',
    grid: 'rgba(255,255,255,0.045)',
    axisText: '#8b98ad',
    cross: 'rgba(139,152,173,0.5)',
    legendText: '#e6eaf2',
};

function setupCanvas(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = Math.max(50, rect.width * dpr);
    canvas.height = Math.max(50, rect.height * dpr);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return ctx;
}

function _fmtVol(v) {
    if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B';
    if (v >= 1e6) return (v / 1e6).toFixed(2) + 'M';
    if (v >= 1e3) return (v / 1e3).toFixed(1) + 'K';
    return Math.round(v);
}

/* Свечной график Bybit-стиля. meta: {symbol, interval} — для заголовка. */
function drawCandles(canvas, candles, meta) {
    if (!canvas || !candles || !candles.length) return;
    canvas._bb = { candles, meta: meta || {} };
    _renderBB(canvas, null, null);

    if (!canvas._hoverBound) {
        canvas._hoverBound = true;
        canvas.addEventListener('mousemove', e => {
            if (!canvas._bb) return;
            const rect = canvas.getBoundingClientRect();
            _renderBB(canvas, e.clientX - rect.left, e.clientY - rect.top);
        });
        canvas.addEventListener('mouseleave', () => {
            if (canvas._bb) _renderBB(canvas, null, null);
        });
        canvas.addEventListener('touchmove', e => {
            if (!canvas._bb) return;
            const rect = canvas.getBoundingClientRect();
            const t = e.touches[0];
            _renderBB(canvas, t.clientX - rect.left, t.clientY - rect.top);
        }, { passive: true });
        canvas.addEventListener('touchend', () => {
            if (canvas._bb) setTimeout(() => _renderBB(canvas, null, null), 1200);
        });
    }
}

function _renderBB(canvas, mouseX, mouseY) {
    const { candles, meta } = canvas._bb;
    const ctx = setupCanvas(canvas);
    const W = canvas.clientWidth, H = canvas.clientHeight;
    ctx.clearRect(0, 0, W, H);

    /* --- раскладка: [цены][объём][ось времени] --- */
    const padTop = 30, padRight = 66, padBottom = 22, padLeft = 6;
    const volH = Math.max(44, H * 0.16);
    const priceBottom = H - padBottom - volH - 6;
    const priceH = priceBottom - padTop;

    const n = candles.length;
    const highs = candles.map(c => c.high);
    const lows = candles.map(c => c.low);
    const vols = candles.map(c => c.volume);
    let min = Math.min(...lows), max = Math.max(...highs);
    const pad = (max - min) * 0.06 || 1;
    const lo = min - pad, hi = max + pad;
    const y = v => priceBottom - ((v - lo) / (hi - lo)) * priceH;

    const plotW = W - padLeft - padRight;
    const cw = plotW / n;
    const x = i => padLeft + i * cw + cw / 2;

    /* --- сетка и правая ось цен --- */
    ctx.font = '10px "IBM Plex Mono", monospace';
    ctx.textAlign = 'left';
    const rows = 5;
    for (let g = 0; g <= rows; g++) {
        const val = lo + (g / rows) * (hi - lo);
        const gy = y(val);
        ctx.strokeStyle = BB.grid;
        ctx.beginPath(); ctx.moveTo(padLeft, gy); ctx.lineTo(W - padRight, gy); ctx.stroke();
        ctx.fillStyle = BB.axisText;
        ctx.fillText(fmtPrice(val), W - padRight + 6, gy + 3);
    }
    // пунктирные вертикальные линии сетки
    const tStep = Math.max(1, Math.round(n / 8));
    ctx.strokeStyle = BB.grid;
    for (let i = 0; i < n; i += tStep) {
        ctx.beginPath(); ctx.moveTo(x(i), padTop); ctx.lineTo(x(i), H - padBottom); ctx.stroke();
    }

    /* --- объёмы --- */
    const vMax = Math.max(...vols) || 1;
    for (let i = 0; i < n; i++) {
        const c = candles[i];
        const up = c.close >= c.open;
        const vh = Math.max(1, (c.volume / vMax) * (volH - 6));
        ctx.fillStyle = up ? 'rgba(46,189,133,0.28)' : 'rgba(246,70,93,0.28)';
        ctx.fillRect(x(i) - cw * 0.3, H - padBottom - vh, cw * 0.6, vh);
    }

    /* --- свечи (Bybit: тело залито полностью, фитиль тонкий) --- */
    for (let i = 0; i < n; i++) {
        const c = candles[i];
        const up = c.close >= c.open;
        const col = up ? BB.up : BB.down;
        ctx.strokeStyle = col;
        ctx.fillStyle = col;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x(i), y(c.high));
        ctx.lineTo(x(i), y(c.low));
        ctx.stroke();
        const bodyW = Math.max(1, cw * 0.62);
        const bodyTop = y(Math.max(c.open, c.close));
        const bodyH = Math.max(1, y(Math.min(c.open, c.close)) - bodyTop);
        ctx.fillRect(x(i) - bodyW / 2, bodyTop, bodyW, bodyH);
    }

    /* --- ось времени --- */
    ctx.textAlign = 'center';
    ctx.fillStyle = BB.axisText;
    for (let i = 0; i < n; i += tStep) {
        ctx.fillText(_fmtTimeShort(candles[i].ts), x(i), H - 6);
    }

    /* --- бейдж последней цены на правой оси (как на Bybit) --- */
    const last = candles[n - 1];
    const lastUp = last.close >= last.open;
    const lp = last.close;
    if (lp >= lo && lp <= hi) {
        const ly = y(lp);
        ctx.setLineDash([4, 3]);
        ctx.strokeStyle = lastUp ? 'rgba(46,189,133,0.55)' : 'rgba(246,70,93,0.55)';
        ctx.beginPath(); ctx.moveTo(padLeft, ly); ctx.lineTo(W - padRight, ly); ctx.stroke();
        ctx.setLineDash([]);
        // плашка с ценой
        ctx.font = 'bold 10px "IBM Plex Mono", monospace';
        const txt = fmtPrice(lp);
        const tw = ctx.measureText(txt).width + 10;
        ctx.fillStyle = lastUp ? BB.up : BB.down;
        _rr(ctx, W - padRight, ly - 8, tw, 16, 3);
        ctx.fill();
        ctx.fillStyle = '#0d1117';
        ctx.textAlign = 'left';
        ctx.fillText(txt, W - padRight + 5, ly + 3.5);
    }

    /* --- кроссхейр + OHLC-легенда --- */
    let legendCandle = last;
    if (mouseX != null && mouseX >= padLeft && mouseX <= W - padRight) {
        const idx = Math.max(0, Math.min(n - 1, Math.floor((mouseX - padLeft) / cw)));
        const c = candles[idx];
        legendCandle = c;
        // вертикальная линия
        ctx.setLineDash([3, 3]);
        ctx.strokeStyle = BB.cross;
        ctx.beginPath(); ctx.moveTo(x(idx), padTop); ctx.lineTo(x(idx), H - padBottom); ctx.stroke();
        // горизонтальная линия + ценовой тег
        if (mouseY >= padTop && mouseY <= priceBottom) {
            const my = Math.max(padTop, Math.min(priceBottom, mouseY));
            ctx.beginPath(); ctx.moveTo(padLeft, my); ctx.lineTo(W - padRight, my); ctx.stroke();
            ctx.setLineDash([]);
            const pv = lo + ((priceBottom - my) / priceH) * (hi - lo);
            ctx.font = '10px "IBM Plex Mono", monospace';
            const pvTxt = fmtPrice(pv);
            const pvw = ctx.measureText(pvTxt).width + 10;
            ctx.fillStyle = '#2a3242';
            _rr(ctx, W - padRight, my - 8, pvw, 16, 3);
            ctx.fill();
            ctx.fillStyle = BB.legendText;
            ctx.textAlign = 'left';
            ctx.fillText(pvTxt, W - padRight + 5, my + 3.5);
            // тег времени снизу
            const tTxt = _fmtTimeFull(c.ts);
            const ttw = ctx.measureText(tTxt).width + 10;
            ctx.fillStyle = '#2a3242';
            _rr(ctx, x(idx) - ttw / 2, H - padBottom, ttw, 16, 3);
            ctx.fill();
            ctx.fillStyle = BB.legendText;
            ctx.textAlign = 'center';
            ctx.fillText(tTxt, x(idx), H - padBottom + 12);
        } else {
            ctx.setLineDash([]);
        }
    }

    /* --- заголовок-легенда OHLC (левый верх, как на Bybit) --- */
    const chg = legendCandle.close - legendCandle.open;
    const chgPct = (chg / legendCandle.open) * 100;
    const up = chg >= 0;
    const col = up ? BB.up : BB.down;
    ctx.textAlign = 'left';
    let lx = padLeft + 2;
    const ly = 14;
    ctx.font = '11px "IBM Plex Mono", monospace';
    if (meta.symbol) {
        ctx.fillStyle = BB.legendText;
        const sm = `${meta.symbol} · ${meta.interval || ''}`;
        ctx.fillText(sm, lx, ly);
        lx += ctx.measureText(sm + '   ').width;
    }
    const parts = [
        ['O', legendCandle.open], ['H', legendCandle.high],
        ['L', legendCandle.low], ['C', legendCandle.close],
    ];
    for (const [k, v] of parts) {
        ctx.fillStyle = BB.axisText;
        ctx.fillText(k, lx, ly);
        lx += ctx.measureText(k).width + 2;
        ctx.fillStyle = col;
        const tv = fmtPrice(v);
        ctx.fillText(tv, lx, ly);
        lx += ctx.measureText(tv + '  ').width;
    }
    ctx.fillStyle = col;
    ctx.fillText(`${chg >= 0 ? '+' : ''}${fmt(chg, Math.abs(chg) < 1 ? 4 : 2)} (${chgPct >= 0 ? '+' : ''}${fmt(chgPct, 2)}%)`, lx, ly);

    /* объём текущей свечи под легендой */
    ctx.fillStyle = BB.axisText;
    ctx.fillText(`Vol: ${_fmtVol(legendCandle.volume)}`, padLeft + 2, ly + 13);
}

function _rr(ctx, x, y, w, h, r) {
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(x, y, w, h, r);
    else ctx.rect(x, y, w, h);
}

function _fmtTimeShort(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    if (sameDay) return d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
    return d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' }) + ' ' +
        d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}
function _fmtTimeFull(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    return d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

/* Свечной график с маркерами сделок (бэктест) */
function drawCandlesWithTrades(canvas, candles, trades) {
    if (!canvas || !candles || !candles.length) return;
    const ctx = setupCanvas(canvas);
    const W = canvas.clientWidth, H = canvas.clientHeight;
    ctx.clearRect(0, 0, W, H);

    const highs = candles.map(c => c.high);
    const lows = candles.map(c => c.low);
    const min = Math.min(...lows), max = Math.max(...highs);
    const pad = (max - min) * 0.08 || 1;
    const y = v => H - 26 - ((v - (min - pad)) / ((max + pad) - (min - pad))) * (H - 46);
    const n = candles.length;
    const cw = W / n;
    const x = i => i * cw + cw / 2;

    for (let i = 0; i < n; i++) {
        const c = candles[i];
        const up = c.close >= c.open;
        ctx.strokeStyle = up ? 'rgba(46,189,133,.85)' : 'rgba(246,70,93,.85)';
        ctx.fillStyle = up ? 'rgba(46,189,133,.6)' : 'rgba(246,70,93,.6)';
        ctx.beginPath();
        ctx.moveTo(x(i), y(c.high));
        ctx.lineTo(x(i), y(c.low));
        ctx.stroke();
        const bodyTop = y(Math.max(c.open, c.close));
        const bodyH = Math.max(1, y(Math.min(c.open, c.close)) - bodyTop);
        ctx.fillRect(x(i) - cw * 0.28, bodyTop, cw * 0.56, bodyH);
    }

    (trades || []).forEach(t => {
        const ei = t.chart_entry_index;
        const xi = t.chart_exit_index;
        if (ei == null || ei < 0 || ei >= n) return;
        const isLong = t.side === 'LONG';
        const color = t.pnl_pct >= 0 ? BB.up : BB.down;

        const ex = x(ei), ey = y(t.entry);
        ctx.fillStyle = isLong ? BB.up : BB.down;
        ctx.beginPath();
        if (isLong) { ctx.moveTo(ex, ey - 7); ctx.lineTo(ex - 5, ey + 3); ctx.lineTo(ex + 5, ey + 3); }
        else { ctx.moveTo(ex, ey + 7); ctx.lineTo(ex - 5, ey - 3); ctx.lineTo(ex + 5, ey - 3); }
        ctx.closePath();
        ctx.fill();

        if (xi != null && xi >= 0 && xi < n) {
            const xx = x(xi), xy = y(t.exit);
            ctx.strokeStyle = color;
            ctx.lineWidth = 1.6;
            ctx.beginPath();
            ctx.moveTo(xx - 4, xy - 4); ctx.lineTo(xx + 4, xy + 4);
            ctx.moveTo(xx + 4, xy - 4); ctx.lineTo(xx - 4, xy + 4);
            ctx.stroke();
            ctx.setLineDash([3, 3]);
            ctx.globalAlpha = 0.55;
            ctx.beginPath(); ctx.moveTo(ex, ey); ctx.lineTo(xx, xy); ctx.stroke();
            ctx.setLineDash([]);
            ctx.globalAlpha = 1;
        }
    });

    ctx.fillStyle = BB.axisText;
    ctx.font = '11px monospace';
    ctx.fillText(fmtPrice(max + pad), 6, 14);
    ctx.fillText(fmtPrice(min - pad), 6, H - 30);
    ctx.fillStyle = BB.up;
    ctx.fillText('▲ вход LONG', 10, H - 12);
    ctx.fillStyle = BB.down;
    ctx.fillText('▼ вход SHORT · × выход', 130, H - 12);
}

/* Equity-кривая бэктеста */
function drawEquity(canvas, curve) {
    if (!canvas || !curve || !curve.length) return;
    const ctx = setupCanvas(canvas);
    const W = canvas.clientWidth, H = canvas.clientHeight;
    ctx.clearRect(0, 0, W, H);

    const vals = curve.map(p => p.equity);
    const min = Math.min(...vals), max = Math.max(...vals);
    const pad = (max - min) * 0.1 || 1;
    const x = i => (i / (curve.length - 1)) * (W - 10) + 5;
    const y = v => H - 12 - ((v - (min - pad)) / ((max + pad) - (min - pad))) * (H - 30);

    ctx.strokeStyle = '#1f2937';
    ctx.fillStyle = '#9aa7b8';
    ctx.font = '10px monospace';
    for (let g = 0; g <= 4; g++) {
        const gy = H - 12 - (g / 4) * (H - 30);
        ctx.beginPath(); ctx.moveTo(5, gy); ctx.lineTo(W - 5, gy); ctx.stroke();
        const val = min - pad + (g / 4) * ((max + pad) - (min - pad));
        ctx.fillText(fmt(val), 8, gy - 3);
    }

    ctx.strokeStyle = '#4f8cff';
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    curve.forEach((p, i) => { i === 0 ? ctx.moveTo(x(i), y(p.equity)) : ctx.lineTo(x(i), y(p.equity)); });
    ctx.stroke();

    ctx.lineTo(x(curve.length - 1), H - 12);
    ctx.lineTo(x(0), H - 12);
    ctx.closePath();
    ctx.fillStyle = 'rgba(79,140,255,0.12)';
    ctx.fill();
}
