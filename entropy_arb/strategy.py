"""Strategies: names shown to the user, and the volume-cost guard.

Three ways to enter a trade (config: execution.mode). Exactly one is active:

  1  simultaneous   "Обе ноги сразу"   both IOC legs go out at once
  2  entropy_first  "Сначала Entropy"  Entropy IOC with a tight price limit
                                       first, the hedge only for what filled
  3  volume         "Объём"            as 2, with the entry band narrowed by
                                       volume_narrow_bps for more trades,
                                       held to a price of volume: while the
                                       realized cost of $10 000 of Entropy
                                       volume is above volume_max_cost_usd,
                                       opening trades need that much more
                                       premium (closes are never held back)

The volume cost is measured on the bot's own trades of this pair:

    cost per $10 000 of Entropy volume = − Σ fill_edge / Σ Entropy notional
                                         × 10 000   (dollars; = bps)

A negative fill edge is money the trades lost, so cost > 0 means volume is
being bought; cost <= 0 means the trades earn.
"""
from __future__ import annotations

import csv
import time
from collections import deque
from typing import Deque, Optional, Tuple

# key -> (number, title)
STRATEGIES = {
    "simultaneous": (1, "Обе ноги сразу"),
    "entropy_first": (2, "Сначала Entropy"),
    "volume": (3, "Объём"),
}
MODES = tuple(STRATEGIES)

VOLUME_WINDOW_TRADES = 50       # the cost is measured over the last N trades
VOLUME_WINDOW_HOURS = 72.0      # ... no older than this
VOLUME_MIN_TRADES = 10          # fewer trades: no guard yet (too noisy)
VOLUME_MAX_CHARGE_BPS = 20.0    # the guard never asks for more than this


def strategy_number(mode: str) -> int:
    return STRATEGIES.get(mode, (0, mode))[0]


def strategy_title(mode: str, with_number: bool = True) -> str:
    """'Стратегия 2 · Сначала Entropy'."""
    num, title = STRATEGIES.get(mode, (0, mode))
    return f"Стратегия {num} · {title}" if with_number and num else title


def tight_entropy_first(mode: str) -> bool:
    """Strategies that send the Entropy leg first with a tight limit."""
    return mode in ("entropy_first", "volume")


class VolumeCost:
    """Realized cost of Entropy volume over the recent trades of one pair.

    Samples are (ts, entropy_notional_usd, fill_edge_usd) of trades where
    both legs filled. Seeded from trades.csv on start, so a restart does not
    reset the guard."""

    def __init__(self, max_trades: int = VOLUME_WINDOW_TRADES,
                 window_hours: float = VOLUME_WINDOW_HOURS,
                 min_trades: int = VOLUME_MIN_TRADES) -> None:
        self.samples: Deque[Tuple[float, float, float]] = deque(
            maxlen=max_trades)
        self.window_sec = window_hours * 3600.0
        self.min_trades = min_trades

    def add(self, notional: float, fill_edge: float,
            ts: Optional[float] = None) -> None:
        if notional and notional > 0:
            self.samples.append((ts if ts is not None else time.time(),
                                 float(notional), float(fill_edge)))

    def seed_csv(self, path: str, symbol: str, venue: str,
                 legacy_symbol: str = "SNDK",
                 legacy_venue: str = "lighter-rh",
                 strategy: Optional[str] = "volume") -> int:
        """Load this pair's recent filled trades from trades.csv — by default
        only those made by strategy 3 (other strategies execute differently,
        their cost says little about this one)."""
        rows = []
        try:
            with open(path, newline="") as fh:
                for r in csv.DictReader(fh):
                    sym = (r.get("symbol") or "").strip() or legacy_symbol
                    ven = (r.get("hedge_venue") or "").strip() or legacy_venue
                    if sym != symbol or ven != venue or r.get("ok") != "1":
                        continue
                    if strategy and (r.get("strategy") or "") != strategy:
                        continue
                    try:
                        ts = float(r["ts"])
                        bf = float(r.get("buy_fill") or 0)
                        sf = float(r.get("sell_fill") or 0)
                        fill = float(r.get("fill_edge_usd") or 0)
                    except (KeyError, TypeError, ValueError):
                        continue
                    if bf <= 0 or sf <= 0:
                        continue
                    ent_buy = (r.get("buy_venue") or "").upper() == "ENTROPY"
                    key = "buy_notional" if ent_buy else "sell_notional"
                    try:
                        notional = float(r.get(key) or 0)
                    except ValueError:
                        continue
                    rows.append((ts, notional, fill))
        except FileNotFoundError:
            return 0
        for ts, notional, fill in rows[-self.samples.maxlen:]:
            self.add(notional, fill, ts)
        return len(self.samples)

    def recent(self, now: Optional[float] = None):
        now = now if now is not None else time.time()
        return [s for s in self.samples if now - s[0] <= self.window_sec]

    def cost_bps(self, now: Optional[float] = None) -> Tuple[Optional[float],
                                                             int]:
        """(cost of $10 000 of Entropy volume in dollars — the same number
        in bps; trades counted). None while there are too few trades."""
        rec = self.recent(now)
        notional = sum(s[1] for s in rec)
        if len(rec) < self.min_trades or notional <= 0:
            return None, len(rec)
        return -sum(s[2] for s in rec) / notional * 1e4, len(rec)

    def charge_bps(self, max_cost: float, now: Optional[float] = None) -> float:
        """Extra premium (bps) opening trades must clear: how far the
        realized cost is above the allowed price of volume, capped."""
        cost, _n = self.cost_bps(now)
        if cost is None or cost <= max_cost:
            return 0.0
        return min(cost - max_cost, VOLUME_MAX_CHARGE_BPS)
