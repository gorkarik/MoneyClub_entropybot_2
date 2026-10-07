"""Fast book snapshots (data for the backtester) and the midline hint of the
calibration (median premium over 24 h and 7 days)."""
import asyncio
import csv
import gzip
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.config import ConfigError, load_config  # noqa: E402
from entropy_arb.ticks import (HEADER, TickRecorder, day_of,  # noqa: E402
                               vwap_for_notional)
import analyze  # noqa: E402

DAY = 86400.0
T0 = 1_780_000_000.0   # some UTC day, not near midnight


def _book(bid, ask, sz=1.0, levels=1):
    b = OrderBook()
    b.apply_hl([[{"px": str(bid - i), "sz": str(sz)} for i in range(levels)],
                [{"px": str(ask + i), "sz": str(sz)} for i in range(levels)]])
    return b


def _rec(tmp, eb=None, hb=None, notional=20.0):
    return TickRecorder(str(tmp / "ticks"), "tradexyz", "SNDK",
                        eb or _book(100.0, 100.1),
                        hb or _book(99.9, 100.0), staleness_sec=10.0,
                        interval_sec=2.0, notional=notional)


def test_vwap_walks_the_book():
    asks = [(100.0, 0.1), (101.0, 1.0)]
    # $20: $10 at 100 (0.1) + $10 at 101 (0.0990…)
    px = vwap_for_notional(asks, 20.0)
    assert 100.0 < px < 101.0
    assert px == pytest.approx(20.0 / (0.1 + 10.0 / 101.0))
    assert vwap_for_notional(asks, 1000.0) is None      # book too thin
    assert vwap_for_notional([(100.0, 1.0)], 10.0) == pytest.approx(100.0)


def test_row_has_top_of_book_and_sized_prices(tmp_path):
    rec = _rec(tmp_path, eb=_book(100.0, 100.2, sz=0.1, levels=3))
    now = time.time()
    r = dict(zip(HEADER, rec.row(now)))
    assert (r["e_bid"], r["e_ask"], r["h_bid"], r["h_ask"]) == \
        ("100", "100.2", "99.9", "100")
    # $20 does not fit in the top 0.1 ($10): the average is worse than top
    assert float(r["e_buy_px"]) > 100.2 and float(r["e_sell_px"]) < 100.0
    assert r["notional"] == "20" and int(r["e_age_ms"]) >= 0


def test_stale_book_writes_nothing(tmp_path):
    eb = _book(100.0, 100.1)
    eb.alive_ts = time.time() - 60
    rec = _rec(tmp_path, eb=eb)
    assert rec.row(time.time()) is None and not rec.sample()
    assert not os.path.exists(tmp_path / "ticks")


def test_one_file_per_pair_and_day_old_day_compressed(tmp_path):
    rec = _rec(tmp_path)
    for i in range(3):
        rec.entropy_book.touch()
        rec.hedge_book.touch()
        assert rec.sample(T0 + i * 2)
    first = rec.path
    assert os.path.basename(first) == f"tradexyz_SNDK_{day_of(T0)}.csv"
    rec.entropy_book.touch()
    rec.hedge_book.touch()
    assert rec.sample(T0 + DAY)                  # next day: new file
    assert rec.path != first
    assert not os.path.exists(first)             # yesterday compressed (no loop)
    with gzip.open(first + ".gz", "rt") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == HEADER and len(rows) == 4
    rec._close_file()


def test_leftover_plain_files_found_for_compression(tmp_path):
    d = tmp_path / "ticks"
    d.mkdir()
    (d / "tradexyz_SNDK_2026-01-01.csv").write_text("x\n")
    (d / "tradexyz_ANTH_2026-01-01.csv").write_text("x\n")   # other pair
    rec = _rec(tmp_path)
    old = rec.stale_days("2026-01-02")
    assert [os.path.basename(p) for p in old] == ["tradexyz_SNDK_2026-01-01.csv"]
    assert rec.stale_days("2026-01-01") == []


def test_run_writes_and_stops(tmp_path):
    async def go():
        rec = _rec(tmp_path)
        rec.interval_sec = 0.01
        stop = asyncio.Event()
        t = asyncio.create_task(rec.run(stop))
        await asyncio.sleep(0.1)
        stop.set()
        await t
        return rec
    rec = asyncio.run(go())
    assert rec.rows_written >= 2
    with open(rec.path) as fh:
        assert fh.readline().strip() == ",".join(HEADER)


def test_ticks_setting_validated(tmp_path):
    base = "thresholds:\n  midline_bps: -7\n  upper_bps: 4\n  lower_bps: 4\n"
    p = tmp_path / "c.yaml"
    p.write_text(base)
    c = load_config(str(p), "/x", symbol="SNDK", hedge_venue="lighter-rh")
    assert c.ticks_sec == 2.0 and c.ticks_dir == "logs/ticks"   # on by default
    for ok in (0, 1, 5, 60):
        p.write_text(base + f"recorder:\n  ticks_sec: {ok}\n")
        load_config(str(p), "/x", symbol="SNDK", hedge_venue="lighter-rh")
    for bad in (0.5, 61, -1):
        p.write_text(base + f"recorder:\n  ticks_sec: {bad}\n")
        with pytest.raises(ConfigError, match="ticks_sec"):
            load_config(str(p), "/x", symbol="SNDK", hedge_venue="lighter-rh")


# --------------------------------------------------------- midline hint

def _minutes(path, start, n, prem):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["minute_ts", "premium_close_bps", "premium_mean_bps",
                    "sell_edge_max_bps", "buy_edge_max_bps", "samples"])
        for i in range(n):
            p = prem(i)
            w.writerow([start + i * 60, p, p, p + 1, -p + 1, 60])


def test_midline_hint_day_and_week(tmp_path):
    path = str(tmp_path / "m.csv")
    now = T0
    # 7 days: first 6 at -10, the last day at -7
    _minutes(path, now - 7 * DAY, 7 * 1440,
             lambda i: -7.0 if i >= 6 * 1440 else -10.0)
    hint = analyze.midline_hint(path, now=now)
    assert [h for h, _, _ in hint] == [24, 168]
    (_, day, n_day), (_, week, n_week) = hint
    assert day == -7.0 and n_day == 1440
    assert week == -10.0 and n_week == 7 * 1440
    lines = analyze.hint_lines(hint)
    assert "за 24 ч -7.0" in lines[0] and "сдвинулся" in lines[1]


def test_midline_hint_needs_enough_data(tmp_path):
    path = str(tmp_path / "m.csv")
    _minutes(path, T0 - 100 * 60, 100, lambda i: -8.0)
    hint = analyze.midline_hint(path, now=T0)
    assert all(med is None for _, med, _ in hint)
    assert "мало данных" in analyze.hint_lines(hint)[0]


def test_menu_reads_hint(tmp_path, monkeypatch):
    import club
    path = str(tmp_path / "m.csv")
    _minutes(path, time.time() - 2 * DAY, 2 * 1440, lambda i: -8.6)
    hint = club.midline_hint(path)
    assert [(h, med) for h, med, _ in hint] == [(24, -8.6), (168, -8.6)]
    assert abs(hint[0][2] - 1440) <= 1 and hint[1][2] == 2 * 1440
