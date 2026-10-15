"""
Отчёты: markdown для человека, JSON для портала.

Правило отчёта: рядом с каждой красивой цифрой — её надёжность (число сделок,
нижняя граница интервала Уилсона, экспектанси после издержек) и явная пометка
«достигнуто / не достигнуто». Если 75% не берётся на честных допущениях, отчёт
говорит это прямо и показывает, что именно даёт система вместо этого.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from typing import Optional

import pandas as pd

from .config import ResearchConfig
from .metrics import Stats, compute_stats, group_stats
from .barriers import breakeven_winrate

DISCLAIMER = (
    "Все цифры получены на исторических данных с учётом комиссии тейкера и "
    "проскальзывания, при консервативном разрешении неоднозначных баров "
    "(если в одном баре задеты TP и SL — считается стоп). Прошлые результаты "
    "не гарантируют будущих. Отчёт не является инвестиционной рекомендацией."
)


def fmt_pct(v) -> str:
    return "—" if v is None or v != v else f"{float(v):+.3f}%"


# --------------------------------------------------------------------- #
# Настройки для портала (.env)
# --------------------------------------------------------------------- #
def env_snippet(verdict: dict, bundle_path: Optional[str] = None,
                min_conf: float = 0.55) -> str:
    """Строки для portal/.env по итогам исследования (имена совпадают с portal_config)."""
    gates = verdict.get("gates") or {}
    lines = ["# ---- Сгенерировано исследовательским контуром (research/) ----"]
    tp = verdict.get("tp_pct")
    sl = verdict.get("sl_pct")
    if tp and sl:
        lines += [f"MANUAL_TP_PCT={round(float(tp), 3)}",
                  f"MANUAL_SL_PCT={round(float(sl), 3)}",
                  "RR_BINDING=false"]
    if gates.get("min_conf"):
        min_conf = float(gates["min_conf"])
    lines.append(f"MIN_CONFIDENCE={round(float(min_conf), 3)}")
    if gates.get("stype_blocklist"):
        lines.append(f"STYPE_BLOCKLIST={gates['stype_blocklist']}")
    if gates.get("hours_block"):
        lines.append("HOURS_BLOCK=" + ",".join(str(h) for h in gates["hours_block"]))
    if gates.get("atr_block"):
        lines.append(f"ATR_PCT_BLOCK={gates['atr_block']}")
    if gates.get("vol_season_block"):
        lines.append(f"VOL_SEASON_BLOCK={gates['vol_season_block']}")
    allowed = verdict.get("allowed_strategies") or []
    if allowed:
        lines.append("# Разрешённые (стратегия/сторона) по данным:")
        for a in allowed[:40]:
            lines.append(f"#   {a}")
    if bundle_path:
        lines += [f"CURATED_BUNDLE={bundle_path}",
                  "USE_CURATED=true"]
    return "\n".join(lines)


# --------------------------------------------------------------------- #
# Таблицы
# --------------------------------------------------------------------- #
def _table(rows: list[dict], cols: list[str], headers: Optional[list[str]] = None) -> str:
    if not rows:
        return "_(нет данных)_\n"
    headers = headers or cols
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(cols)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
    return "\n".join(out) + "\n"


def strategy_table(trades: pd.DataFrame, min_n: int = 10) -> tuple[str, list[dict]]:
    """«Кто вообще работает»: по каждой стратегии и стороне — цифры с CI."""
    if trades.empty:
        return "_(нет сделок)_\n", []
    by = group_stats(trades.to_dict("records"),
                     lambda r: f"{r['stype']}/{str(r['side'])[0]}", min_n=min_n)
    rows = []
    for k, s in sorted(by.items(), key=lambda kv: (-kv[1].expectancy)):
        rows.append({"группа": k, "сделок": s.n, "винрейт": f"{s.winrate:.1f}",
                     "ниж.WR": f"{s.wr_lo:.1f}", "экспект": f"{s.expectancy:+.3f}",
                     "PF": "∞" if s.profit_factor is None else f"{s.profit_factor:.2f}",
                     "DD пп": f"{s.max_dd_pp:.1f}", "удерж, мин": f"{s.median_hold_min:.0f}",
                     "таймаут": f"{s.timeout_share:.0f}%"})
    return _table(rows, ["группа", "сделок", "винрейт", "ниж.WR", "экспект", "PF",
                         "DD пп", "удерж, мин", "таймаут"],
                  ["стратегия/сторона", "сделок", "винрейт, %", "ниж. гр. 95%",
                   "экспект, %", "PF", "DD, пп", "медиана удержания", "таймаут"]), rows


def variant_table_md(rows: list[dict]) -> str:
    if not rows:
        return "_(нет данных)_\n"
    shaped = []
    for r in rows:
        shaped.append({
            "вариант": r.get("variant"), "сделок": r.get("n"),
            "винрейт": r.get("winrate"), "ниж.WR": r.get("wr_lo"),
            "экспект": r.get("expectancy"), "PF": r.get("profit_factor"),
            "DD%": r.get("max_dd_frac"), "прошёл": "да" if r.get("ok") else "нет",
        })
    return _table(shaped, ["вариант", "сделок", "винрейт", "ниж.WR", "экспект", "PF", "DD%", "прошёл"],
                  ["вариант TP/SL", "сделок", "винрейт, %", "ниж. гр. 95%",
                   "экспект, %", "PF", "просадка, %", "ограничения"])


# --------------------------------------------------------------------- #
# Главный рендер
# --------------------------------------------------------------------- #
def render_markdown(*, cfg: ResearchConfig, quality: list[dict], trades: pd.DataFrame,
                    variants: Optional[pd.DataFrame] = None,
                    verdicts: list[dict] | None = None,
                    walk_forward: Optional[dict] = None,
                    bundle_report: Optional[dict] = None,
                    mirror_stats: Optional[dict] = None,
                    bundle_path: Optional[str] = None,
                    data_note: str = "") -> str:
    now = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    costs = cfg.costs.total_round_trip_pct()
    L: list[str] = []
    L.append(f"# Отчёт исследовательского контура TradeVision\n")
    L.append(f"_Сформирован: {now}. Издержки в расчёте: {costs:.3f}% на сделку "
             f"(комиссия {cfg.costs.fee_bps:g} б.п. + проскальзывание "
             f"{cfg.costs.slippage_bps:g} б.п. на каждую сторону)._\n")
    if data_note:
        L.append(f"> {data_note}\n")
    L.append(f"> {DISCLAIMER}\n")

    # 1. Резюме
    L.append("## 1. Короткий вывод\n")
    if trades.empty:
        L.append("Сделок нет — проверьте, что данные скачаны и сигналы извлечены.\n")
        return "\n".join(L)
    overall = compute_stats(trades.to_dict("records"))
    be = breakeven_winrate(1.0, 1.0, costs)
    L.append(f"- Сделок в анализе: **{overall.n}**, винрейт **{overall.winrate:.1f}%** "
             f"(нижняя граница 95%: {overall.wr_lo:.1f}%), экспектанси "
             f"**{overall.expectancy:+.3f}%** на сделку, PF "
             f"{'∞' if overall.profit_factor is None else round(overall.profit_factor, 2)}.\n")
    L.append(f"- Максимальная просадка: {overall.max_dd_pp:.1f} п.п. "
             f"(≈{overall.max_dd_frac:.1f}% депозита при риске {2.0:g}% на сделку). "
             f"Медиана удержания: {overall.median_hold_min:.0f} мин. "
             f"Доля таймаутов: {overall.timeout_share:.0f}%.\n")
    if verdicts:
        for v in verdicts:
            t = v.get("test") or {}
            goal = "✅ ЦЕЛЬ ВЫПОЛНЕНА" if v.get("passes_target") else "❌ цель не выполнена"
            L.append(f"- **{v['group']}**: вариант `{v['variant']}`, на тестовой части "
                     f"n={t.get('n')}, винрейт {t.get('winrate')}% "
                     f"(ниж. гр. {t.get('wr_lo')}%), экспектанси {t.get('expectancy')}%. {goal} — {v.get('why')}\n")
    L.append("\n")

    # 2. Данные
    L.append("## 2. Качество данных\n")
    L.append(_table(quality, ["symbol", "tf", "bars", "coverage_pct", "gaps", "dupes", "bad_ohlc", "first", "last"],
                    ["монета", "ТФ", "баров", "покрытие, %", "пропуски", "дубли",
                     "битые OHLC", "начало", "конец"]))
    L.append("\n")

    # 3. Стратегии
    L.append("## 3. Какие сигналы вообще работают (все стратегии × стороны)\n")
    tbl, srows = strategy_table(trades)
    L.append("Цифры после издержек и с консервативным разрешением неоднозначных баров.\n\n")
    L.append(tbl)
    if srows:
        good = [r for r in srows if float(str(r["экспект"]).replace("+", "")) > 0]
        L.append(f"\nВ плюсе по экспектанси: **{len(good)}** из {len(srows)} групп "
                 f"(остальные выведены в стоп-лист кандидатов). Строки с отрицательной "
                 f"экспектанси — прямой кандидат в `STYPE_BLOCKLIST`.\n")

    # 4. Варианты выхода
    L.append("\n## 4. Перебор вариантов выхода (TP/SL, breakeven, трейлинг)\n")
    if variants is not None and not variants.empty:
        top = variants.head(12)
        best_rows = top[
            [c for c in ["variant", "n", "winrate", "wr_lo", "expectancy",
                         "profit_factor", "max_dd_frac", "ok"] if c in top.columns]
        ].to_dict("records")
        L.append(variant_table_md(best_rows))
        hmm = breakeven_winrate(float(best_rows[0].get("expectancy", 0) and 1.0), 1.0, costs)
        L.append(f"\n> Безубыточный винрейт для равных TP/SL при этих издержках ≈ "
                 f"{hmm:.1f}%. Именно поэтому «75%» подаётся вместе с экспектанси: "
                 f"винрейт сам по себе ничего не говорит о прибыли.\n")
    else:
        L.append("_(варианты не считались)_\n")

    # 5. Диагностика
    L.append("\n## 5. Почему цель берётся или не берётся (диагностика)\n")
    if verdicts:
        for v in verdicts:
            d = (v.get("diagnostics") or {}).get("overall") or {}
            if not d:
                continue
            L.append(f"**{v['group']}**\n")
            L.append(f"- Доля сделок, где ход в пользу так и не дошёл до TP: "
                     f"{d.get('miss_share_pct')}% (медианный ход в пользу {d.get('median_mfe_pct')}%, "
                     f"против {d.get('median_mae_pct')}%, заданный TP {d.get('median_tp_pct')}%).\n")
            by_atr = (v.get("diagnostics") or {}).get("by_atr") or []
            if by_atr:
                L.append(_table(by_atr, ["atr_bucket", "n", "tp_pct", "wr", "miss_share_pct",
                                         "median_mfe", "median_mae", "exp"],
                                ["ATR-бакет, %", "сделок", "TP, %", "винрейт, %",
                                 "не дошло до TP, %", "медиана MFE, %", "медиана MAE, %",
                                 "экспект, %"]))
            f = v.get("formula") or {}
            if f:
                L.append(f"- Переносимая формула по волатильности: TP ≈ "
                         f"**{f.get('tp_atr_mult_median')} × ATR%**, SL ≈ "
                         f"**{f.get('sl_atr_mult_median')} × ATR%** "
                         f"(медианный ATR {f.get('atr_median_pct')}%).\n")
            L.append("")

    # 6. Walk-forward
    L.append("\n## 6. Проверка вне выборки (walk-forward)\n")
    folds = (walk_forward or {}).get("folds") or []
    if folds:
        L.append(_table(folds, ["fold", "variant", "train_n", "train_wr", "train_exp",
                                "test_n", "test_wr", "test_wlo", "test_exp", "test_pf"],
                        ["фолд", "вариант", "train: сделок", "train: WR, %",
                         "train: экспект, %", "test: сделок", "test: WR, %",
                         "test: ниж. WR", "test: экспект, %", "test: PF"]))
        L.append("\nПравила подбирались только на train-части; цифры test — вне выборки. "
                 "Если test-винрейт заметно ниже train — правило переобучено.\n")
    else:
        L.append("_(walk-forward не считался)_\n")

    # 7. Мета-модель
    L.append("\n## 7. Мета-модель (ИИ-фильтр сигналов)\n")
    if bundle_report:
        tc = bundle_report.get("threshold_choice") or {}
        L.append(f"- Порог вероятности: {tc.get('threshold')} (охват "
                 f"{round(100 * float(tc.get('coverage') or 0), 1)}% сделок, "
                 f"винрейт на train {tc.get('winrate')}%).\n")
        L.append(f"- Цель по винрейту на пороге: "
                 f"{'достигнута' if tc.get('target_met') else 'НЕ достигнута'} — {tc.get('reason')}.\n")
        tr = (bundle_report.get("train") or {})
        te = (bundle_report.get("test") or {})
        rows = []
        for name, block in (("train", tr), ("test", te)):
            for kind, s in (block or {}).items():
                rows.append({"часть": name, "выборка": "все сигналы" if kind == "all" else "после фильтра ИИ",
                             "сделок": s.get("n"), "винрейт": s.get("winrate"),
                             "ниж.WR": s.get("wr_lo"), "экспект": s.get("expectancy"),
                             "PF": s.get("profit_factor")})
        L.append(_table(rows, ["часть", "выборка", "сделок", "винрейт", "ниж.WR", "экспект", "PF"],
                        ["часть", "выборка", "сделок", "винрейт, %", "ниж. гр. 95%",
                         "экспект, %", "PF"]))
        rel = bundle_report.get("reliability_test") or []
        if rel:
            L.append("\nКалибровка на тестовой части (предсказанная вероятность → фактический винрейт):\n\n")
            L.append(_table(rel, ["p_bucket", "n", "predicted", "actual_winrate"],
                            ["диапазон p", "сделок", "предсказано", "фактический винрейт, %"]))
        coefs = bundle_report.get("coefficients") or []
        if coefs:
            L.append("\nСамые влиятельные признаки: " +
                     ", ".join(f"`{c[0]}` ({c[1]:+.2f})" for c in coefs[:6]) + ".\n")
        if bundle_path:
            L.append(f"\nБандл сохранён: `{bundle_path}` — его подключает портал "
                     f"(`CURATED_BUNDLE` + `USE_CURATED=true`).\n")
    else:
        L.append("_(мета-модель не обучалась)_\n")

    # 8. Зеркальный прогон
    if mirror_stats:
        L.append("\n## 8. Зеркальный прогон движка портала\n")
        L.append("Тот же код, что работает в сканере портала (авто-селектор стратегий, "
                 "фильтры входа, breakeven/трейлинг, кулдаун 30 минут, одна сделка на пару):\n\n")
        L.append(_table([mirror_stats], ["n", "winrate", "wr_lo", "expectancy", "profit_factor",
                                         "max_dd_pp", "median_hold_min", "timeout_share"],
                        ["сделок", "винрейт, %", "ниж. гр. 95%", "экспект, %", "PF",
                         "DD, пп", "медиана удержания, мин", "таймаут, %"]))
        L.append("\nЭто самая честная проверка: цифры нельзя завысить ни выбором TP/SL, "
                 "ни «удобным» разрешением баров — путь сделок считает движок портала.\n")

    L.append("\n---\n")
    L.append("Как воспроизвести: `python research/run.py all --timeframes 15m,1h`. "
             "Подробности архитектуры — `research/ARCHITECTURE.md`, "
             "что скачивать — `research/DOWNLOAD_SPEC.md`.\n")
    return "\n".join(L)


def write_outputs(out_dir: str, markdown: str, payload: dict,
                  bundle: Optional[dict] = None) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    md_path = os.path.join(out_dir, "report.md")
    json_path = os.path.join(out_dir, "recommendation.json")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(markdown)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    paths = {"markdown": md_path, "json": json_path}
    if bundle is not None:
        b_path = os.path.join(out_dir, "bundle.json")
        from .curated import save_bundle
        save_bundle(bundle, b_path)
        paths["bundle"] = b_path
    return paths
