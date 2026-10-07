"""Auto-calibration of the midline (автокалибровка центра). OFF by default.

When the user turns it on, the bot moves ONLY thresholds.midline_bps of the
running pair toward the premium's median over the last `window_hours`, once
every `every_hours`. upper_bps / lower_bps are never touched. Guards:

  * enough data: at least `min_hours` of minutes inside the window;
  * small steps: at most `max_step_bps` per update;
  * a leash: never further than `max_drift_bps` from the ANCHOR — the
    midline the user last set by hand (menu, calibration) — so a slow drift
    of bad data cannot walk the midline away;
  * flat only: the update waits while a position is open (a midline moved
    under an open position would change its exit level);
  * changes under 0.1 bps are skipped.

Every change is written to the pair file (so it survives a restart), to
logs/autocalib.csv and to the log, and announced in Telegram. Setting the
midline by hand resets the anchor and the timer.
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from typing import List, Optional

from . import yamledit

WINDOWS = (24.0, 72.0, 168.0)       # menu choices: 24 h, 3 days, 7 days
STEP_MIN_BPS = 0.1                  # smaller changes are not worth a write


@dataclass
class Decision:
    target: Optional[float]         # median over the window (None: no data)
    minutes: int                    # minutes of data in the window
    new_midline: Optional[float]    # what to set (None: leave as is)
    reason: str                     # why (for the log / menu)


def median(vals: List[float]) -> float:
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def window_premiums(csv_path: str, window_hours: float, now: float,
                    min_samples: int = 10) -> List[float]:
    """premium_close_bps of the minutes inside the window (minute file of
    the pair; minutes with too few fresh samples are skipped)."""
    cutoff = now - window_hours * 3600.0
    out = []
    try:
        with open(csv_path, newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    ts = float(r["minute_ts"])
                    if ts < cutoff or ts > now:
                        continue
                    if int(r.get("samples") or 0) < min_samples:
                        continue
                    out.append(float(r["premium_close_bps"]))
                except (KeyError, ValueError, TypeError):
                    continue
    except FileNotFoundError:
        pass
    return out


def decide(current: float, anchor: float, prems: List[float], *,
           min_hours: float, max_step_bps: float,
           max_drift_bps: float) -> Decision:
    n = len(prems)
    if n < min_hours * 60:
        return Decision(None, n, None,
                        f"мало данных: {n} мин из нужных {min_hours * 60:.0f}")
    target = round(median(prems), 1)
    # step toward the target, then keep within the leash around the anchor
    step = max(-max_step_bps, min(max_step_bps, target - current))
    new = current + step
    new = max(anchor - max_drift_bps, min(anchor + max_drift_bps, new))
    new = round(new, 1) or 0.0
    if new == 0.0:
        # 0 means "not calibrated" in the pair file — never write it
        new = 0.1 if current > 0 else -0.1
    if abs(new - current) < STEP_MIN_BPS - 1e-9:
        why = ("уже у медианы" if abs(target - current) < STEP_MIN_BPS
               else f"упёрлось в предел ±{max_drift_bps:g} от ручного "
                    f"{anchor:+.1f}")
        return Decision(target, n, None, why)
    notes = []
    if abs(target - current) > max_step_bps + 1e-9:
        notes.append(f"шаг ограничен {max_step_bps:g} bps")
    if abs(new - anchor) >= max_drift_bps - 1e-9 and \
            abs(target - anchor) > max_drift_bps:
        notes.append(f"предел ±{max_drift_bps:g} от ручного {anchor:+.1f}")
    return Decision(target, n, new, "; ".join(notes))


def save(ticker_file: str, midline: float, now: float) -> None:
    """Persist the new midline and the time of this run in the pair file,
    keeping its comments."""
    with open(ticker_file, encoding="utf-8") as fh:
        text = fh.read()
    text = yamledit.set_value(text, "thresholds", "midline_bps", f"{midline:g}")
    text = yamledit.set_value(text, "ticker", "autocalib_last_ts", f"{now:.0f}")
    tmp = ticker_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, ticker_file)


def mark_run(ticker_file: str, now: float) -> None:
    """Remember that a check ran (even without a change), so a restart does
    not re-run it before every_hours have passed."""
    with open(ticker_file, encoding="utf-8") as fh:
        text = fh.read()
    text = yamledit.set_value(text, "ticker", "autocalib_last_ts", f"{now:.0f}")
    tmp = ticker_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, ticker_file)


def due(last_ts: Optional[float], every_hours: float, now: float) -> bool:
    return last_ts is None or now - last_ts >= every_hours * 3600.0 - 1.0


def window_label(hours: float) -> str:
    return {24.0: "24 ч", 72.0: "3 дня", 168.0: "7 дней"}.get(
        float(hours), f"{hours:g} ч")
