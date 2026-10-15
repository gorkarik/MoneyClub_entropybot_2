"""Volume preset, SNDK/NBIS warning, holding time, result by balance.

Run:  python3 -m pytest tests/
"""
import csv
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb import holding, results  # noqa: E402
from entropy_arb import telegram as tg  # noqa: E402
from entropy_arb.strategy import (VOLUME_NARROW_BPS, miss_warning,  # noqa: E402
                                  volume_narrow_bps)
from test_engine import approx, make_engine  # noqa: E402
from test_tickers import RH, club_dir  # noqa: E402,F401

ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_volume_narrowing_is_fixed_and_fits_the_band():
    assert volume_narrow_bps(6, 5) == VOLUME_NARROW_BPS == 2.0
    assert volume_narrow_bps(3, 2) == 1.0          # half the narrower side
    assert volume_narrow_bps(0, 5) == 0.0


def test_miss_warning_only_for_tight_strategies_on_sndk_nbis():
    assert miss_warning("entropy_first", "SNDK")
    assert miss_warning("volume", "nbis")
    assert not miss_warning("simultaneous", "SNDK")
    assert not miss_warning("volume", "OAI")


def tr(ts, sid, ent_buy, qty, fill=0.0):
    """A trades.csv row: Entropy bought (ent_buy) or sold, both legs filled."""
    return {"ts": str(ts), "session_id": sid, "ok": "1",
            "buy_venue": "ENTROPY" if ent_buy else "RH",
            "sell_venue": "RH" if ent_buy else "ENTROPY",
            "buy_fill": str(qty), "sell_fill": str(qty),
            "fill_edge_usd": str(fill)}


def test_holding_fifo():
    rows = [tr(0, "s", False, 1.0),        # short 1
            tr(100, "s", False, 1.0),      # short 2
            tr(400, "s", True, 1.5),       # closes lot@0 (400 s), half lot@100
            tr(700, "s", True, 0.5)]       # closes the rest of lot@100
    h = holding.holding(rows)
    assert sorted(h.pieces) == [(300.0, 0.5), (400.0, 1.0), (600.0, 0.5)]
    approx(h.mean(), (400 + 0.5 * 300 + 0.5 * 600) / 2.0)
    assert h.median() == 400.0 and h.p90() == 600.0
    approx(h.share_in_position(), 1.0)      # never flat between 0 and 700


def test_holding_open_lots_close_at_session_end_and_hedges_count():
    rows = [tr(0, "s", True, 1.0), tr(100, "s", False, 1.0),
            tr(500, "s", True, 2.0)]
    hedges = [{"ts": "800", "session_id": "s", "venue": "ENTROPY",
               "side": "SELL", "filled": "1.0"},
              {"ts": "800", "session_id": "s", "venue": "RH",
               "side": "BUY", "filled": "9"}]
    sess = [{"session_id": "s", "start_ts": "0", "end_ts": "1000"}]
    h = holding.holding(rows, hedges, sess)
    # 100 s; 300 s (lot@500 half closed by the hedge at 800); 500 s (rest
    # still open, closed at the session end 1000)
    assert sorted(h.pieces) == [(100.0, 1.0), (300.0, 1.0), (500.0, 1.0)]
    # in position 0..100 and 500..1000 of 1000 s
    approx(h.share_in_position(), 0.6)
    assert h.median() == 300.0
    assert "в позиции 60% времени" in holding.summary(h)


def test_breakdown_line_explains_the_gap():
    line = results.breakdown_line(1.08, 5.53, -0.10, 0.02)
    assert line == ("Из результата: сделки +$5.53 · funding -$0.0800 · "
                    "закрытие остатка позиции и прочее -$4.37")
    assert results.breakdown_line(None, 5.0) is None


