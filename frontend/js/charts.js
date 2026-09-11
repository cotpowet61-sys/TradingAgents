/* Профессиональные canvas-графики (данные тянутся из Bybit через backend).
   Свечной график: свечи + объём + оси цен/времени + тултип OHLC при наведении. */

function setupCanvas(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
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

/* Полноценный свечной график с объёмом, осями и тултипом */
function drawCandles(canvas, candles) {
    if (!canvas || !candles || !candles.length) return;
    renderCandles(canvas, candles, null);

    if (!canvas._hoverBound) {
        canvas._hoverBound = true;
        canvas.addEventListener('mousemove', e => {
            if (!canvas._lastCandles) return;
            const rect = canvas.getBoundingClientRect();
            renderCandles(canvas, canvas._lastCandles, e.clientX - rect.left);
        });
        canvas.addEventListener('mouseleave', () => {
            if (canvas._lastCandles) renderCandles(canvas, canvas._lastCandles, null);
        });
    }
    canvas._lastCandles = candles;
}

function renderCandles(canvas, candles, mouseX) {
    const ctx = setupCanvas(canvas);
    const W = canvas.clientWidth, H = canvas.clientHeight;
    ctx.clearRect(0, 0, W, H);

    // --- компоновка ---
    const padTop = 14, padRight = 74, padBottom = 26, padLeft = 8;
    const volH = Math.max(50, H * 0.18);           // высота зоны объёма
    const priceBottom = H - padBottom - volH - 6;  // низ ценовой зоны
    const priceH = priceBottom - padTop;

    const highs = candles.map(c => c.high);
    const lows = candles.map(c => c.low);
    const vols = candles.map(c => c.volume);
    const min = Math.min(...lows), max = Math.max(...highs);
    const pad = (max - min) * 0.07 || 1;
    const lo = min - pad, hi = max + pad;
    const y = v => priceBottom - ((v - lo) / (hi - lo)) * priceH;

    const n = candles.length;
    const plotW = W - padLeft - padRight;
    const cw = plotW / n;
    const x = i => padLeft + i * cw + cw / 2;

    // --- горизонтальная сетка + ось цен (справа) ---
    ctx.strokeStyle = 'rgba(255,255,255,0.06)';
    ctx.fillStyle = '#8b98ad';
    ctx.font = '10px monospace';
    ctx.textAlign = 'left';
    for (let g = 0; g <= 5; g++) {
        const val = lo + (g / 5) * (hi - lo);
        const gy = y(val);
        ctx.beginPath(); ctx.moveTo(padLeft, gy); ctx.lineTo(W - padRight, gy); ctx.stroke();
        ctx.fillText(fmtPrice(val), W - padRight + 6, gy + 3);
    }

    // --- свечи ---
    for (let i = 0; i < n; i++) {
        const c = candles[i];
        const up = c.close >= c.open;
        const col = up ? '#22c55e' : '#ef4444';
        ctx.strokeStyle = col;
        ctx.fillStyle = col;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x(i), y(c.high));
        ctx.lineTo(x(i), y(c.low));
        ctx.stroke();
        const bodyTop = y(Math.max(c.open, c.close));
        const bodyH = Math.max(1, y(Math.min(c.open, c.close)) - bodyTop);
        ctx.fillRect(x(i) - cw * 0.32, bodyTop, cw * 0.64, bodyH);
    }

    // --- объём ---
    const vMax = Math.max(...vols) || 1;
    for (let i = 0; i < n; i++) {
        const c = candles[i];
        const up = c.close >= c.open;
        const vh = (c.volume / vMax) * (volH - 6);
        ctx.fillStyle = up ? 'rgba(34,197,94,0.45)' : 'rgba(239,68,68,0.45)';
        ctx.fillRect(x(i) - cw * 0.32, H - padBottom - vh, cw * 0.64, vh);
    }

    // --- ось времени (снизу) ---
    ctx.textAlign = 'center';
    const step = Math.max(1, Math.round(n / 8));
    for (let i = 0; i < n; i += step) {
        ctx.fillText(_fmtTimeShort(candles[i].ts), x(i), H - 8);
    }

    // --- тултип при наведении ---
    if (mouseX != null && mouseX >= padLeft && mouseX <= W - padRight) {
        const idx = Math.round((mouseX - padLeft - cw / 2) / cw);
        if (idx >= 0 && idx < n) {
            const c = candles[idx];
            const cx = x(idx);
            ctx.strokeStyle = 'rgba(255,255,255,0.25)';
            ctx.setLineDash([3, 3]);
            ctx.beginPath(); ctx.moveTo(cx, padTop); ctx.lineTo(cx, H - padBottom); ctx.stroke();
            ctx.setLineDash([]);

            const up = c.close >= c.open;
            const lines = [
                _fmtTimeFull(c.ts),
                `O: ${fmtPrice(c.open)}  H: ${fmtPrice(c.high)}`,
                `L: ${fmtPrice(c.low)}  C: ${fmtPrice(c.close)}`,
                `Vol: ${_fmtVol(c.volume)}`,
                `${up ? '▲ +' : '▼ '}${fmt(((c.close - c.open) / c.open) * 100, 2)}%`,
            ];
            ctx.font = '11px monospace';
            const tw = Math.max(...lines.map(l => ctx.measureText(l).width)) + 16;
            const th = lines.length * 15 + 10;
            let bx = cx + 12;
            if (bx + tw > W) bx = cx - tw - 12;
            const by = padTop + 4;
            ctx.fillStyle = 'rgba(15,20,31,0.95)';
            ctx.strokeStyle = 'rgba(255,255,255,0.15)';
            ctx.beginPath();
            if (ctx.roundRect) ctx.roundRect(bx, by, tw, th, 8);
            else ctx.rect(bx, by, tw, th);
            ctx.fill(); ctx.stroke();
            ctx.fillStyle = '#e6eaf2';
            ctx.textAlign = 'left';
            lines.forEach((l, i) => ctx.fillText(l, bx + 8, by + 16 + i * 15));
        }
    }
}

