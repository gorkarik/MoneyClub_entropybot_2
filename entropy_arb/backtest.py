"""Backtest: replay recorded book snapshots (logs/ticks) through the entry
rules with candidate settings, and see what they would have done.

What is modelled, the same way the engine does it:
  * the band: centre + upper to sell Entropy, centre − lower to buy it,
    both venues' taker fees on top (book.plan_arb's inequality);
  * autonomy: floating centre (median of the premium sampled every 10 s),
    exit relief for trades that reduce the position, opening cooldown and
    the jump pause;
  * strategy 3 narrowing, the inventory ladder, the position cap, the trade
    cooldown; closing trades never exceed the position.

What is not: depth beyond the $20 the snapshot measured (every fill is at
the recorded $20 VWAP), the race between the two legs, strategy 2/3 misses.
The execution loss the bot really has is charged per trade as `slip_bps`
of the Entropy notional — take it from the bot's own trades.

Бэктест: прогон записанных стаканов через правила входа с другими
настройками. Результат — оценка, не обещание.
"""
from __future__ import annotations

import csv
import glob
import gzip
import math
import os
import time
from dataclasses import dataclass, field, replace
from typing import Iterable, Iterator, List, Optional

from . import autonomy, holding
from .autonomy import AutonomyConf, Centre
from .strategy import volume_narrow_bps

MAX_AGE_MS = 3000.0        # a side older than this is stale: tick skipped
GAP_SEC = 60.0             # longer gaps (bot off) are not counted as time


@dataclass(frozen=True)
class Tick:
    ts: float
    e_bid: float
    e_ask: float
    h_bid: float
    h_ask: float
    e_buy: float           # $20 VWAP buying on Entropy
    e_sell: float
    h_buy: float
    h_sell: float


@dataclass(frozen=True)
class Params:
    midline_bps: float
    upper_bps: float
    lower_bps: float
    entropy_fee_bps: float = 0.0
    hedge_fee_bps: float = 0.0
    order_usd: float = 20.0
    min_order_usd: float = 10.0
    cap_usd: float = 150.0
    cooldown_sec: float = 0.0
    inventory_scale_bps: float = 0.0
    inventory_floor_frac: float = 0.5
    mode: str = "simultaneous"
    slip_bps: float = 0.0
    auto: AutonomyConf = field(default_factory=AutonomyConf)


@dataclass
class Result:
    trades: int = 0
    opens: int = 0
    closes: int = 0
    e_volume: float = 0.0
    trades_usd: float = 0.0        # sum of fill edges (fees, slip included)
    final_usd: float = 0.0         # closing what is left at the last tick
    hours: float = 0.0
    hours_at_cap: float = 0.0
    max_pos_usd: float = 0.0
    jump_pauses: int = 0
    rows: List[dict] = field(default_factory=list)

    @property
    def pnl(self) -> float:
        return self.trades_usd + self.final_usd

    @property
    def pnl_bps(self) -> Optional[float]:
        return self.pnl / self.e_volume * 1e4 if self.e_volume > 0 else None

    def hold(self) -> holding.Holding:
        return holding.holding(self.rows)


def _f(x) -> Optional[float]:
    try:
        v = float(x)
        return v if math.isfinite(v) and v > 0 else None
    except (TypeError, ValueError):
        return None


def tick_files(ticks_dir: str, venue: str, symbol: str) -> List[str]:
    pat = os.path.join(ticks_dir, f"{venue}_{symbol}_*.csv*")
    return sorted(glob.glob(pat))


def read_ticks(paths: Iterable[str], since: float = 0.0) -> Iterator[Tick]:
    for p in paths:
        opener = gzip.open if p.endswith(".gz") else open
        try:
            with opener(p, "rt", newline="") as fh:
                for r in csv.DictReader(fh):
                    ts = _f(r.get("ts"))
                    if ts is None or ts < since:
                        continue
                    if (_f(r.get("e_age_ms")) or 0) > MAX_AGE_MS or \
                            (_f(r.get("h_age_ms")) or 0) > MAX_AGE_MS:
                        continue
                    v = [_f(r.get(k)) for k in (
                        "e_bid", "e_ask", "h_bid", "h_ask", "e_buy_px",
                        "e_sell_px", "h_buy_px", "h_sell_px")]
                    if None in v:
                        continue
                    yield Tick(ts, *v)
        except (OSError, EOFError):
            continue


