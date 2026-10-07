#!/usr/bin/env python3
"""Totals per period: volume per venue, earned, lost, result — from the
bot's own journals (trades.csv, hedges.csv, sessions.csv). Nothing here
changes settings.

Итоги по периодам (неделя, месяц, всё время) по журналам бота.

Usage:
    python3 tools/totals.py --dir logs                       # all pairs
    python3 tools/totals.py --dir logs --symbol SNDK --hedge-venue lighter-rh
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import time
from collections import defaultdict

PERIODS = [(7, "Неделя"), (30, "Месяц"), (0, "Всё время")]
VENUE_TITLES = {"ENTROPY": "Entropy", "RH": "Lighter RH", "LIGHTER": "Lighter",
                "XYZ": "trade.xyz"}
HEDGE_TITLES = {"lighter-rh": "Lighter RH", "lighter": "Lighter",
                "tradexyz": "trade.xyz"}
STRATEGY_TITLES = {"simultaneous": "1 · Обе ноги сразу",
                   "entropy_first": "2 · Сначала Entropy",
                   "volume": "3 · Объём"}


def num(x):
    try:
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


def pair_of(r, legacy_symbol, legacy_venue):
    return ((r.get("symbol") or "").strip() or legacy_symbol,
            (r.get("hedge_venue") or "").strip() or legacy_venue)


def money(v, sign=True):
    if v is None:
        return "—"
    d = 4 if abs(v) < 1 else 2
    r = round(v, d)
    if r == 0:
        return "$0"
    s = f"${abs(r):,.{d}f}"
    if not sign:
        return s
    return ("+" if r > 0 else "-") + s


def leg_volume(r, side):
    """Filled notional of one leg: fill × average price when known, else
    the planned notional scaled by the filled share."""
    fill = num(r.get(f"{side}_fill")) or 0.0
    px = num(r.get(f"{side}_avg_px"))
    if px:
        return fill * px
    qty = num(r.get("qty")) or 0.0
    notional = num(r.get(f"{side}_notional")) or 0.0
    return notional * (fill / qty) if qty else 0.0


class Totals:
    def __init__(self):
        self.trades = 0
        self.opens = self.closes = 0
        self.vol = defaultdict(float)      # venue title -> $
        self.e_vol = 0.0                   # Entropy volume of the trades only
        self.fill = 0.0
        self.exp = 0.0
        self.gain = 0.0
        self.loss = 0.0
        self.wins = self.losses = 0
        self.days = defaultdict(float)     # UTC date -> fill $
        self.hedges = 0
        self.sessions = 0
        self.pnl = 0.0
        self.pnl_n = 0
        self.fund_e = 0.0
        self.fund_h = 0.0
        self.fund_n = 0
        self.fees = 0.0
        self.strategies = defaultdict(int)

    def add_trade(self, r):
        bv = leg_volume(r, "buy")
        sv = leg_volume(r, "sell")
        if bv <= 0 and sv <= 0:
            return
        b = VENUE_TITLES.get((r.get("buy_venue") or "").upper(),
                             r.get("buy_venue") or "?")
        s = VENUE_TITLES.get((r.get("sell_venue") or "").upper(),
                             r.get("sell_venue") or "?")
        self.vol[b] += bv
        self.vol[s] += sv
        self.e_vol += bv if b == "Entropy" else sv
        if bv <= 0 or sv <= 0:
            return           # one leg only: volume yes, a trade no
        self.trades += 1
        f = num(r.get("fill_edge_usd")) or 0.0
        self.fill += f
        self.exp += num(r.get("exp_edge_usd")) or 0.0
        if f >= 0:
            self.gain += f
            self.wins += 1
        else:
            self.loss += f
            self.losses += 1
        ts = num(r.get("ts")) or 0.0
        self.days[time.strftime("%Y-%m-%d", time.gmtime(ts))] += f
        a = (r.get("action") or "").strip()
        if a == "open":
            self.opens += 1
        elif a == "close":
            self.closes += 1
        st = (r.get("strategy") or "").strip()
        if st:
            self.strategies[st] += 1

    def add_hedge(self, r):
        filled = num(r.get("filled")) or 0.0
        px = num(r.get("avg_px")) or num(r.get("limit_px")) or 0.0
        if filled <= 0 or px <= 0:
            return
        v = VENUE_TITLES.get((r.get("venue") or "").upper(),
                             r.get("venue") or "?")
        self.vol[v] += filled * px
        self.hedges += 1

    def add_session(self, r):
        self.sessions += 1
        p = num(r.get("pnl_usd"))
        if p is not None:
            self.pnl += p
            self.pnl_n += 1
        fe, fh = num(r.get("funding_entropy_usd")), num(r.get("funding_hedge_usd"))
        if fe is not None or fh is not None:
            self.fund_e += fe or 0.0
            self.fund_h += fh or 0.0
            self.fund_n += 1
        self.fees += num(r.get("fees_est_usd")) or 0.0


def collect(trades, hedges, sessions, cutoff):
    t = Totals()
    for r in trades:
        if (num(r.get("ts")) or 0) >= cutoff:
            t.add_trade(r)
    for r in hedges:
        if (num(r.get("ts")) or 0) >= cutoff:
            t.add_hedge(r)
    for r in sessions:
        if (num(r.get("end_ts")) or 0) >= cutoff:
            t.add_session(r)
    return t


def block(t: Totals, title: str, hedge_title: str = ""):
    out = [f"── {title} ──"]
    if not t.trades and not t.vol and not t.sessions:
        out.append("  нет данных за период")
        return out
    total = sum(t.vol.values())
    out.append(f"  Объём всего: ${total:,.2f}")
    for venue in sorted(t.vol, key=lambda k: (k != "Entropy", k)):
        out.append(f"    {venue:<11} ${t.vol[venue]:,.2f}")
    oc = (f" (открытий {t.opens}, закрытий {t.closes})"
          if t.opens or t.closes else "")
    out.append(f"  Сделок: {t.trades}{oc} · выравниваний позиции: {t.hedges}")
    out.append(f"  По сделкам: заработано {money(t.gain)} в {t.wins} · "
               f"потеряно {money(t.loss)} в {t.losses}")
    out.append(f"  Итог по сделкам: {money(t.fill)} "
               f"(ожидалось {money(t.exp)})")
    if t.e_vol > 0:
        cost = -t.fill / t.e_vol * 1e4
        word = "стоили" if cost > 0 else "принесли"
        out.append(f"  $10 000 объёма на Entropy {word} {money(abs(cost), False)}"
                   f" (= {money(abs(cost) * 100, False)} за $1 000 000)")
    if t.sessions:
        out.append(f"  Результат по балансу (сессий: {t.sessions}): "
                   f"{money(t.pnl) if t.pnl_n else '—'}")
        if t.fund_n:
            out.append(f"    в нём funding: Entropy {money(t.fund_e)} · "
                       f"{hedge_title or 'хедж'} {money(t.fund_h)}")
        out.append(f"    комиссии (расчёт по ставкам настроек): "
                   f"{money(t.fees, False)}")
    if len(t.days) >= 2:
        best = max(t.days.items(), key=lambda kv: kv[1])
        worst = min(t.days.items(), key=lambda kv: kv[1])
        out.append(f"  Лучший день: {best[0]} {money(best[1])} · худший: "
                   f"{worst[0]} {money(worst[1])}")
    if t.strategies:
        out.append("  Стратегии: " + ", ".join(
            f"{STRATEGY_TITLES.get(k, k)} — {n}"
            for k, n in sorted(t.strategies.items())))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="logs")
    ap.add_argument("--symbol", default="")
    ap.add_argument("--hedge-venue", default="")
    ap.add_argument("--legacy-symbol", default="SNDK")
    ap.add_argument("--legacy-venue", default="lighter-rh")
    a = ap.parse_args()
    p = lambda n: os.path.join(a.dir, n)  # noqa: E731
    trades, hedges = read_csv(p("trades.csv")), read_csv(p("hedges.csv"))
    sessions = read_csv(p("sessions.csv"))
    leg = (a.legacy_symbol, a.legacy_venue)

    def of_pair(rows, pair):
        return [r for r in rows if pair_of(r, *leg) == pair]

    pairs = sorted({pair_of(r, *leg) for r in trades + sessions})
    now = time.time()
    out = []
    if a.symbol:
        pair = (a.symbol, a.hedge_venue or a.legacy_venue)
        trades, hedges, sessions = (of_pair(trades, pair), of_pair(hedges, pair),
                                    of_pair(sessions, pair))
        ht = HEDGE_TITLES.get(pair[1], pair[1])
        out.append(f"MONEY CLUB · итоги · {pair[0]} · Entropy ↔ {ht}")
        hedge_title = ht
    else:
        out.append("MONEY CLUB · итоги · все пары")
        hedge_title = ""
    out.append(f"сформировано {time.strftime('%Y-%m-%d %H:%M', time.gmtime(now))}"
               f" UTC · день считается по UTC")
    out.append("")
    if not trades and not sessions:
        out.append("Сделок пока нет — итоги появятся после торговли.")
        print("\n".join(out))
        return
    for days, title in PERIODS:
        cutoff = now - days * 86400 if days else 0.0
        t = collect(trades, hedges, sessions, cutoff)
        out += block(t, title, hedge_title)
        if not a.symbol and len(pairs) > 1:
            for pair in pairs:
                pt = collect(of_pair(trades, pair), of_pair(hedges, pair),
                             of_pair(sessions, pair), cutoff)
                if not pt.trades and not pt.sessions:
                    continue
                ht = HEDGE_TITLES.get(pair[1], pair[1])
                out.append(f"    {pair[0]} ↔ {ht}: сделок {pt.trades}, объём "
                           f"Entropy ${pt.vol.get('Entropy', 0):,.2f}, итог по "
                           f"сделкам {money(pt.fill)}"
                           + (f", по балансу {money(pt.pnl)}" if pt.pnl_n
                              else ""))
        out.append("")
    out.append("Итог по сделкам — разница цен исполнения обеих ног (с комиссией"
               " по ставке настроек).")
    out.append("Результат по балансу — изменение балансов бирж за сессии: в нём "
               "ещё funding,")
    out.append("выравнивания и открытые позиции. Возврат комиссии Entropy сюда "
               "не входит.")
    print("\n".join(out))


if __name__ == "__main__":
    main()
