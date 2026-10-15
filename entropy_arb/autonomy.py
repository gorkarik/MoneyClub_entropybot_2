"""Autonomy: keep the bot trading without manual re-centering (ON by default).

Three rules, all settings in config.yaml `autonomy:`.

  * Floating centre. The engine samples the mid-to-mid premium every
    SAMPLE_SEC and uses the median of the last `window_min` minutes as the
    live centre of the band, also while a position is open. It never moves
    further than `max_drift_bps` from the midline set in the pair file (a
    leash against bad data). The pair file is never written: the live centre
    exists only while the bot runs. With too little data (`min_min` minutes)
    the pair's midline is used.

  * Exit relief. The hurdle of a trade that REDUCES the position falls from
    the full band (centre + upper / centre − lower) toward
    centre ± `exit_floor_bps` as the position ages (linear to `exit_hours`)
    and, up to `size_weight` of the way, as it fills the second half of
    the cap. A position the
    market left behind is closed near break-even instead of being held for
    days at the cap.

  * Calm opening. Trades that ADD to the position are spaced by at least
    `open_cooldown_sec`, and paused while the premium of the last
    `jump_window_min` minutes is more than `jump_bps` away from the centre
    (a level shift: the centre has not caught up yet). Closing trades are
    never held back.
"""
from __future__ import annotations

import csv
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional, Tuple

SAMPLE_SEC = 10.0          # how often the engine samples the premium


@dataclass(frozen=True)
class AutonomyConf:
    enabled: bool = True
    window_min: float = 60.0
    min_min: float = 20.0
    max_drift_bps: float = 50.0
    exit_hours: float = 6.0
    exit_floor_bps: float = 0.0
    size_weight: float = 0.5
    open_cooldown_sec: float = 30.0
    jump_bps: float = 20.0
    jump_window_min: float = 5.0


def median(vals: List[float]) -> float:
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


class Centre:
    """Rolling premium samples → live centre and level-shift detector."""

    def __init__(self, conf: AutonomyConf, manual: float) -> None:
        self.conf = conf
        self.manual = manual               # pair-file midline (the leash)
        self.samples: Deque[Tuple[float, float]] = deque()
        self.live: Optional[float] = None  # None: not enough data yet
        self.recent: Optional[float] = None
        self.at_leash = False

    def seed(self, rows: List[Tuple[float, float]]) -> None:
        """Older (ts, premium) rows, e.g. minutes of the pair file."""
        for ts, p in sorted(rows):
            self.samples.append((ts, p))

    def add(self, ts: float, premium: float) -> None:
        self.samples.append((ts, premium))
        self.update(ts)

    def update(self, now: float) -> None:
        c = self.conf
        cut = now - c.window_min * 60.0
        while self.samples and self.samples[0][0] < cut:
            self.samples.popleft()
        if not self.samples or \
                now - self.samples[0][0] < c.min_min * 60.0 - SAMPLE_SEC:
            self.live = self.recent = None
            self.at_leash = False
            return
        med = median([p for _, p in self.samples])
        lo, hi = self.manual - c.max_drift_bps, self.manual + c.max_drift_bps
        self.at_leash = not lo <= med <= hi
        self.live = round(max(lo, min(hi, med)), 2)
        rcut = now - c.jump_window_min * 60.0
        recent = [p for t, p in self.samples if t >= rcut]
        self.recent = median(recent) if recent else None

    def jumped(self) -> bool:
        if self.live is None or self.recent is None or self.conf.jump_bps <= 0:
            return False
        return abs(self.recent - self.live) > self.conf.jump_bps


def seed_rows(csv_path: str, window_min: float, now: float,
              min_samples: int = 10) -> List[Tuple[float, float]]:
    """(ts, premium) of the pair's recorded minutes inside the window, so a
    restart does not wait `min_min` minutes for the centre."""
    cut = now - window_min * 60.0
    out: List[Tuple[float, float]] = []
    try:
        with open(csv_path, newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    ts = float(r["minute_ts"]) + 30.0     # mid of the minute
                    if ts < cut or ts > now:
                        continue
                    if int(r.get("samples") or 0) < min_samples:
                        continue
                    out.append((ts, float(r["premium_mean_bps"])))
                except (KeyError, ValueError, TypeError):
                    continue
    except FileNotFoundError:
        pass
    return out


def exit_fraction(conf: AutonomyConf, age_sec: Optional[float],
                  cap_frac: float) -> float:
    """0..1: how far a reducing trade's hurdle moves from the full band
    toward the exit floor."""
    f_age = 0.0
    if age_sec is not None and conf.exit_hours > 0:
        f_age = min(max(age_sec, 0.0) / (conf.exit_hours * 3600.0), 1.0)
    # the size part starts at half the cap, like the inventory ladder
    u = min(max(cap_frac, 0.0), 1.0)
    f_size = conf.size_weight * max(0.0, (u - 0.5) / 0.5)
    return max(f_age, f_size)


def exit_relief_bps(conf: AutonomyConf, band_bps: float, age_sec: Optional[float],
                    cap_frac: float) -> float:
    """How many bps to take off a reducing trade's hurdle (never below
    the floor: relief ≤ band − floor)."""
    room = band_bps - conf.exit_floor_bps
    if room <= 0:
        return 0.0
    return room * exit_fraction(conf, age_sec, cap_frac)
