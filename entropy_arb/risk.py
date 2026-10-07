"""Loss limit for one session (стоп по убытку за сессию).

A session is one run of the bot, Start .. Stop. At Start the bot reads both
venues' equity from the exchanges: that sum is the session baseline. It is
kept in memory only — the next Start takes a fresh one, so deposits and
withdrawals between sessions need no action.

Loss = baseline − current Σ equity. The stop trips only on two readings
over the limit, at least confirm_interval_sec apart, with every venue's
reading strictly newer than in the first one; a dip that does not repeat
is logged as possible measurement noise.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

OK = "ok"            # under the limit
PENDING = "pending"  # first reading over the limit: confirm later
WAIT = "wait"        # still over, but not yet a valid second reading
NOISE = "noise"      # was over, came back under: single dip, logged
TRIP = "trip"        # confirmed: stop


@dataclass
class Sample:
    total: float                  # Σ equity, both venues
    as_of: Dict[str, float]       # venue key -> data timestamp
    wall: float                   # when the sample was taken


class LossGuard:
    def __init__(self, base_total: float, max_loss_pct: float,
                 confirm_interval_sec: float) -> None:
        self.base_total = base_total
        self.limit_usd = base_total * max_loss_pct / 100.0
        self.confirm_interval_sec = confirm_interval_sec
        self.pending: Optional[Sample] = None
        self.last_loss: Optional[float] = None

    def loss(self, s: Sample) -> float:
        return self.base_total - s.total

    def observe(self, s: Sample) -> str:
        loss = self.loss(s)
        self.last_loss = loss
        if loss < self.limit_usd:
            if self.pending is not None:
                self.pending = None
                return NOISE
            return OK
        if self.pending is None:
            self.pending = s
            return PENDING
        p = self.pending
        newer = all(s.as_of.get(k, 0.0) > p.as_of[k] for k in p.as_of)
        if s.wall - p.wall >= self.confirm_interval_sec and newer:
            return TRIP
        return WAIT
