"""Backtester: replay of recorded book snapshots through the entry rules.

Run:  python3 -m pytest tests/
"""
import gzip
import os
import subprocess
import sys
import time
from dataclasses import replace

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb import backtest as bt  # noqa: E402
from entropy_arb.autonomy import AutonomyConf  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
OFF = AutonomyConf(enabled=False)


def tick(ts, prem_bps, h=100.0, spread_bps=1.0):
    """Both books 1 bps wide, Entropy mid = h × (1 + prem)."""
    e = h * (1 + prem_bps / 1e4)
    he, hh = e * spread_bps / 2e4, h * spread_bps / 2e4
    return bt.Tick(ts, e - he, e + he, h - hh, h + hh,
                   e + he, e - he, h + hh, h - hh)


def wave(n=400, lo=-10.0, hi=10.0, step=2.0, period=200):
    """Premium swinging lo..hi: a triangle with `period` ticks."""
    out = []
    for i in range(n):
        x = (i % period) / period
        prem = lo + (hi - lo) * (2 * x if x < 0.5 else 2 - 2 * x)
        out.append(tick(i * step, prem))
    return out


def test_round_trips_earn_the_band():
    p = bt.Params(midline_bps=0, upper_bps=5, lower_bps=5, order_usd=20,
                  min_order_usd=10, cap_usd=20, auto=OFF)
    r = bt.run(wave(), p)
    # every swing: sell Entropy at ≥ +5 (net of the 1 bps spread), buy back
    # at ≤ −5: the round trip earns ~10 bps of $20
    assert r.trades >= 4 and r.opens - r.closes in (0, 1)
    assert r.trades_usd > 0
    closed = [float(x["fill_edge_usd"]) for x in r.rows[:2 * r.closes]]
    rt_bps = sum(closed) / (20.0 * r.closes) * 1e4
    assert 6 < rt_bps < 14


def test_slip_and_fees_are_charged():
    base = bt.Params(midline_bps=0, upper_bps=5, lower_bps=5, cap_usd=20,
                     auto=OFF)
    clean = bt.run(wave(), base)
    slip = bt.run(wave(), replace(base, slip_bps=2.0))
    fee = bt.run(wave(), replace(base, entropy_fee_bps=1.0))
    approx_cost = clean.e_volume * 2.0 / 1e4
    assert abs((clean.pnl - slip.pnl) - approx_cost) < 1e-9
    assert fee.pnl < clean.pnl


def test_cap_hours_and_final_close():
    # premium only goes up: the bot sells Entropy to the cap and stays there
    ticks = [tick(i * 2.0, min(20.0, i * 0.5)) for i in range(400)]
    p = bt.Params(midline_bps=0, upper_bps=5, lower_bps=5, order_usd=20,
                  min_order_usd=10, cap_usd=60, auto=OFF)
    r = bt.run(ticks, p)
    assert r.opens == 3 and r.closes == 0
    assert 59 < r.max_pos_usd < 61
    assert r.hours_at_cap > 0.15                # most of the 800 s
    assert r.final_usd < 0                       # buying back at +20 costs


def test_floating_centre_follows_a_level_shift():
    # the premium sits at +30 instead of the midline 0 and swings ±8
    ticks = [replace(t, ts=t.ts) for t in
             [tick(i * 2.0, 30 + w) for i, w in enumerate(
                 [(-8 + 16 * ((i % 200) / 100 if (i % 200) < 100 else
                              2 - (i % 200) / 100)) for i in range(3000)])]]
    fixed = bt.Params(midline_bps=0, upper_bps=5, lower_bps=5, cap_usd=40,
                      auto=OFF)
    auto = replace(fixed, auto=AutonomyConf(window_min=30, min_min=10,
                                            open_cooldown_sec=0))
    r_fixed, r_auto = bt.run(ticks, fixed), bt.run(ticks, auto)
    assert r_fixed.closes == 0                   # stuck short at the cap
    assert r_auto.closes > 5 and r_auto.pnl > r_fixed.pnl


def test_volume_mode_trades_more():
    p = bt.Params(midline_bps=0, upper_bps=6, lower_bps=6, cap_usd=20,
                  auto=OFF)
    w = wave(lo=-7, hi=7)
    assert bt.run(w, replace(p, mode="volume")).trades > bt.run(w, p).trades


def test_variants_cover_autonomy_band_and_strategy():
    titles = [t for t, _ in bt.variants(bt.Params(0, 5, 5))]
    assert titles[0] == "Текущие настройки"
    assert "Без автономности" in titles and "Полоса шире на 1 bps" in titles
    assert "Стратегия 3 (полоса уже)" in titles


def test_tool_reads_tick_files(tmp_path):
    ticks_dir = tmp_path / "logs" / "ticks"
    ticks_dir.mkdir(parents=True)
    head = ("ts,time_utc,e_bid,e_bid_sz,e_ask,e_ask_sz,h_bid,h_bid_sz,h_ask,"
            "h_ask_sz,e_buy_px,e_sell_px,h_buy_px,h_sell_px,notional,"
            "e_age_ms,h_age_ms\n")
    now = time.time() - 3600
    lines = []
    for i, t in enumerate(wave(n=600)):
        lines.append(f"{now + t.ts},x,{t.e_bid},1,{t.e_ask},1,{t.h_bid},1,"
                     f"{t.h_ask},1,{t.e_buy},{t.e_sell},{t.h_buy},{t.h_sell},"
                     f"20,{9000 if i == 5 else 100},100\n")
    with gzip.open(ticks_dir / "lighter-rh_OAI_2026-10-14.csv.gz", "wt") as fh:
        fh.write(head + "".join(lines))
    assert len(bt.load_ticks(str(ticks_dir), "lighter-rh", "OAI")) == 599
    (tmp_path / "config.yaml").write_text(
        "thresholds:\n  midline_bps: 0\n  upper_bps: 5\n  lower_bps: 5\n"
        "entropy:\n  max_position_usd: 20\nhedge:\n  max_position_usd: 20\n"
        "sizing:\n  max_order_notional_usd: 20\n  min_order_notional_usd: 10\n")
    out = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "backtest.py"),
         "--symbol", "OAI", "--hedge-venue", "lighter-rh", "--days", "1",
         "--compare", "--env", str(tmp_path / "none"), "--slip-bps", "0"],
        cwd=tmp_path, capture_output=True, text=True, check=True).stdout
    assert "599 срезов" in out and "Текущие настройки" in out
    assert "Без автономности" in out
    assert "потеря на исполнении: 0 bps" in out