def test_sessions_breakdown():
    b = results.sessions_breakdown([
        {"pnl_usd": "1.08", "fill_edge_usd": "5.53",
         "funding_entropy_usd": "-0.1", "funding_hedge_usd": ""},
        {"pnl_usd": "2", "fill_edge_usd": ""},          # old row: skipped
    ])
    assert b["n"] == 1
    approx(b["rest"], 1.08 - 5.53 + 0.1)


def test_finish_text_has_breakdown():
    eng = make_engine()
    txt = tg.finish_text(eng, {"pnl": 1.08, "edge_compare": (20, 6.79, 5.53),
                               "funding_entropy": None, "funding_hedge": None,
                               "reason": "manual", "closed": True})
    assert "Из результата: сделки +$5.53 · закрытие остатка позиции и " \
           "прочее -$4.45" in txt


def _write(path, header, rows):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in header})


def test_totals_report_balance_and_holding(tmp_path):
    import time
    now = time.time()
    tcols = ["ts", "session_id", "ok", "symbol", "hedge_venue", "direction",
             "buy_venue", "sell_venue", "qty", "buy_fill", "sell_fill",
             "buy_avg_px", "sell_avg_px", "fill_edge_usd", "exp_edge_usd",
             "action"]
    rows = []
    for i, (ent_buy, fill) in enumerate([(False, -0.3), (True, 0.35)]):
        r = tr(now - 3600 + i * 600, "s1", ent_buy, 0.1, fill)
        r.update(symbol="OAI", hedge_venue="lighter-rh", qty="0.1",
                 buy_avg_px="100", sell_avg_px="100", exp_edge_usd="0.1",
                 direction="buy_entropy" if ent_buy else "sell_entropy")
        rows.append(r)
    _write(tmp_path / "trades.csv", tcols, rows)
    _write(tmp_path / "sessions.csv",
           ["session_id", "start_ts", "end_ts", "symbol", "hedge_venue",
            "pnl_usd", "fill_edge_usd", "funding_entropy_usd",
            "funding_hedge_usd", "fees_est_usd"],
           [{"session_id": "s1", "start_ts": str(now - 3700),
             "end_ts": str(now - 2000), "symbol": "OAI",
             "hedge_venue": "lighter-rh", "pnl_usd": "0.02",
             "fill_edge_usd": "0.05", "funding_entropy_usd": "-0.01",
             "funding_hedge_usd": "0", "fees_est_usd": "0.01"}])
    out = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "totals.py"), "--dir",
         str(tmp_path), "--symbol", "OAI", "--hedge-venue", "lighter-rh"],
        capture_output=True, text=True, check=True).stdout
    assert "Результат по балансу (1 сесс.): +$0.0200" in out
    assert "закрытие остатка позиции, выравнивания, разница комиссий: " \
           "-$0.0200" in out
    assert "Удержание позиции: среднее 10 мин" in out
    rep = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "report.py"), "--dir",
         str(tmp_path), "--hours", "0", "--config", str(tmp_path / "none")],
        capture_output=True, text=True, check=True).stdout
    assert "== Результат по балансу ==" in rep
    assert "удержание позиции на Entropy: среднее 10 мин" in rep


def test_strategy_menu_shows_fixed_preset_and_warning(club_dir, monkeypatch):
    import club
    from rich.console import Console
    club.save_config([("execution", "mode", "volume")], (RH, "SNDK"))
    monkeypatch.setattr(club, "current_pair", lambda: (RH, "SNDK"))
    rec = Console(record=True, width=120)
    monkeypatch.setattr(club, "console", rec)
    monkeypatch.setattr(club, "header", lambda *a, **k: None)
    monkeypatch.setattr(club, "ask", lambda *a, **k: "0")
    club.strategy_menu()
    out = rec.export_text()
    assert "вшито" in out and "полоса уже на" in out
    assert "цена объёма ≤ $1 за $10 000" in out
    assert "SNDK: у стратегий 2 и 3 здесь много промахов" in out
    assert "Сужение полосы" not in out and "Предельная цена" not in out