def load_ticks(ticks_dir: str, venue: str, symbol: str,
               days: float = 0.0) -> List[Tick]:
    since = time.time() - days * 86400 if days > 0 else 0.0
    ticks = list(read_ticks(tick_files(ticks_dir, venue, symbol), since))
    ticks.sort(key=lambda t: t.ts)
    return ticks


def _row(ts: float, ent_buy: bool, qty: float, fill: float) -> dict:
    """A trades.csv-like row, so holding.py can read the simulated trades."""
    return {"ts": str(ts), "session_id": "bt", "ok": "1",
            "buy_venue": "ENTROPY" if ent_buy else "HEDGE",
            "sell_venue": "HEDGE" if ent_buy else "ENTROPY",
            "buy_fill": str(qty), "sell_fill": str(qty),
            "fill_edge_usd": str(fill)}


def run(ticks: List[Tick], p: Params) -> Result:
    res = Result()
    auto = p.auto
    centre = Centre(auto, manual=p.midline_bps) if auto.enabled else None
    narrow = (volume_narrow_bps(p.upper_bps, p.lower_bps)
              if p.mode == "volume" else 0.0)
    fe, fh = p.entropy_fee_bps / 1e4, p.hedge_fee_bps / 1e4
    pos = 0.0                       # Entropy qty, hedge holds −pos
    last_sample = last_trade = last_open = -1e18
    open_ts: Optional[float] = None
    jump_on = False
    prev: Optional[Tick] = None
    for t in ticks:
        ref = (t.e_bid + t.e_ask) / 2.0
        h_mid = (t.h_bid + t.h_ask) / 2.0
        pos_usd = pos * ref
        if prev is not None:
            dt = t.ts - prev.ts
            if dt <= GAP_SEC:
                res.hours += dt / 3600.0
                if p.cap_usd - abs(pos_usd) < p.min_order_usd:
                    res.hours_at_cap += dt / 3600.0
        prev = t
        if centre is not None and t.ts - last_sample >= autonomy.SAMPLE_SEC:
            centre.add(t.ts, (ref / h_mid - 1.0) * 1e4)
            last_sample = t.ts
        if t.ts - last_trade < p.cooldown_sec:
            continue
        c = centre.live if centre is not None and centre.live is not None \
            else p.midline_bps
        u = min(abs(pos_usd) / p.cap_usd, 1.0) if p.cap_usd > 0 else 1.0
        age = t.ts - open_ts if open_ts is not None else None
        jumped = centre is not None and centre.jumped()
        if jumped and not jump_on:
            res.jump_pauses += 1
        jump_on = jumped

        for sell_entropy in (True, False):
            opens = pos <= 0 if sell_entropy else pos >= 0
            band = p.upper_bps if sell_entropy else p.lower_bps
            hurdle = (c + band) if sell_entropy else (band - c)
            if auto.enabled and not opens:
                hurdle -= autonomy.exit_relief_bps(auto, band, age, u)
            hurdle -= narrow
            if opens and p.inventory_scale_bps > 0:
                floor = min(max(p.inventory_floor_frac, 0.0), 0.99)
                if u > floor:
                    hurdle += p.inventory_scale_bps * (u - floor) / (1 - floor)
            if sell_entropy:
                ok = t.e_sell * (1 - fe) >= t.h_buy * (1 + fh) * \
                    (1 + hurdle / 1e4)
            else:
                ok = t.h_sell * (1 - fh) >= t.e_buy * (1 + fe) * \
                    (1 + hurdle / 1e4)
            if not ok:
                continue
            if opens:
                if auto.enabled and (t.ts - last_open < auto.open_cooldown_sec
                                     or jumped):
                    continue
                notional = min(p.order_usd, p.cap_usd - abs(pos_usd))
                if notional < p.min_order_usd:
                    continue
                qty = notional / ref
            else:            # never more than the position: no flip
                qty = min(p.order_usd / ref, abs(pos))
                if (abs(pos) - qty) * ref < p.min_order_usd:
                    qty = abs(pos)   # no remainder below the minimum order
                if qty <= 0:
                    continue
                notional = qty * ref
            if sell_entropy:
                fill = qty * (t.e_sell * (1 - fe) - t.h_buy * (1 + fh))
                pos -= qty
            else:
                fill = qty * (t.h_sell * (1 - fh) - t.e_buy * (1 + fe))
                pos += qty
            if abs(pos) * ref < 1e-6:
                pos = 0.0    # float dust after a full close
            fill -= notional * p.slip_bps / 1e4
            res.trades += 1
            res.opens += opens
            res.closes += not opens
            res.e_volume += notional
            res.trades_usd += fill
            res.rows.append(_row(t.ts, not sell_entropy, qty, fill))
            last_trade = t.ts
            if opens:
                last_open = t.ts
            was_flat = abs(pos_usd) < 1.0
            pos_usd = pos * ref
            res.max_pos_usd = max(res.max_pos_usd, abs(pos_usd))
            if abs(pos_usd) < 1.0:
                open_ts = None
            elif was_flat:
                open_ts = t.ts
            break
    if prev is not None and abs(pos) > 0:
        t = prev
        if pos > 0:          # sell Entropy, buy back the hedge
            res.final_usd = pos * (t.e_sell * (1 - fe) - t.h_buy * (1 + fh))
        else:
            res.final_usd = -pos * (t.h_sell * (1 - fh) - t.e_buy * (1 + fe))
        res.rows.append(_row(t.ts, pos < 0, abs(pos), res.final_usd))
    return res


