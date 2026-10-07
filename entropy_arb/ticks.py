"""Fast book snapshots (частый срез стаканов) — data for the backtester.

The minute recorder (recorder.py) keeps one row per minute: enough to pick
thresholds, too coarse to replay the strategy, because a signal that lives
for a few seconds disappears inside the minute. This recorder writes one row
every `interval_sec` seconds (1..60, default 2) while both books are fresh.

Files: <dir>/<venue>_<SYMBOL>_<YYYY-MM-DD>.csv, one per pair and UTC day, so
markets never mix and old days are easy to archive or delete. When the day
rolls over (and at start-up, for days left over by a crash) the finished
file is gzip-compressed to .csv.gz and the plain file removed — a day at 2 s
is ~7 MB plain, ~1-2 MB compressed. The minute file is not touched.

Columns (prices as the venue quotes them, sizes in base units):

    ts, time_utc
    e_bid, e_bid_sz, e_ask, e_ask_sz      Entropy top of book
    h_bid, h_bid_sz, h_ask, h_ask_sz      hedge venue top of book
    e_buy_px, e_sell_px, h_buy_px, h_sell_px
        average price to buy / sell `notional` dollars on that venue right
        now (walking the book) — what a trade of max order size would
        actually pay; empty when the book is too thin
    notional                              the $ size those prices are for
    e_age_ms, h_age_ms                    ms since each book last changed
"""
from __future__ import annotations

import asyncio
import csv
import gzip
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from .book import OrderBook

log = logging.getLogger("ticks")

HEADER = ["ts", "time_utc",
          "e_bid", "e_bid_sz", "e_ask", "e_ask_sz",
          "h_bid", "h_bid_sz", "h_ask", "h_ask_sz",
          "e_buy_px", "e_sell_px", "h_buy_px", "h_sell_px", "notional",
          "e_age_ms", "h_age_ms"]


def vwap_for_notional(levels: List[Tuple[float, float]],
                      notional: float) -> Optional[float]:
    """Average price of taking `notional` dollars from `levels` (best
    first). None when the levels hold less than that."""
    if notional <= 0:
        return None
    spent = qty = 0.0
    for px, sz in levels:
        if px <= 0 or sz <= 0:
            continue
        take = min(sz, (notional - spent) / px)
        spent += take * px
        qty += take
        if spent >= notional * (1 - 1e-9):
            return spent / qty
    return None


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def gzip_file(path: str) -> bool:
    """path -> path.gz, then remove path. Returns True on success; a failure
    leaves the plain file in place (nothing is lost)."""
    tmp = path + ".gz.part"
    try:
        with open(path, "rb") as src, gzip.open(tmp, "wb", 6) as dst:
            shutil.copyfileobj(src, dst)
        os.replace(tmp, path + ".gz")
        os.remove(path)
        return True
    except Exception:
        log.exception("could not compress %s", path)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


class TickRecorder:
    def __init__(self, directory: str, venue: str, symbol: str,
                 entropy_book: OrderBook, hedge_book: OrderBook,
                 staleness_sec: float, interval_sec: float,
                 notional: float) -> None:
        self.dir = directory
        self.prefix = f"{venue}_{symbol}_"
        self.entropy_book = entropy_book
        self.hedge_book = hedge_book
        self.staleness_sec = staleness_sec
        self.interval_sec = interval_sec
        self.notional = notional
        self.rows_written = 0
        self.day: Optional[str] = None
        self.path: Optional[str] = None
        self._fh = None
        self._writer = None
        self._pending: List[asyncio.Task] = []

    def path_for(self, day: str) -> str:
        return os.path.join(self.dir, f"{self.prefix}{day}.csv")

    def stale_days(self, today: str) -> List[str]:
        """Plain files of this pair from earlier days (left by a crash or a
        stop before midnight) — to be compressed."""
        try:
            names = os.listdir(self.dir)
        except FileNotFoundError:
            return []
        pat = re.compile(re.escape(self.prefix) + r"(\d{4}-\d{2}-\d{2})\.csv$")
        out = []
        for n in sorted(names):
            m = pat.match(n)
            if m and m.group(1) < today:
                out.append(os.path.join(self.dir, n))
        return out

    def _open(self, day: str) -> None:
        os.makedirs(self.dir, exist_ok=True)
        path = self.path_for(day)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path) as fh0:
                if fh0.readline().strip() != ",".join(HEADER):
                    log.warning("%s has another header — moved to %s.old",
                                path, path)
                    os.replace(path, path + ".old")
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self._fh = open(path, "a", newline="")
        self._writer = csv.writer(self._fh)
        if new:
            self._writer.writerow(HEADER)
        self.day, self.path = day, path

    def _close_file(self) -> None:
        if self._fh is not None:
            self._fh.close()
        self._fh = self._writer = None

    def _compress_later(self, paths: List[str]) -> None:
        for p in paths:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                gzip_file(p)        # no loop (tests, shutdown): inline
                continue
            self._pending.append(loop.create_task(asyncio.to_thread(
                gzip_file, p)))

    def row(self, now: float) -> Optional[list]:
        eb, hb = self.entropy_book, self.hedge_book
        if not (eb.is_fresh(self.staleness_sec)
                and hb.is_fresh(self.staleness_sec)):
            return None
        e_b, e_a, h_b, h_a = (eb.sorted_bids(), eb.sorted_asks(),
                              hb.sorted_bids(), hb.sorted_asks())
        if not (e_b and e_a and h_b and h_a):
            return None
        n = self.notional

        def px(v):
            return "" if v is None else f"{v:.10g}"
        return [f"{now:.3f}",
                datetime.fromtimestamp(now, tz=timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z",
                f"{e_b[0][0]:.10g}", f"{e_b[0][1]:.10g}",
                f"{e_a[0][0]:.10g}", f"{e_a[0][1]:.10g}",
                f"{h_b[0][0]:.10g}", f"{h_b[0][1]:.10g}",
                f"{h_a[0][0]:.10g}", f"{h_a[0][1]:.10g}",
                px(vwap_for_notional(e_a, n)), px(vwap_for_notional(e_b, n)),
                px(vwap_for_notional(h_a, n)), px(vwap_for_notional(h_b, n)),
                f"{n:g}",
                f"{(now - eb.last_update_ts) * 1e3:.0f}",
                f"{(now - hb.last_update_ts) * 1e3:.0f}"]

    def sample(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        r = self.row(now)
        if r is None:
            return False
        day = day_of(now)
        if day != self.day:
            old = self.path
            self._close_file()
            self._open(day)
            if old is not None:
                self._compress_later([old])
        self._writer.writerow(r)
        self._fh.flush()
        self.rows_written += 1
        return True

    async def run(self, stop: asyncio.Event) -> None:
        self._compress_later(self.stale_days(day_of(time.time())))
        log.info("fast book snapshots every %gs -> %s/%s<date>.csv",
                 self.interval_sec, self.dir, self.prefix)
        try:
            while not stop.is_set():
                try:
                    self.sample()
                except Exception:
                    log.exception("tick sample failed")
                try:
                    await asyncio.wait_for(stop.wait(),
                                           timeout=self.interval_sec)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._close_file()
            if self._pending:
                await asyncio.gather(*self._pending, return_exceptions=True)
            log.info("fast snapshots stopped — %d row(s) this run",
                     self.rows_written)
