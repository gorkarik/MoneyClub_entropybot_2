"""Holding time of the Entropy position, from the bot's journals.

Every fill on Entropy (arbitrage trades in trades.csv, position fixes and
the Stop close in hedges.csv) moves the Entropy position. Opening fills
become lots; reducing fills close the oldest lots first (FIFO), and each
closed piece is one holding time, weighted by its size. Lots still open
when a session ends are counted as closed at the session's end (the Stop
closes positions), or at its last fill when the end is unknown.

Время удержания позиции: среднее, медиана, p90 и доля времени в позиции.
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

EPS = 1e-9


def _num(x) -> Optional[float]:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def trade_qty(r: dict) -> float:
    """Signed Entropy quantity of one trades.csv row (+ bought)."""
    if (r.get("buy_venue") or "").upper() == "ENTROPY":
        return _num(r.get("buy_fill")) or 0.0
    if (r.get("sell_venue") or "").upper() == "ENTROPY":
        return -(_num(r.get("sell_fill")) or 0.0)
    return 0.0


def hedge_qty(r: dict) -> float:
    """Signed Entropy quantity of one hedges.csv row (0 for the hedge venue)."""
    if (r.get("venue") or "").upper() != "ENTROPY":
        return 0.0
    q = _num(r.get("filled")) or 0.0
    return q if (r.get("side") or "").upper() == "BUY" else -q


@dataclass
class Holding:
    pieces: List[Tuple[float, float]]   # (seconds held, qty)
    in_pos_sec: float = 0.0
    span_sec: float = 0.0

    @property
    def n(self) -> int:
        return len(self.pieces)

    def _sorted(self):
        return sorted(self.pieces)

    def mean(self) -> Optional[float]:
        w = sum(q for _, q in self.pieces)
        return sum(h * q for h, q in self.pieces) / w if w > 0 else None

    def quantile(self, p: float) -> Optional[float]:
        """Size-weighted quantile of the holding times."""
        s = self._sorted()
        w = sum(q for _, q in s)
        if w <= 0:
            return None
        acc = 0.0
        for h, q in s:
            acc += q
            if acc >= p * w - EPS:
                return h
        return s[-1][0]

    def median(self) -> Optional[float]:
        return self.quantile(0.5)

    def p90(self) -> Optional[float]:
        return self.quantile(0.9)

    def share_in_position(self) -> Optional[float]:
        return self.in_pos_sec / self.span_sec if self.span_sec > 0 else None


def _session_holding(events: List[Tuple[float, float]],
                     start: Optional[float], end: Optional[float],
                     out: Holding) -> None:
    events.sort()
    if not events:
        return
    t0 = start if start is not None else events[0][0]
    t1 = end if end is not None and end >= events[-1][0] else events[-1][0]
    lots: deque = deque()          # [ts, signed qty]
    pos = 0.0
    since: Optional[float] = None  # when the position became non-flat
    for ts, q in events:
        if abs(q) < EPS:
            continue
        while abs(q) > EPS and lots and (lots[0][1] > 0) != (q > 0):
            lt, lq = lots[0]
            m = min(abs(lq), abs(q))
            out.pieces.append((max(ts - lt, 0.0), m))
            lq += m if lq < 0 else -m
            q += -m if q > 0 else m
            if abs(lq) < EPS:
                lots.popleft()
            else:
                lots[0][1] = lq
        if abs(q) > EPS:
            lots.append([ts, q])
        was_flat = abs(pos) < EPS
        pos = sum(lq for _, lq in lots)
        if was_flat and abs(pos) >= EPS:
            since = ts
        elif not was_flat and abs(pos) < EPS and since is not None:
            out.in_pos_sec += ts - since
            since = None
    for lt, lq in lots:            # still open: closed at the session end
        out.pieces.append((max(t1 - lt, 0.0), abs(lq)))
    if since is not None:
        out.in_pos_sec += t1 - since
    out.span_sec += max(t1 - t0, 0.0)


def holding(trades: Iterable[dict], hedges: Iterable[dict] = (),
            sessions: Iterable[dict] = (), cutoff: float = 0.0) -> Holding:
    """Holding time over the given (already pair-filtered) journal rows."""
    ev: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    for r in trades:
        ts = _num(r.get("ts"))
        if ts is None or ts < cutoff:
            continue
        q = trade_qty(r)
        if q:
            ev[r.get("session_id") or ""].append((ts, q))
    for r in hedges:
        ts = _num(r.get("ts"))
        if ts is None or ts < cutoff:
            continue
        q = hedge_qty(r)
        if q:
            ev[r.get("session_id") or ""].append((ts, q))
    bounds = {}
    for r in sessions:
        sid = r.get("session_id") or ""
        bounds[sid] = (_num(r.get("start_ts")), _num(r.get("end_ts")))
    out = Holding(pieces=[])
    for sid, events in ev.items():
        start, end = bounds.get(sid, (None, None))
        if start is not None:
            start = max(start, cutoff)
        _session_holding(events, start, end, out)
    return out


def dur(sec: Optional[float]) -> str:
    """'45 с', '12 мин', '3.4 ч', '2.1 дн'."""
    if sec is None:
        return "—"
    if sec < 90:
        return f"{sec:.0f} с"
    if sec < 90 * 60:
        return f"{sec / 60:.0f} мин"
    if sec < 48 * 3600:
        return f"{sec / 3600:.1f} ч"
    return f"{sec / 86400:.1f} дн"


def summary(h: Holding) -> Optional[str]:
    """'среднее 42 мин · медиана 12 мин · p90 2.5 ч (по 37 закрытиям) ·
    в позиции 64% времени' — None without closed pieces."""
    if not h.n:
        return None
    s = (f"среднее {dur(h.mean())} · медиана {dur(h.median())} · "
         f"p90 {dur(h.p90())} (частей позиции: {h.n})")
    share = h.share_in_position()
    if share is not None:
        s += f" · в позиции {share * 100:.0f}% времени"
    return s
