#!/usr/bin/env python3
"""Backtest one pair on the recorded book snapshots (logs/ticks): what the
current settings, and a few variants, would have done. Nothing here changes
settings. See entropy_arb/backtest.py for what is and is not modelled.

Бэктест пары по записанным стаканам: текущие настройки и варианты.

Usage:
    python3 tools/backtest.py --symbol OAI --hedge-venue lighter-rh --days 3
    python3 tools/backtest.py ... --compare          # table of variants
    python3 tools/backtest.py ... --upper 6 --lower 6 --no-autonomy
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from dataclasses import replace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb import backtest as bt  # noqa: E402
from entropy_arb import holding  # noqa: E402

DEFAULT_SLIP_BPS = 1.0


def measured_slip(trades_csv: str, symbol: str, venue: str,
                  days: float = 7.0):
    """(average execution loss per trade, bps of notional; trades) from the
    bot's own trades of this pair: plan − fact. None when too few."""
    since = time.time() - days * 86400
    vals = []
    try:
        with open(trades_csv, newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("ok") != "1" or (r.get("symbol") or "") != symbol or \
                        (r.get("hedge_venue") or "") != venue:
                    continue
                try:
                    if float(r["ts"]) < since:
                        continue
                    exp = float(r.get("exp_edge_usd") or 0)
                    fill = float(r.get("fill_edge_usd") or 0)
                    avg = (float(r.get("buy_notional") or 0) +
                           float(r.get("sell_notional") or 0)) / 2
                except (KeyError, ValueError):
                    continue
                if avg > 0 and float(r.get("buy_fill") or 0) > 0 and \
                        float(r.get("sell_fill") or 0) > 0:
                    vals.append((exp - fill) / avg * 1e4)
    except FileNotFoundError:
        pass
    if len(vals) < 10:
        return None, len(vals)
    return max(sum(vals) / len(vals), 0.0), len(vals)


def money(v):
    d = 4 if abs(v) < 1 else 2
    r = round(v, d)
    return "$0" if r == 0 else f"{'+' if r > 0 else '-'}${abs(r):,.{d}f}"


def summary(title, r: bt.Result):
    h = r.hold()
    return [title, str(r.trades), f"${r.e_volume:,.0f}", money(r.pnl),
            f"{r.pnl_bps:+.1f}" if r.pnl_bps is not None else "—",
            f"{r.hours_at_cap:.1f} из {r.hours:.0f}",
            holding.dur(h.median())]


def table(header, rows):
    w = [max(len(str(x)) for x in col) for col in zip(header, *rows)]
    line = lambda r: "  ".join(str(x).ljust(n) for x, n in zip(r, w))  # noqa
    return [line(header), line(["─" * n for n in w])] + [line(r) for r in rows]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--hedge-venue", required=True)
    ap.add_argument("--ticks-dir", default="")
    ap.add_argument("--days", type=float, default=3.0, help="0 = all ticks")
    ap.add_argument("--slip-bps", type=float, default=None,
                    help="execution loss per trade; default: measured")
    ap.add_argument("--compare", action="store_true")
    for k in ("midline", "upper", "lower", "cap", "order"):
        ap.add_argument(f"--{k}", type=float, default=None)
    ap.add_argument("--no-autonomy", action="store_true")
    ap.add_argument("--mode", default="")
    a = ap.parse_args()

    from entropy_arb.config import ConfigError, load_config
    try:
        cfg = load_config(a.config, a.env, symbol=a.symbol,
                          hedge_venue=a.hedge_venue)
    except ConfigError as e:
        print(f"Настройки пары не читаются: {e}")
        return 1
    p = bt.params_from_config(cfg)
    over = {"midline_bps": a.midline, "upper_bps": a.upper,
            "lower_bps": a.lower, "cap_usd": a.cap, "order_usd": a.order}
    p = replace(p, **{k: v for k, v in over.items() if v is not None})
    if a.no_autonomy:
        p = replace(p, auto=replace(p.auto, enabled=False))
    if a.mode:
        p = replace(p, mode=a.mode)
    if a.slip_bps is not None:
        slip, slip_note = a.slip_bps, "задано вручную"
    else:
        slip, n = measured_slip(cfg.trades_csv, a.symbol, a.hedge_venue)
        if slip is None:
            slip = DEFAULT_SLIP_BPS
            slip_note = f"сделок пары мало ({n}), взято {DEFAULT_SLIP_BPS:g}"
        else:
            slip_note = f"по {n} сделкам пары за 7 дней"
    p = replace(p, slip_bps=round(slip, 2))

    ticks_dir = a.ticks_dir or cfg.ticks_dir
    ticks = bt.load_ticks(ticks_dir, a.hedge_venue, a.symbol, a.days)
    out = [f"MONEY CLUB · бэктест · {a.symbol} · Entropy ↔ {a.hedge_venue}"]
    if len(ticks) < 100:
        out.append(f"Мало записанных стаканов в {ticks_dir} "
                   f"({len(ticks)}). Они пишутся, пока бот работает; "
                   f"запустите бэктест через день-два.")
        print("\n".join(out))
        return 0
    span = (ticks[-1].ts - ticks[0].ts) / 3600
    out.append(f"данные: {time.strftime('%Y-%m-%d %H:%M', time.gmtime(ticks[0].ts))}"
               f" … {time.strftime('%Y-%m-%d %H:%M', time.gmtime(ticks[-1].ts))}"
               f" UTC ({span:.0f} ч, {len(ticks)} срезов)")
    out.append(f"настройки: центр {p.midline_bps:g}, полоса +{p.upper_bps:g}/"
               f"−{p.lower_bps:g} bps · сделка ${p.order_usd:g} · лимит "
               f"${p.cap_usd:g} · автономность "
               f"{'вкл' if p.auto.enabled else 'выкл'} · стратегия {p.mode}")
    out.append(f"потеря на исполнении: {p.slip_bps:g} bps на сделку "
               f"({slip_note})")
    out.append("")
    head = ["вариант", "сделок", "объём Entropy", "результат", "bps",
            "часов у лимита", "удержание (медиана)"]
    runs = bt.variants(p) if a.compare else [("Текущие настройки", p)]
    rows = [summary(t, bt.run(ticks, vp)) for t, vp in runs]
    out += table(head, rows)
    out.append("")
    out.append("Результат — со всеми комиссиями, потерей на исполнении и "
               "закрытием остатка позиции")
    out.append("в конце периода. bps — от объёма на Entropy. «Часов у лимита» — "
               "сколько позиция стояла")
    out.append("у лимита и новые сделки в ту сторону были невозможны. Это "
               "оценка по срезам стакана")
    out.append("раз в 2 секунды: глубина больше $20 и промахи стратегий 2 и 3 "
               "не моделируются.")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
