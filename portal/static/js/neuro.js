/* ==========================================================================
   Neuro-field — живой фон «извилины мозга»: дрейфующие узлы, синапсы и
   пробегающие по связям нервные импульсы. Реагирует на курсор и события
   (сигналы подсвечивают сеть лонг/шорт-всплеском).
   ========================================================================== */
'use strict';
(function () {
    const canvas = document.getElementById('neuro-bg');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    let W = 0, H = 0, nodes = [], pulses = [], sparks = [];
    let mouse = { x: -9999, y: -9999 };
    let running = true, raf = null;
    const DPR = Math.min(window.devicePixelRatio || 1, 1.5);

    const COLORS = [
        [139, 92, 246],   // фиолет
        [34, 211, 238],   // циан
        [124, 77, 255],   // глубокий фиолет
    ];
    let flashUntil = 0, flashColor = null;

    function resize() {
        W = canvas.width = Math.floor(innerWidth * DPR);
        H = canvas.height = Math.floor(innerHeight * DPR);
        canvas.style.width = innerWidth + 'px';
        canvas.style.height = innerHeight + 'px';
        seed();
    }

    function seed() {
        const target = Math.max(36, Math.min(110, Math.floor(W * H / (26000 * DPR * DPR))));
        nodes = [];
        for (let i = 0; i < target; i++) {
            nodes.push({
                x: Math.random() * W, y: Math.random() * H,
                vx: (Math.random() - 0.5) * 0.16 * DPR, vy: (Math.random() - 0.5) * 0.16 * DPR,
                r: (Math.random() * 1.6 + 1.1) * DPR,
                c: COLORS[i % COLORS.length],
                tw: Math.random() * Math.PI * 2,       // фаза мерцания
            });
        }
        pulses = []; sparks = [];
    }

    const LINK = 150 * DPR;

    function step(t) {
        ctx.clearRect(0, 0, W, H);

        // дрейф узлов + мягкое заворачивание за края
        for (const n of nodes) {
            n.x += n.vx; n.y += n.vy; n.tw += 0.02;
            if (n.x < -30) n.x = W + 30; if (n.x > W + 30) n.x = -30;
            if (n.y < -30) n.y = H + 30; if (n.y > H + 30) n.y = -30;
            // лёгкое притяжение к курсору
            const dxm = mouse.x - n.x, dym = mouse.y - n.y;
            const dm = Math.hypot(dxm, dym);
            if (dm < 190 * DPR && dm > 4) { n.x += dxm / dm * 0.05 * DPR; n.y += dym / dm * 0.05 * DPR; }
        }

        // связи
        const flashing = performance.now() < flashUntil;
        for (let i = 0; i < nodes.length; i++) {
            const a = nodes[i];
            for (let j = i + 1; j < nodes.length; j++) {
                const b = nodes[j];
                const dx = a.x - b.x, dy = a.y - b.y;
                const d2 = dx * dx + dy * dy;
                if (d2 > LINK * LINK) continue;
                const d = Math.sqrt(d2);
                let alpha = (1 - d / LINK) * 0.16;
                if (flashing) alpha *= 2.2;
                const c = flashColor || a.c;
                ctx.strokeStyle = `rgba(${c[0]},${c[1]},${c[2]},${alpha})`;
                ctx.lineWidth = 1 * DPR;
                ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
            }
        }

        // спавн импульсов по случайным связям
        if (pulses.length < nodes.length * 0.6 && Math.random() < 0.35) {
            const i = (Math.random() * nodes.length) | 0;
            const a = nodes[i];
            // ищем ближайшего соседа
            let best = -1, bd = LINK;
            for (let j = 0; j < nodes.length; j++) {
                if (j === i) continue;
                const d = Math.hypot(a.x - nodes[j].x, a.y - nodes[j].y);
                if (d < bd) { bd = d; best = j; }
            }
            if (best >= 0) {
                pulses.push({ a: i, b: best, t: 0, sp: 0.010 + Math.random() * 0.02, c: flashColor || a.c });
            }
        }

        // отрисовка импульсов
        for (let i = pulses.length - 1; i >= 0; i--) {
            const p = pulses[i];
            p.t += p.sp;
            if (p.t >= 1) { pulses.splice(i, 1); continue; }
            const A = nodes[p.a], B = nodes[p.b];
            if (!A || !B) { pulses.splice(i, 1); continue; }
            const x = A.x + (B.x - A.x) * p.t, y = A.y + (B.y - A.y) * p.t;
            // «хвост» импульса
            const t0 = Math.max(0, p.t - 0.14);
            const x0 = A.x + (B.x - A.x) * t0, y0 = A.y + (B.y - A.y) * t0;
            const grad = ctx.createLinearGradient(x0, y0, x, y);
            grad.addColorStop(0, `rgba(${p.c[0]},${p.c[1]},${p.c[2]},0)`);
            grad.addColorStop(1, `rgba(${p.c[0]},${p.c[1]},${p.c[2]},0.55)`);
            ctx.strokeStyle = grad; ctx.lineWidth = 1.6 * DPR;
            ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x, y); ctx.stroke();
            const glow = 5 * DPR;
            const rg = ctx.createRadialGradient(x, y, 0, x, y, glow);
            rg.addColorStop(0, `rgba(${p.c[0]},${p.c[1]},${p.c[2]},0.9)`);
            rg.addColorStop(1, `rgba(${p.c[0]},${p.c[1]},${p.c[2]},0)`);
            ctx.fillStyle = rg;
            ctx.beginPath(); ctx.arc(x, y, glow, 0, Math.PI * 2); ctx.fill();
        }

        // узлы (мерцают)
        for (const n of nodes) {
            const tw = 0.45 + 0.55 * Math.sin(n.tw);
            const glow = (n.r * 4.2) * (flashing ? 1.6 : 1);
            const c = flashColor || n.c;
            const rg = ctx.createRadialGradient(n.x, n.y, 0, n.x, n.y, glow);
            rg.addColorStop(0, `rgba(${c[0]},${c[1]},${c[2]},${0.5 * tw})`);
            rg.addColorStop(1, `rgba(${c[0]},${c[1]},${c[2]},0)`);
            ctx.fillStyle = rg;
            ctx.beginPath(); ctx.arc(n.x, n.y, glow, 0, Math.PI * 2); ctx.fill();
            ctx.fillStyle = `rgba(${c[0]},${c[1]},${c[2]},${0.75 * tw})`;
            ctx.beginPath(); ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2); ctx.fill();
        }

        // вспышки событий (сигналы)
        if (flashing) {
            const k = (flashUntil - performance.now()) / 1200;
            ctx.fillStyle = `rgba(${flashColor[0]},${flashColor[1]},${flashColor[2]},${0.05 * k})`;
            ctx.fillRect(0, 0, W, H);
        }

        if (running && !reduced) raf = requestAnimationFrame(step);
    }

    function start() { if (!raf && running && !reduced) raf = requestAnimationFrame(step); }
    function stop() { if (raf) { cancelAnimationFrame(raf); raf = null; } }

    addEventListener('resize', resize);
    addEventListener('mousemove', e => { mouse.x = e.clientX * DPR; mouse.y = e.clientY * DPR; });
    addEventListener('mouseout', () => { mouse.x = -9999; mouse.y = -9999; });
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) stop(); else start();
    });

    // публичный хук: вспышка сети при событии (lonG=зелёная, short=красная)
    window.neuroFlash = function (kind) {
        flashColor = kind === 'long' ? [0, 224, 164] : kind === 'short' ? [255, 92, 122] : [139, 92, 246];
        flashUntil = performance.now() + 1200;
    };

    resize();
    if (reduced) {
        // статичный кадр без анимации
        step(0);
        stop();
    } else {
        start();
    }
})();
