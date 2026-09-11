/* Лёгкие canvas-графики (fallback свечной график + equity-кривая бэктеста). */

function setupCanvas(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return ctx;
}

/* Свечной график (fallback, когда TradingView недоступен) */
function drawCandles(canvas, candles) {
    if (!canvas || !candles || !candles.length) return;
    const ctx = setupCanvas(canvas);
    const W = canvas.clientWidth, H = canvas.clientHeight;
    ctx.clearRect(0, 0, W, H);

    const closes = candles.map(c => c.close);
    const highs = candles.map(c => c.high);
    const lows = candles.map(c => c.low);
    const min = Math.min(...lows), max = Math.max(...highs);
    const pad = (max - min) * 0.08 || 1;
    const y = v => H - 20 - ((v - (min - pad)) / ((max + pad) - (min - pad))) * (H - 40);
    const n = candles.length;
    const cw = W / n;

    for (let i = 0; i < n; i++) {
        const c = candles[i];
        const x = i * cw + cw / 2;
        const up = c.close >= c.open;
        ctx.strokeStyle = up ? '#22c55e' : '#ef4444';
        ctx.fillStyle = up ? '#22c55e' : '#ef4444';
        ctx.beginPath();
        ctx.moveTo(x, y(c.high));
        ctx.lineTo(x, y(c.low));
        ctx.stroke();
        const bodyTop = y(Math.max(c.open, c.close));
        const bodyH = Math.max(1, y(Math.min(c.open, c.close)) - bodyTop);
        ctx.fillRect(x - cw * 0.3, bodyTop, cw * 0.6, bodyH);
    }

    // подписи цен
    ctx.fillStyle = '#9aa7b8';
    ctx.font = '11px monospace';
    ctx.fillText(fmtPrice(max + pad), 6, 14);
    ctx.fillText(fmtPrice(min - pad), 6, H - 24);
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
