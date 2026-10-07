#!/usr/bin/env python3
"""Detailed report of the bot's own records, for pasting into any AI.

Reads the journals in logs/ (trades.csv, hedges.csv, equity.csv,
sessions.csv, minutes.csv, engine.log) for a period and prints plain text:
where the result came from (expected edge, slippage per leg, fees,
partial fills), and how it depends on direction, hour, leg latency and the
market premium. Nothing here changes settings.

Подробный отчёт по записям бота за период — для копирования в любой ИИ.

Usage:
    python3 tools/report.py --hours 24          # 0 = all time
    python3 tools/report.py --hours 0 --summary # short block (calibration)
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import re
import statistics as st
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

MAX_TRADE_LINES = 150
LOG_TAIL_BYTES = 8 * 1024 * 1024


# ------------------------------------------------------------------ helpers

def num(x):
    try:
        if x is None or x == "":
            return None
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def read_csv(path):
    try:
        with open(path, newline="") as fh:
            return list(csv.DictReader(fh))
    except FileNotFoundError:
        return []


def in_period(rows, key, cutoff):
    out = []
    for r in rows:
        t = num(r.get(key))
        if t is not None and t >= cutoff:
            out.append(r)
    return out


def utc(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts)) if ts else "—"


def pct(vals, q):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * q / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] if lo == hi else v[lo] * (hi - k) + v[hi] * (k - lo)


def f(x, nd=4, sign=False):
    if x is None:
        return "—"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def mean(vals):
    v = [x for x in vals if x is not None]
    return sum(v) / len(v) if v else None


def table(header, rows):
    w = [len(h) for h in header]
    srows = [[str(c) for c in r] for r in rows]
    for r in srows:
        for i, c in enumerate(r):
            w[i] = max(w[i], len(c))
    out = ["  ".join(h.ljust(w[i]) for i, h in enumerate(header))]
    for r in srows:
        out.append("  ".join(c.rjust(w[i]) if i else c.ljust(w[i])
                             for i, c in enumerate(r)))
    return out


# -------------------------------------------------------------- trade math

class T:
    """One trades.csv row with derived numbers (None where unknown)."""

    def __init__(self, r, entropy_fee_now):
        self.r = r
        self.ts = num(r.get("ts")) or 0.0
        self.dir = r.get("direction", "")
        self.ok = r.get("ok") == "1"
        self.qty = num(r.get("qty")) or 0.0
        self.bfill = num(r.get("buy_fill")) or 0.0
        self.sfill = num(r.get("sell_fill")) or 0.0
        self.bnot = num(r.get("buy_notional")) or 0.0
        self.snot = num(r.get("sell_notional")) or 0.0
        self.exp = num(r.get("exp_edge_usd")) or 0.0
        self.fill_rec = num(r.get("fill_edge_usd")) or 0.0
        self.prem = num(r.get("marginal_premium_bps"))
        self.status = f"{r.get('buy_status', '')}/{r.get('sell_status', '')}"
        self.bpx = num(r.get("buy_avg_px"))
        self.spx = num(r.get("sell_avg_px"))
        self.bexp = num(r.get("buy_exp_px"))
        self.sexp = num(r.get("sell_exp_px"))
        self.bfee = num(r.get("buy_fee_bps"))
        self.sfee = num(r.get("sell_fee_bps"))
        self.bms = num(r.get("buy_ms"))
        self.sms = num(r.get("sell_ms"))
        self.gap = num(r.get("leg_gap_ms"))
        self.new = self.bfee is not None          # MONEY CLUB journal row
        self.ent_is_buy = r.get("buy_venue", "").upper() == "ENTROPY"
        # filled notional (turnover) — exact for new rows, planned for old
        if self.new and self.bpx and self.spx:
            self.turnover = self.bfill * self.bpx + self.sfill * self.spx
        else:
            fr_b = self.bfill / self.qty if self.qty else 0.0
            fr_s = self.sfill / self.qty if self.qty else 0.0
            self.turnover = self.bnot * fr_b + self.snot * fr_s
        # fees: recorded rates for new rows; old rows were written with the
        # fee config of the time (0 on Entropy for most history), so the
        # Entropy leg is re-charged at today's rate and reported separately
        if self.new:
            self.fee_missing = 0.0
        else:
            ent_notional = (self.bnot * (self.bfill / self.qty if self.qty
                                         else 0) if self.ent_is_buy else
                            self.snot * (self.sfill / self.qty if self.qty
                                         else 0))
            self.fee_missing = ent_notional * entropy_fee_now / 1e4
        self.fill_adj = self.fill_rec - self.fee_missing
        # slippage per leg (new rows): positive = cost
        self.bslip = ((self.bpx - self.bexp) * self.bfill
                      if self.bpx and self.bexp else None)
        self.sslip = ((self.sexp - self.spx) * self.sfill
                      if self.spx and self.sexp else None)
        self.bslip_bps = ((self.bpx / self.bexp - 1) * 1e4
                          if self.bpx and self.bexp else None)
        self.sslip_bps = ((1 - self.spx / self.sexp) * 1e4
                          if self.spx and self.sexp else None)
        self.partial = abs(self.bfill - self.sfill) > 1e-12
        avg_not = (self.bnot + self.snot) / 2 or None
        self.fill_bps = self.fill_adj / avg_not * 1e4 if avg_not else None
        self.exp_bps = self.exp / avg_not * 1e4 if avg_not else None


# -------------------------------------------------------------------- report

def load_settings(path):
    if yaml is None:
        return {}
    try:
        with open(path) as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return {}


def apply_ticker_file(cfg, path):
    """Overlay tickers/<TICKER>.yaml on config.yaml (same rule as the bot:
    thresholds only from the ticker file)."""
    if not path:
        return cfg
    prof = load_settings(path)
    if not prof:
        return cfg
    try:
        from entropy_arb.config import merge_ticker_profile
        return merge_ticker_profile(cfg, prof)
    except Exception:
        return cfg


def for_symbol(rows, symbol, legacy_symbol, venue="", legacy_venue=""):
    """Rows of one pair (ticker + hedge venue). Rows written before ticker
    selection have no symbol: they belong to `legacy_symbol`; rows written
    before venue selection have no hedge_venue: they belong to
    `legacy_venue` (the only ones traded then)."""
    if not symbol and not venue:
        return rows
    out = []
    for r in rows:
        sym = (r.get("symbol") or "").strip() or legacy_symbol
        ven = (r.get("hedge_venue") or "").strip() or legacy_venue
        if symbol and sym != symbol:
            continue
        if venue and ven != venue:
            continue
        out.append(r)
    return out


def exec_loss_bps(trades):
    """(average execution loss per trade in bps, trades counted) — what was
    expected at decision time minus what the fills gave, over trades where
    both legs filled. None when there are no such trades."""
    ok = [t for t in trades if t.ok and t.bfill > 0 and t.sfill > 0]
    exp_bps = mean([t.exp_bps for t in ok]) if ok else None
    got_bps = mean([t.fill_bps for t in ok]) if ok else None
    if exp_bps is None or got_bps is None:
        return None, 0
    return exp_bps - got_bps, len(ok)


def key_metrics(trades, hedges):
    """The few numbers that say where the money goes (Russian lines)."""
    out = []
    ok = [t for t in trades if t.ok and t.bfill > 0 and t.sfill > 0]
    if ok:
        exp_bps = mean([t.exp_bps for t in ok])
        got_bps = mean([t.fill_bps for t in ok])
        if exp_bps is not None and got_bps is not None:
            out.append(f"проскальзывание: ожидалось {exp_bps:+.2f} bps на "
                       f"сделку, получено {got_bps:+.2f} bps — потеря на "
                       f"исполнении {exp_bps - got_bps:.2f} bps на сделку "
                       f"(${sum(t.exp for t in ok) - sum(t.fill_adj for t in ok):.4f}"
                       f" за {len(ok)} сделок)")
    one_leg = [t for t in trades if (t.bfill > 0) != (t.sfill > 0)]
    partial = [t for t in trades if t.bfill > 0 and t.sfill > 0 and t.partial]
    n = len(trades)
    if n:
        out.append(f"сделок, где исполнилась только одна нога: {len(one_leg)} "
                   f"из {n}" + (f"; частично: {len(partial)}" if partial
                                else ""))
    hed = [h for h in hedges if (h.get("reason") or "") == "hedge"]
    rl = [h for h in hedges
          if (h.get("err") or "").startswith("RATE_LIMITED")]
    rl_trades = [t for t in trades if "send-failed" in t.status]
    if n or hed:
        out.append(f"выравниваний позиции (хеджей): {len(hed)}"
                   + (f" — {len(hed) / n:.2f} на сделку" if n else ""))
    out.append(f"ноги не отправлены (send-failed, чаще всего лимит запросов): "
               f"{len(rl_trades)} в сделках, {len(rl)} в выравниваниях")
    return out


def fund_cell(s):
    """Session funding per leg, "Entropy/hedge" (already inside pnl$)."""
    fe, fh = num(s.get("funding_entropy_usd")), num(s.get("funding_hedge_usd"))
    if fe is None and fh is None:
        return "—"
    return f"{f(fe, 4, True)}/{f(fh, 4, True)}"


def diag_lines(diag, cfg):
    """Execution diagnostics (logs/exec_diag.csv): for every signal the bot
    acted on — how old each book was, how long the signal had lasted, and
    where the premium and the Entropy price were 1 s and 3 s later. Answers
    the key question: is the premium the bot sees still there when its
    order lands, or does it vanish (a stale book)?"""
    out = []
    w = out.append
    w("== Диагностика сигналов (logs/exec_diag.csv) ==")
    mode = g(cfg, "execution", "mode", default="simultaneous")
    w(f"режим исполнения: {mode} · предел цены Entropy в режиме "
      f"entropy_first: {g(cfg, 'execution', 'entropy_first_slip_bps', default=5.0)}"
      f" bps · потолок сигнала: "
      f"{g(cfg, 'execution', 'max_excess_bps', default=0) or 'выкл'}"
      f" · учёт проскальзывания: "
      f"{'вкл' if g(cfg, 'slipgate', 'enabled', default=False) else 'выкл'}")
    if not diag:
        w("— нет данных: файл появляется после обновления бота, по строке на "
          "каждый сигнал")
        w("")
        return out
    by_out = defaultdict(int)
    for r in diag:
        by_out[r.get("outcome") or "?"] += 1
    w("сигналов: " + str(len(diag)) + " · исходы: "
      + ", ".join(f"{k} {v}" for k, v in sorted(by_out.items())))
    tried = [r for r in diag if r.get("mode") in ("entropy_first", "volume")]
    if tried:
        miss = sum(1 for r in tried if r.get("outcome") == "missed")
        w(f"режим «сначала Entropy»: промахов {miss} из {len(tried)} "
          f"({miss / len(tried) * 100:.0f}%) — промах ничего не стоит, но "
          f"тратит запрос Hyperliquid")

    def med(rows, key):
        v = [num(r.get(key)) for r in rows]
        v = sorted(x for x in v if x is not None)
        return v[len(v) // 2] if v else None
    w("возраст стакана в момент сигнала, медиана: Entropy "
      f"{f(med(diag, 'e_age_ms'), 0)} мс · хедж {f(med(diag, 'h_age_ms'), 0)} "
      f"мс · сигнал держался {f(med(diag, 'signal_age_ms'), 0)} мс")
    w("ноги: Entropy " + f(med(diag, "e_ms"), 0) + " мс · хедж "
      + f(med(diag, "h_ms"), 0) + " мс")
    rows = []
    for d in ("sell_entropy", "buy_entropy"):
        ds = [r for r in diag if r.get("direction") == d]
        if not ds:
            continue
        sgn = 1 if d == "sell_entropy" else -1   # >0 = premium moved back
        fades1, fades3 = [], []
        for r in ds:
            p0, p1, p3 = (num(r.get("mid_prem0_bps")), num(r.get(
                "mid_prem1_bps")), num(r.get("mid_prem3_bps")))
            if p0 is not None and p1 is not None:
                fades1.append(sgn * (p0 - p1))
            if p0 is not None and p3 is not None:
                fades3.append(sgn * (p0 - p3))
        rows.append([d, len(ds), f(med(ds, "mid_prem0_bps"), 2, True),
                     f(pct(fades1, 50), 2, True), f(pct(fades3, 50), 2, True),
                     f(med(ds, "e_slip_bps"), 2, True),
                     f(med(ds, "e_mark1_bps"), 2, True),
                     f(med(ds, "e_mark3_bps"), 2, True)])
    out += table(["direction", "n", "prem0", "ушло_1с", "ушло_3с",
                  "slipE", "markE_1с", "markE_3с"], rows)
    w("prem0 — премия (mid) в момент сигнала; ушло_1с/3с — насколько она "
      "вернулась назад за 1 и 3 с (больше 0 = всплеск исчезает); slipE — "
      "проскальзывание ноги Entropy; markE — куда цена Entropy ушла после "
      "сделки (больше 0 = в нашу пользу, меньше 0 = мы купили/продали на "
      "всплеске). Если «ушло» близко к выгоде сигнала, сигналы в основном "
      "призрачные: помогает режим «сначала Entropy» и потолок сигнала.")
    w("")
    return out


def g(d, *keys, default=None):
    for k in keys:
        d = (d or {}).get(k)
    return default if d is None else d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="logs")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--summary", action="store_true",
                    help="short block of the bot's trades (for calibration)")
    ap.add_argument("--exec-loss", action="store_true",
                    help="print only 'EXEC_LOSS <bps> <trades>' (or "
                         "'EXEC_LOSS none 0') for the menu's calibration")
    ap.add_argument("--symbol", default="",
                    help="only this ticker's rows (empty = everything)")
    ap.add_argument("--legacy-symbol", default="SNDK",
                    help="ticker of old rows that have no symbol column")
    ap.add_argument("--hedge-venue", default="",
                    help="only this hedge venue's rows (empty = all)")
    ap.add_argument("--legacy-venue", default="lighter-rh",
                    help="hedge venue of old rows that have no venue column")
    ap.add_argument("--ticker-file", default="",
                    help="tickers/<TICKER>.yaml to show that ticker's settings")
    ap.add_argument("--minutes", default="",
                    help="minute-data CSV (default: <dir>/minutes.csv)")
    a = ap.parse_args()

    now = time.time()
    cutoff = now - a.hours * 3600 if a.hours > 0 else 0.0
    cfg = apply_ticker_file(load_settings(a.config), a.ticker_file)
    fee_ent = float(g(cfg, "entropy", "taker_fee_bps", default=0.0))
    fee_hdg = float(g(cfg, "hedge", "taker_fee_bps", default=0.0))
    p = lambda n: os.path.join(a.dir, n)  # noqa: E731

    sym, leg = a.symbol.strip(), a.legacy_symbol.strip()
    ven, lven = a.hedge_venue.strip(), a.legacy_venue.strip()
    pick = lambda name: for_symbol(read_csv(p(name)), sym, leg,  # noqa: E731
                                   ven, lven)
    trades = [T(r, fee_ent) for r in in_period(pick("trades.csv"), "ts",
                                               cutoff)]
    hedges = in_period(pick("hedges.csv"), "ts", cutoff)
    if a.exec_loss:
        loss, n = exec_loss_bps(trades)
        print(f"EXEC_LOSS {loss:.4f} {n}" if loss is not None
              else "EXEC_LOSS none 0")
        return
    equity = in_period(pick("equity.csv"), "ts", cutoff)
    sessions = in_period(pick("sessions.csv"), "end_ts", cutoff)
    minutes = in_period(read_csv(a.minutes or p("minutes.csv")),
                        "minute_ts", cutoff)
    period = "всё время" if a.hours <= 0 else f"последние {a.hours:g} ч"

    out = []
    w = out.append
    ok = [t for t in trades if t.ok]
    new = [t for t in trades if t.new]
    old = [t for t in trades if not t.new]

    if a.summary:
        w(f"Сделки бота за период: {len(trades)} (успешных {len(ok)}), "
          f"оборот ${sum(t.turnover for t in trades):,.2f}")
        w(f"  ожидалось ${sum(t.exp for t in trades):+.4f} · по факту с "
          f"комиссией ${sum(t.fill_adj for t in trades):+.4f}"
          + (f" (в {len(old)} старых сделках комиссия Entropy досчитана по "
             f"{fee_ent:g} bps)" if old else ""))
        for line in key_metrics(trades, hedges):
            w("  " + line)
        print("\n".join(out))
        return

    # 0. header & settings
    w(f"MONEY CLUB · подробный отчёт · {period} · сформирован "
      f"{utc(now)} UTC")
    w("Арбитраж: одна нога на Entropy (Hyperliquid), вторая на хедж-бирже; "
      "бот всегда торгует тейкером.")
    w("premium_bps = (цена Entropy / цена хеджа − 1)·10000. Вход SELL "
      "entropy при премии ≥ midline+upper(+комиссии), BUY entropy при ≤ "
      "midline−lower(−комиссии).")
    w("")
    w("== Настройки сейчас ==")
    w(f"thresholds: midline {g(cfg, 'thresholds', 'midline_bps')} · upper "
      f"{g(cfg, 'thresholds', 'upper_bps')} · lower "
      f"{g(cfg, 'thresholds', 'lower_bps')} bps")
    w(f"fees: entropy {fee_ent:g} bps · hedge {fee_hdg:g} bps · position "
      f"cap ${g(cfg, 'entropy', 'max_position_usd', default='—')} · order ≤ "
      f"${g(cfg, 'sizing', 'max_order_notional_usd', default='—')} · "
      f"take_fraction "
      f"{g(cfg, 'sizing', 'take_fraction', default=0.5)}")
    w(f"execution: persist {g(cfg, 'execution', 'premium_persist_sec', default=0.3)}s"
      f" · leg_slippage {g(cfg, 'execution', 'leg_slippage_bps', default=50)} bps"
      f" · hedge_slippage {g(cfg, 'execution', 'hedge_slippage_bps', default=20)} bps"
      f" · loss limit {g(cfg, 'risk', 'max_loss_pct', default=0)}% per session")
    mode = g(cfg, "execution", "mode", default="simultaneous")
    try:
        from entropy_arb.strategy import strategy_title
        mode_t = strategy_title(mode)
    except Exception:
        mode_t = mode
    w(f"strategy: {mode_t} ({mode})"
      + (f" · worst Entropy price "
         f"{g(cfg, 'execution', 'entropy_first_slip_bps', default=5)} bps"
         if mode in ("entropy_first", "volume") else "")
      + (f" · band −{g(cfg, 'execution', 'volume_narrow_bps', default=1)} bps"
         f" · price of volume ≤ $"
         f"{g(cfg, 'execution', 'volume_max_cost_usd', default=2)} per $10k"
         if mode == "volume" else "")
      + f" · protection: execution-loss correction "
        f"{'on' if g(cfg, 'slipgate', 'enabled', default=False) else 'off'}, "
        f"skip big signals "
        f"{g(cfg, 'execution', 'max_excess_bps', default=0) or 'off'}")
    w("")

    w("== Главное: куда уходят деньги ==")
    for line in key_metrics(trades, hedges):
        w(line)
    w("")

    # 1. coverage
    span = [t.ts for t in trades]
    w("== Покрытие данных ==")
    w(f"сделок: {len(trades)} (с ценами исполнения, комиссией и задержкой: "
      f"{len(new)}; старый формат без этих полей: {len(old)})")
    if span:
        w(f"первая/последняя сделка: {utc(min(span))} / {utc(max(span))} UTC")
    w(f"выравниваний/закрытий: {len(hedges)} · сессий: {len(sessions)} · "
      f"замеров баланса: {len(equity)} · минут рынка: {len(minutes)}")
    if old:
        w(f"! В старых строках fill_edge посчитан с комиссией из конфига того "
          f"времени (для большей части истории 0). Ниже комиссия Entropy "
          f"для них досчитана по {fee_ent:g} bps и показана отдельно.")
    w("")

    # 2. totals
    w("== Итог сделок ==")
    turn = sum(t.turnover for t in trades)
    s_exp = sum(t.exp for t in trades)
    s_rec = sum(t.fill_rec for t in trades)
    s_miss = sum(t.fee_missing for t in trades)
    s_adj = s_rec - s_miss
    w(f"успешных {len(ok)} из {len(trades)} · оборот (обе ноги) "
      f"${turn:,.2f}")
    w(f"ожидаемый доход по плану (exp_edge, уже с комиссией плана): "
      f"${s_exp:+.4f}")
    w(f"фактический доход по сделкам (fill_edge как записан): ${s_rec:+.4f}")
    if s_miss:
        w(f"досчитанная комиссия Entropy старых строк: −${s_miss:.4f}")
    w(f"фактический с полной комиссией: ${s_adj:+.4f} · "
      f"на оборот {f(s_adj / turn * 1e4 if turn else None, 2, True)} bps")
    w(f"расхождение факт − план: ${s_adj - s_exp:+.4f} "
      f"({f((s_adj - s_exp) / turn * 1e4 if turn else None, 2, True)} bps "
      f"от оборота)")
    w(f"средняя сделка: план {f(mean([t.exp_bps for t in trades]), 2, True)}"
      f" bps · факт {f(mean([t.fill_bps for t in trades]), 2, True)} bps")
    w("")

    # 3. decomposition (new rows)
    w("== Откуда расхождение (только сделки нового формата) ==")
    if not new:
        w("нет сделок нового формата за период — данные начнут копиться "
          "после обновления бота.")
    else:
        e = sum(t.exp for t in new)
        fa = sum(t.fill_adj for t in new)
        bs = [t.bslip for t in new if t.bslip is not None]
        ss = [t.sslip for t in new if t.sslip is not None]
        fees = sum((t.bfill * (t.bpx or 0) * (t.bfee or 0) +
                    t.sfill * (t.spx or 0) * (t.sfee or 0)) / 1e4
                   for t in new)
        part = [t for t in new if t.partial]
        w(f"сделок {len(new)}: план ${e:+.4f} → факт ${fa:+.4f} "
          f"(разница ${fa - e:+.4f})")
        w(f"проскальзывание ноги покупки: ${sum(bs):+.4f} (стоимость; "
          f"среднее {f(mean([t.bslip_bps for t in new]), 2, True)} bps, "
          f"p90 {f(pct([t.bslip_bps for t in new], 90), 2, True)})")
        w(f"проскальзывание ноги продажи: ${sum(ss):+.4f} (стоимость; "
          f"среднее {f(mean([t.sslip_bps for t in new]), 2, True)} bps, "
          f"p90 {f(pct([t.sslip_bps for t in new], 90), 2, True)})")
        w(f"комиссии по факту исполнения: ${fees:.4f} (по ставкам в момент "
          f"сделки — примерно, не данные биржи)")
        w(f"неполные исполнения (ноги разного объёма): {len(part)}")
        w(f"остаток (неполные исполнения, округления): "
          f"${(fa - e) + sum(bs) + sum(ss):+.4f}")
        by_v = defaultdict(list)
        for t in new:
            bv, sv = t.r.get("buy_venue"), t.r.get("sell_venue")
            if t.bslip_bps is not None:
                by_v[bv].append(t.bslip_bps)
            if t.sslip_bps is not None:
                by_v[sv].append(t.sslip_bps)
        for v, xs in by_v.items():
            w(f"  {v}: среднее проскальзывание {f(mean(xs), 2, True)} bps "
              f"на {len(xs)} ногах")
    w("")

    # 4. by direction
    w("== По направлению ==")
    rows = []
    for d in sorted({t.dir for t in trades}):
        ts = [t for t in trades if t.dir == d]
        rows.append([d, len(ts), f"{sum(t.turnover for t in ts):,.0f}",
                     f(sum(t.exp for t in ts), 4, True),
                     f(sum(t.fill_adj for t in ts), 4, True),
                     f(mean([t.fill_bps for t in ts]), 2, True),
                     f(mean([t.prem for t in ts]), 2, True)])
    out += table(["direction", "n", "turnover$", "exp$", "fill$",
                  "fill_bps", "premium"], rows) if rows else ["—"]
    w("")

    # 4b. open vs close (rows written since strategies were added)
    tagged = [t for t in trades if (t.r.get("action") or "") in ("open",
                                                                  "close")]
    w("== Открытие / закрытие позиции ==")
    if not tagged:
        w("нет данных: отметка появляется в сделках после этого обновления.")
    else:
        w("open = сделка увеличивает позицию на Entropy, close = уменьшает. "
          "Круг «вход + выход» — это пара open и close; доход круга = сумма "
          "обеих сделок. Отдельная сделка может быть в минусе по замыслу: "
          "при центре премии ниже нуля продажа Entropy и в плане идёт с "
          "отрицательным результатом, а покупка — с положительным.")
        rows = []
        for d in sorted({t.dir for t in tagged}):
            for act in ("open", "close"):
                ts = [t for t in tagged if t.dir == d and
                      t.r.get("action") == act]
                if not ts:
                    continue
                rows.append([f"{d} {act}", len(ts),
                             f(sum(t.exp for t in ts), 4, True),
                             f(sum(t.fill_adj for t in ts), 4, True),
                             f(mean([t.exp_bps for t in ts]), 2, True),
                             f(mean([t.fill_bps for t in ts]), 2, True),
                             f(mean([t.bslip_bps for t in ts]), 2, True),
                             f(mean([t.sslip_bps for t in ts]), 2, True)])
        out += table(["direction/action", "n", "exp$", "fill$", "exp_bps",
                      "fill_bps", "buy_slip", "sell_slip"], rows)
    w("")

    # 5. by hour UTC
    w("== По часам (UTC) ==")
    mh = defaultdict(list)
    for m in minutes:
        ts, pr = num(m.get("minute_ts")), num(m.get("premium_mean_bps"))
        if ts is not None and pr is not None:
            mh[time.gmtime(ts).tm_hour].append(pr)
    th = defaultdict(list)
    for t in trades:
        th[time.gmtime(t.ts).tm_hour].append(t)
    rows = []
    for h in range(24):
        ts = th.get(h, [])
        if not ts and not mh.get(h):
            continue
        rows.append([f"{h:02d}", len(ts),
                     f(sum(t.fill_adj for t in ts), 4, True) if ts else "",
                     f(mean([t.fill_bps for t in ts]), 2, True) if ts else "",
                     f(mean(mh.get(h, [])), 2, True),
                     f(st.pstdev(mh[h]) if len(mh.get(h, [])) > 1 else None,
                       2)])
    out += table(["hour", "trades", "fill$", "fill_bps", "mkt_prem",
                  "mkt_std"], rows) if rows else ["—"]
    w("")

    # 6. latency
    w("== Задержка ног (новый формат) ==")
    if new:
        for name, xs in (("buy_ms", [t.bms for t in new]),
                         ("sell_ms", [t.sms for t in new]),
                         ("gap_ms", [t.gap for t in new])):
            w(f"{name}: медиана {f(pct(xs, 50), 0)} · p90 {f(pct(xs, 90), 0)}"
              f" · max {f(pct(xs, 100), 0)}")
        buckets = [(0, 50), (50, 150), (150, 400), (400, 1e9)]
        rows = []
        for lo, hi in buckets:
            ts = [t for t in new if t.gap is not None and lo <= t.gap < hi]
            if not ts:
                continue
            slip = mean([((t.bslip_bps or 0) + (t.sslip_bps or 0))
                         for t in ts])
            rows.append([f"{lo:g}-{hi:g}" if hi < 1e9 else f">{lo:g}",
                         len(ts), f(slip, 2, True),
                         f(mean([t.fill_bps - (t.exp_bps or 0)
                                 for t in ts if t.fill_bps is not None]),
                           2, True)])
        out += table(["gap_ms", "n", "slip_bps(sum legs)", "fact-plan_bps"],
                     rows) if rows else ["—"]
    else:
        w("—")
    w("")

    # 7. statuses
    w("== Статусы ног ==")
    c = Counter(t.status for t in trades)
    for k, n in c.most_common(12):
        w(f"{k}: {n}")
    if not c:
        w("—")
    w("")

    # 7b. execution diagnostics
    diag = in_period(pick("exec_diag.csv"), "ts", cutoff)
    out += diag_lines(diag, cfg)

    # 8. hedges / closes
    w("== Выравнивания и закрытия позиций ==")
    if hedges:
        rows = []
        grp = defaultdict(list)
        for h in hedges:
            grp[(h.get("reason"), h.get("venue"))].append(h)
        for (reason, venue), hs in sorted(grp.items()):
            filled = sum((num(h.get("filled")) or 0) *
                         (num(h.get("avg_px")) or num(h.get("limit_px")) or 0)
                         for h in hs)
            bad = sum(1 for h in hs if h.get("err"))
            rows.append([reason, venue, len(hs), f"{filled:,.2f}", bad])
        out += table(["reason", "venue", "n", "filled$", "errors"], rows)
    else:
        w("—")
    w("")

    # 9. sessions
    w("== Сессии ==")
    if sessions:
        rows = []
        for s in sessions[-30:]:
            rows.append([utc(num(s.get("start_ts"))),
                         f"{(num(s.get('duration_sec')) or 0) / 3600:.1f}h",
                         f(num(s.get("pnl_usd")), 4, True),
                         f(num(s.get("pnl_pct")), 2, True),
                         f"{num(s.get('turnover_usd')) or 0:,.0f}",
                         s.get("trades"), f(num(s.get("pnl_bps_of_turnover")),
                                            2, True),
                         s.get("stop_reason"), s.get("positions_closed"),
                         s.get("midline_bps"), fund_cell(s)])
        out += table(["start_utc", "dur", "pnl$", "pnl%", "turnover$",
                      "trades", "pnl_bps", "stop", "closed", "midline",
                      "funding$ E/H"], rows)
    else:
        w("— (итоги сессий пишутся после обновления бота)")
    w("")

    # 10. equity
    w("== Баланс обеих бирж ==")
    tot = [(num(e.get("ts")), num(e.get("total_equity"))) for e in equity]
    tot = [(t, v) for t, v in tot if t is not None and v is not None]
    if tot:
        peak, dd = -1e18, 0.0
        for _, v in tot:
            peak = max(peak, v)
            dd = max(dd, peak - v)
        w(f"начало ${tot[0][1]:.2f} ({utc(tot[0][0])}) → конец "
          f"${tot[-1][1]:.2f} ({utc(tot[-1][0])}) · мин "
          f"${min(v for _, v in tot):.2f} · макс ${max(v for _, v in tot):.2f}"
          f" · макс. просадка ${dd:.2f}")
        w("(в разницу входят и пополнения/выводы между сессиями)")
    else:
        w("—")
    w("")

    # 11. market
    w("== Рынок (поминутная запись) ==")
    if minutes:
        pm = [num(m.get("premium_mean_bps")) for m in minutes]
        se = [num(m.get("sell_edge_max_bps")) for m in minutes]
        be = [num(m.get("buy_edge_max_bps")) for m in minutes]
        w(f"премия mid: p5 {f(pct(pm, 5), 2, True)} · p25 "
          f"{f(pct(pm, 25), 2, True)} · медиана {f(pct(pm, 50), 2, True)} · "
          f"p75 {f(pct(pm, 75), 2, True)} · p95 {f(pct(pm, 95), 2, True)}")
        mid = num(g(cfg, "thresholds", "midline_bps"))
        up = num(g(cfg, "thresholds", "upper_bps"))
        lo = num(g(cfg, "thresholds", "lower_bps"))
        if None not in (mid, up, lo):
            sell_h = mid + up + fee_ent + fee_hdg
            buy_h = lo - mid + fee_ent + fee_hdg
            ns = sum(1 for x in se if x is not None and x >= sell_h)
            nb = sum(1 for x in be if x is not None and x >= buy_h)
            w(f"минут, где SELL entropy проходил порог {sell_h:+.2f}: "
              f"{ns} из {len(minutes)}; BUY entropy (порог {buy_h:+.2f}): "
              f"{nb}")
        sp = defaultdict(list)
        for m in minutes:
            for v in ("entropy", "hedge"):
                b, k = num(m.get(f"{v}_bid")), num(m.get(f"{v}_ask"))
                if b and k:
                    sp[v].append((k / b - 1) * 1e4)
        for v, xs in sp.items():
            w(f"спред {v}: медиана {f(pct(xs, 50), 2)} bps · p90 "
              f"{f(pct(xs, 90), 2)} bps")
    else:
        w("—")
    w("")

    # 12. errors from the log
    w("== Ошибки и предупреждения из лога ==")
    errs = Counter()
    try:
        with open(p("engine.log"), "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - LOG_TAIL_BYTES))
            text = fh.read().decode("utf-8", "replace")
        dated = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
        undated = 0
        for line in text.splitlines():
            if " ERROR " not in line and " CRITICAL " not in line \
                    and " WARNING " not in line:
                continue
            m = dated.match(line)
            if m:
                t = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
                if t < cutoff:
                    continue
            else:
                undated += 1
                if a.hours > 0:
                    continue
            msg = line.split(": ", 1)[-1]
            msg = re.sub(r"[-+]?\d+(\.\d+)?", "#", msg)[:120]
            lvl = "CRIT" if " CRITICAL " in line else (
                "ERR" if " ERROR " in line else "WARN")
            errs[(lvl, msg)] += 1
        for (lvl, msg), n in errs.most_common(15):
            w(f"{n:>5} × {lvl} {msg}")
        if not errs:
            w("—")
        if undated and a.hours > 0:
            w(f"(строк старого формата без даты пропущено: {undated})")
    except FileNotFoundError:
        w("— (лог не найден)")
    w("")

    # 13. worst / best
    def line(t):
        return (f"{utc(t.ts)} {t.dir} q={t.qty:g} not=${t.bnot:.2f} "
                f"prem={f(t.prem, 2, True)} exp=${t.exp:+.4f} "
                f"fill=${t.fill_adj:+.4f} st={t.status}"
                + (f" bslip={f(t.bslip_bps, 1, True)}bps "
                   f"sslip={f(t.sslip_bps, 1, True)}bps gap={f(t.gap, 0)}ms"
                   if t.new else ""))

    w("== Худшие 10 сделок ==")
    for t in sorted(trades, key=lambda t: t.fill_adj)[:10]:
        w(line(t))
    w("== Лучшие 10 сделок ==")
    for t in sorted(trades, key=lambda t: -t.fill_adj)[:10]:
        w(line(t))
    w("")

    # 14. all trades (compact) when not too many
    if trades and len(trades) <= MAX_TRADE_LINES:
        w(f"== Все сделки за период ({len(trades)}) ==")
        for t in trades:
            w(line(t))
    elif trades:
        w(f"(всего {len(trades)} сделок — полный список не выводится, "
          f"выберите период короче)")
    w("")
    w("Вопрос для анализа: почему результат такой, что сильнее всего "
      "съедает доход (комиссия, проскальзывание, задержка ног, неудачные "
      "часы или направление) и какие настройки стоит изменить?")
    print("\n".join(out))


if __name__ == "__main__":
    main()