function _fmtTimeShort(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    return d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}
function _fmtTimeFull(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    return d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

/* Свечной график с маркерами сделок (бэктест: видно как проходили сделки) */
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

    // свечи
    for (let i = 0; i < n; i++) {
        const c = candles[i];
        const up = c.close >= c.open;
        ctx.strokeStyle = up ? 'rgba(52,211,153,.85)' : 'rgba(248,113,113,.85)';
        ctx.fillStyle = up ? 'rgba(52,211,153,.6)' : 'rgba(248,113,113,.6)';
        ctx.beginPath();
        ctx.moveTo(x(i), y(c.high));
        ctx.lineTo(x(i), y(c.low));
        ctx.stroke();
        const bodyTop = y(Math.max(c.open, c.close));
        const bodyH = Math.max(1, y(Math.min(c.open, c.close)) - bodyTop);
        ctx.fillRect(x(i) - cw * 0.28, bodyTop, cw * 0.56, bodyH);
    }

    // маркеры сделок
    (trades || []).forEach(t => {
        const ei = t.chart_entry_index;
        const xi = t.chart_exit_index;
        if (ei == null || ei < 0 || ei >= n) return;
        const isLong = t.side === 'LONG';
        const color = t.pnl_pct >= 0 ? '#34d399' : '#f87171';

        // вход: треугольник
        const ex = x(ei), ey = y(t.entry);
        ctx.fillStyle = isLong ? '#34d399' : '#f87171';
        ctx.beginPath();
        if (isLong) { ctx.moveTo(ex, ey - 7); ctx.lineTo(ex - 5, ey + 3); ctx.lineTo(ex + 5, ey + 3); }
        else { ctx.moveTo(ex, ey + 7); ctx.lineTo(ex - 5, ey - 3); ctx.lineTo(ex + 5, ey - 3); }
        ctx.closePath();
        ctx.fill();

        // выход: крестик (только если в пределах)
        if (xi != null && xi >= 0 && xi < n) {
            const xx = x(xi), xy = y(t.exit);
            ctx.strokeStyle = color;
            ctx.lineWidth = 1.6;
            ctx.beginPath();
            ctx.moveTo(xx - 4, xy - 4); ctx.lineTo(xx + 4, xy + 4);
            ctx.moveTo(xx + 4, xy - 4); ctx.lineTo(xx - 4, xy + 4);
            ctx.stroke();
            // линия от входа к выходу
            ctx.setLineDash([3, 3]);
            ctx.strokeStyle = color;
            ctx.globalAlpha = 0.55;
            ctx.beginPath(); ctx.moveTo(ex, ey); ctx.lineTo(xx, xy); ctx.stroke();
            ctx.setLineDash([]);
            ctx.globalAlpha = 1;
        }
    });

    // подписи цен + легенда
    ctx.fillStyle = '#8b98ad';
    ctx.font = '11px monospace';
    ctx.fillText(fmtPrice(max + pad), 6, 14);
    ctx.fillText(fmtPrice(min - pad), 6, H - 30);
    ctx.fillStyle = '#34d399';
    ctx.fillText('▲ вход LONG', 10, H - 12);
    ctx.fillStyle = '#f87171';
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

    // сетка
    ctx.strokeStyle = '#1f2937';
    ctx.fillStyle = '#9aa7b8';
    ctx.font = '10px monospace';
    for (let g = 0; g <= 4; g++) {
        const gy = H - 12 - (g / 4) * (H - 30);
        ctx.beginPath(); ctx.moveTo(5, gy); ctx.lineTo(W - 5, gy); ctx.stroke();
        const val = min - pad + (g / 4) * ((max + pad) - (min - pad));
        ctx.fillText(fmt(val), 8, gy - 3);
    }

    // линия
    ctx.strokeStyle = '#4f8cff';
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    curve.forEach((p, i) => { i === 0 ? ctx.moveTo(x(i), y(p.equity)) : ctx.lineTo(x(i), y(p.equity)); });
    ctx.stroke();

    // заливка
    ctx.lineTo(x(curve.length - 1), H - 12);
    ctx.lineTo(x(0), H - 12);
    ctx.closePath();
    ctx.fillStyle = 'rgba(79,140,255,0.12)';
    ctx.fill();
}
