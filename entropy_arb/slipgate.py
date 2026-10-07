"""Realized slippage per venue (учёт реального проскальзывания).

Every filled leg of an arbitrage trade gives one sample: how much worse
(positive) or better (negative) its average fill was than the planned
average price at the moment of the decision, in bps. The model keeps the
recent samples of the pair (at most `max_samples`, none older than
`lookback_hours`) per venue and is saved to a small JSON file next to the
logs, so a restart does not forget it.

The gate (on only when the user enables it): an OPENING trade — one that
adds to the Entropy position — must also clear

    charge = weight × 2 × (max(0, median_entropy) + max(0, median_hedge))

on top of its usual threshold. "2 ×" because the round trip crosses both
venues twice and the closing trade is never charged (a position must always
be able to close). Until a venue has `min_fills` samples it adds nothing.

Why: in live logs the plan expected ~6.6 bps a trade and the fills lost
~8.6 bps of it, all on the Entropy leg. A threshold that ignores that loses
by construction; with the gate the bot simply stops opening trades whose
expected edge does not cover what execution has actually been costing.
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import time
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("slipgate")


def slip_file_name(venue: str, symbol: str, mode: str) -> str:
    """One history per pair AND execution mode: slippage measured with both
    legs at once says nothing about "Entropy first" (its Entropy leg cannot
    slip more than its price limit), so switching modes starts clean and
    switching back finds the old history again."""
    suffix = "" if mode == "simultaneous" else f"_{mode}"
    return f"slip_{venue}_{symbol}{suffix}.json"


def leg_slip_bps(is_buy: bool, avg_px: float, planned_px: float) -> float:
    """Slippage of one leg in bps; positive = cost."""
    if is_buy:
        return (avg_px / planned_px - 1.0) * 1e4
    return (1.0 - avg_px / planned_px) * 1e4


class SlipModel:
    def __init__(self, path: Optional[str], lookback_hours: float = 48.0,
                 min_fills: int = 5, max_samples: int = 50) -> None:
        self.path = path
        self.lookback_sec = lookback_hours * 3600.0
        self.min_fills = min_fills
        self.max_samples = max_samples
        self.samples: Dict[str, List[Tuple[float, float]]] = {}
        self._load()

    # ------------------------------------------------------------ storage

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
            for k, rows in (raw.get("samples") or {}).items():
                self.samples[k] = [(float(t), float(b)) for t, b in rows][
                    -self.max_samples:]
        except Exception:
            log.warning("slippage history %s unreadable — starting empty",
                        self.path)
            self.samples = {}

    def _save(self) -> None:
        if not self.path:
            return
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"samples": self.samples}, fh)
            os.replace(tmp, self.path)
        except Exception:
            log.exception("slippage history not saved")

    # -------------------------------------------------------------- model

    def add(self, venue_key: str, slip_bps: float,
            ts: Optional[float] = None) -> None:
        if slip_bps != slip_bps or abs(slip_bps) > 500:   # NaN / absurd
            return
        rows = self.samples.setdefault(venue_key, [])
        rows.append((time.time() if ts is None else ts, float(slip_bps)))
        del rows[:-self.max_samples]
        self._save()

    def recent(self, venue_key: str, now: Optional[float] = None) -> List[float]:
        now = time.time() if now is None else now
        return [b for t, b in self.samples.get(venue_key, [])
                if now - t <= self.lookback_sec]

    def median(self, venue_key: str, now: Optional[float] = None):
        """(median bps or None while too few samples, sample count)."""
        vals = self.recent(venue_key, now)
        if len(vals) < self.min_fills:
            return None, len(vals)
        return statistics.median(vals), len(vals)

    def charge_bps(self, venue_keys, weight: float = 1.0,
                   now: Optional[float] = None) -> float:
        """Round-trip charge for an opening trade (see module doc)."""
        one_way = 0.0
        for k in venue_keys:
            med, _n = self.median(k, now)
            if med is not None:
                one_way += max(0.0, med)
        return weight * 2.0 * one_way