def params_from_config(cfg) -> Params:
    """Params of a loaded Config (the pair's current settings)."""
    return Params(
        midline_bps=cfg.midline_bps, upper_bps=cfg.upper_bps,
        lower_bps=cfg.lower_bps, entropy_fee_bps=cfg.entropy.fee_bps,
        hedge_fee_bps=cfg.hedge.fee_bps,
        order_usd=cfg.max_order_notional,
        min_order_usd=cfg.min_order_notional,
        cap_usd=min(cfg.entropy.cap_usd, cfg.hedge.cap_usd),
        cooldown_sec=cfg.cooldown_sec,
        inventory_scale_bps=cfg.inventory_scale_bps,
        inventory_floor_frac=cfg.inventory_floor_frac,
        mode=cfg.exec_mode, auto=cfg.autonomy)


def variants(base: Params) -> List[tuple]:
    """(title, params) of the comparison the menu shows."""
    out = [("Текущие настройки", base)]
    if base.auto.enabled:
        out.append(("Без автономности", replace(
            base, auto=replace(base.auto, enabled=False))))
        for w in (30.0, 120.0):
            if w != base.auto.window_min:
                a = replace(base.auto, window_min=w,
                            min_min=min(base.auto.min_min, w))
                out.append((f"Центр по {w:g} мин", replace(base, auto=a)))
    else:
        out.append(("С автономностью", replace(
            base, auto=replace(base.auto, enabled=True))))
    out.append(("Полоса шире на 1 bps", replace(
        base, upper_bps=base.upper_bps + 1, lower_bps=base.lower_bps + 1)))
    if min(base.upper_bps, base.lower_bps) > 1.5:
        out.append(("Полоса уже на 1 bps", replace(
            base, upper_bps=base.upper_bps - 1, lower_bps=base.lower_bps - 1)))
    other = "volume" if base.mode != "volume" else "entropy_first"
    out.append(("Стратегия 3 (полоса уже)" if other == "volume"
                else "Без сужения стратегии 3", replace(base, mode=other)))
    return out
