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
