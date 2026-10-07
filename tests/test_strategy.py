"""Strategy options: "Entropy first, then hedge" execution, the realized-
slippage gate, the "too good to be real" signal ceiling, and the execution
diagnostics file."""
import asyncio
import csv
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import entropy_arb.engine as engine_mod  # noqa: E402
from entropy_arb.config import ConfigError, load_config  # noqa: E402
from entropy_arb.slipgate import SlipModel, leg_slip_bps  # noqa: E402
from test_guard import _arb_engine  # noqa: E402
from test_tickers import BASE  # noqa: E402


def _signal(eng):
    eng._scan(time.time())                 # arms
    best = eng._scan(time.time())
    assert best is not None
    return best


def _run_trade(eng):
    async def go():
        best = _signal(eng)
        await eng._execute(*best)
        return best
    return asyncio.run(go())


# ------------------------------------------------ Entropy first, then hedge

def test_entropy_first_hedges_exactly_what_filled(tmp_path):
    eng = _arb_engine(tmp_path)
    eng.cfg.exec_mode = "entropy_first"
    buy, sell, plan = _run_trade(eng)
    assert sell is eng.entropy                     # this signal sells Entropy
    # Entropy went alone first, with the tight limit; then the hedge
    (e_buy, e_qty, e_px, _), = eng.entropy.sent
    assert not e_buy and e_qty == plan.qty
    assert e_px == pytest.approx(plan.sell_limit * (1 - 5.0 / 1e4))
    (h_buy, h_qty, _, _), = eng.hedge.sent
    assert h_buy and h_qty == pytest.approx(plan.qty)
    assert eng.trades == 1 and eng.ef_attempts == 1 and eng.ef_misses == 0
    assert eng.entropy.position + eng.hedge.position == pytest.approx(0)


def test_entropy_first_miss_sends_nothing_else_and_is_no_error(tmp_path):
    eng = _arb_engine(tmp_path)
    eng.cfg.exec_mode = "entropy_first"

    async def no_fill(**kw):
        eng.entropy.sent.append(kw)
        return {"status": "canceled", "filled_base": 0.0, "avg_px": None,
                "err": None, "unresolved": False}
    eng.entropy.send_taker = no_fill
    _run_trade(eng)
    assert eng.hedge.sent == []                    # no hedge, no naked leg
    assert eng.trades == 0 and eng.ef_misses == 1
    assert eng.consec_errors == 0 and not eng.halted
    assert eng.entropy.position == 0 and eng.hedge.position == 0
    assert eng.edge_compare() is None              # a miss is not a trade
    rows = list(csv.DictReader(open(eng.cfg.trades_csv)))
    assert rows[-1]["ok"] in ("0", "False")


def test_entropy_first_partial_fill_hedges_the_part(tmp_path):
    eng = _arb_engine(tmp_path)
    eng.cfg.exec_mode = "entropy_first"
    real = eng.entropy.send_taker

    async def half(*, is_buy, qty, limit_px, reduce_only=False):
        return await real(is_buy=is_buy, qty=round(qty / 2, 4),
                          limit_px=limit_px, reduce_only=reduce_only)
    eng.entropy.send_taker = half
    _, _, plan = _run_trade(eng)
    (_, h_qty, _, _), = eng.hedge.sent
    assert h_qty == pytest.approx(round(plan.qty / 2, 4))
    n, exp, got = eng.edge_compare()
    assert exp == pytest.approx(plan.exp_edge_usd * h_qty / plan.qty)


def test_simultaneous_mode_unchanged(tmp_path):
    eng = _arb_engine(tmp_path)
    _run_trade(eng)
    assert len(eng.entropy.sent) == 1 and len(eng.hedge.sent) == 1
    assert eng.ef_attempts == 0


# ------------------------------------------------- realized slippage gate

def test_slip_model_median_window_and_persistence(tmp_path):
    p = str(tmp_path / "slip.json")
    m = SlipModel(p, lookback_hours=1, min_fills=3, max_samples=4)
    now = time.time()
    m.add("entropy", 9.0, now - 7200)              # too old
    for b in (8.0, 10.0):
        m.add("entropy", b, now)
    assert m.median("entropy", now) == (None, 2)   # too few recent
    m.add("entropy", 12.0, now)
    assert m.median("entropy", now) == (10.0, 3)
    m.add("hedge", -1.0, now)
    m2 = SlipModel(p, lookback_hours=1, min_fills=1, max_samples=4)
    assert m2.median("entropy", now)[0] == 10.0    # survived a restart
    # negative slippage (a gift) never lowers the hurdle
    assert m2.charge_bps(["entropy", "hedge"], 1.0, now) == pytest.approx(20.0)
    assert leg_slip_bps(True, 100.1, 100.0) == pytest.approx(10.0)
    assert leg_slip_bps(False, 99.9, 100.0) == pytest.approx(10.0)


def test_gate_charges_opening_trades_only(tmp_path):
    eng = _arb_engine(tmp_path)
    for _ in range(5):
        eng.slip.add("entropy", 8.0)
        eng.slip.add("hedge", 0.5)
    base = eng._eff_threshold(eng.hedge, eng.entropy)
    eng.cfg.slipgate_enabled = True
    # flat: selling Entropy opens a position -> 2 × (8 + 0.5)
    assert eng._eff_threshold(eng.hedge, eng.entropy) == \
        pytest.approx(base + 17.0)
    # long Entropy: selling it closes -> no charge
    eng.entropy.position = 0.5
    assert eng._eff_threshold(eng.hedge, eng.entropy) == pytest.approx(base)


def test_gate_stops_a_trade_that_execution_would_eat(tmp_path):
    eng = _arb_engine(tmp_path)
    eng.cfg.slipgate_enabled = True
    for _ in range(5):
        eng.slip.add("entropy", 80.0)             # 2 × 80 > the ~99 bps edge
    eng._scan(time.time())
    assert eng._scan(time.time()) is None


def test_fills_teach_the_model(tmp_path):
    eng = _arb_engine(tmp_path)
    _run_trade(eng)
    assert len(eng.slip.recent("entropy")) == 1
    assert len(eng.slip.recent("hedge")) == 1


# --------------------------------------------- "too good to be real" ceiling

def test_ceiling_skips_a_premium_far_beyond_the_hurdle(tmp_path):
    async def go():
        eng = _arb_engine(tmp_path)                # books show ~99 bps
        eng.cfg.max_excess_bps = 6.0
        eng._scan(time.time())
        assert eng._scan(time.time()) is None
        eng._scan(time.time())
        assert eng._scan(time.time()) is None
        assert eng.excess_skips == 1               # counted once per episode
        eng.cfg.max_excess_bps = 0.0               # off -> trades again
        eng._scan(time.time())
        assert eng._scan(time.time()) is not None
    asyncio.run(go())


# ----------------------------------------------------- execution diagnostics

def test_diagnostics_row_written(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod, "DIAG_MARKS_SEC", (0.01, 0.02))

    async def go():
        eng = _arb_engine(tmp_path)
        eng.cfg.exec_mode = "entropy_first"
        best = _signal(eng)
        await eng._execute(*best)
        await asyncio.sleep(0.1)
        return eng
    eng = asyncio.run(go())
    p = os.path.join(os.path.dirname(eng.cfg.trades_csv), "exec_diag.csv")
    row = list(csv.DictReader(open(p)))[-1]
    assert row["outcome"] == "filled" and row["mode"] == "entropy_first"
    assert row["direction"] == "sell_entropy"
    assert float(row["e_slip_bps"]) == pytest.approx(5.0, abs=0.01)
    assert row["mid_prem0_bps"] and row["mid_prem3_bps"] and row["e_mark1_bps"]


# ------------------------------------------------------------------ config

def test_strategy_config_defaults_and_validation(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(BASE)
    c = load_config(str(p), "/x", symbol="SNDK", hedge_venue="lighter-rh")
    assert c.exec_mode == "simultaneous" and c.max_excess_bps == 0
    assert c.slipgate_enabled is False and c.entropy_first_slip_bps == 5.0
    for bad in ("execution:\n  mode: maker\n",
                "execution:\n  entropy_first_slip_bps: 0.1\n",
                "slipgate:\n  weight: 0\n"):
        p.write_text(BASE + bad)
        with pytest.raises(ConfigError):
            load_config(str(p), "/x", symbol="SNDK", hedge_venue="lighter-rh")


# ------------------------------------------------------ report and the menu

def test_report_shows_whether_signals_vanish(tmp_path):
    import subprocess
    logs = tmp_path / "logs"
    logs.mkdir()
    now = time.time()
    hdr = engine_mod.DIAG_HEADER
    rows = []
    for i in range(6):        # sell signals at -2 that are back at -9 in 1 s
        rows.append({"ts": f"{now - 60 * i:.3f}", "symbol": "SNDK",
                     "hedge_venue": "lighter-rh", "mode": "entropy_first",
                     "direction": "sell_entropy",
                     "outcome": "missed" if i % 2 else "filled",
                     "signal_age_ms": "320", "e_age_ms": "450",
                     "h_age_ms": "40", "top_prem_bps": "-1.5",
                     "mid_prem0_bps": "-2.0", "mid_prem1_bps": "-8.5",
                     "mid_prem3_bps": "-9.0", "e_slip_bps": "4.0",
                     "e_mark1_bps": "-6.0", "e_mark3_bps": "-7.0",
                     "e_ms": "350", "h_ms": "120"})
    with open(logs / "exec_diag.csv", "w") as fh:
        fh.write(",".join(hdr) + "\n")
        for r in rows:
            fh.write(",".join(r.get(k, "") for k in hdr) + "\n")
    (tmp_path / "config.yaml").write_text(
        BASE + "execution:\n  mode: entropy_first\n")
    root = os.path.join(os.path.dirname(__file__), "..")
    out = subprocess.run([sys.executable, os.path.join(root, "tools",
                                                      "report.py"),
                          "--dir", str(logs), "--config",
                          str(tmp_path / "config.yaml"), "--hours", "24",
                          "--symbol", "SNDK", "--hedge-venue", "lighter-rh"],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    t = out.stdout
    assert "Диагностика сигналов" in t and "промахов 3 из 6" in t
    assert "Entropy 450 мс" in t
    line = [l for l in t.splitlines() if l.startswith("sell_entropy")
            and "+6.50" in l]
    assert line, t                                # the spike faded 6.5 bps


def test_strategy_menu_settings_load(tmp_path, monkeypatch):
    root = os.path.join(os.path.dirname(__file__), "..")
    import club
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.example.yaml").write_text(
        open(os.path.join(root, "config.example.yaml"), encoding="utf-8").read())
    (tmp_path / "config.yaml").write_text(BASE)
    monkeypatch.setattr(club, "find_bots", lambda: [])
    club.ensure_ticker_files()
    pair = ("lighter-rh", "SNDK")
    v = club.cfg_values(pair)
    assert v["exec_mode"] == "simultaneous"
    assert club.strategy_label(v["exec_mode"]) == "Стратегия 1 · Обе ноги сразу"
    assert club.protection_summary(v) == "выключена"
    club.save_config([("execution", "mode", "volume"),
                      ("execution", "volume_narrow_bps", "1.5"),
                      ("execution", "volume_max_cost_usd", "1"),
                      ("execution", "max_excess_bps", "6"),
                      ("slipgate", "enabled", "true")], pair)
    v = club.cfg_values(pair)
    assert club.strategy_label(v["exec_mode"]) == "Стратегия 3 · Объём"
    assert "поправка на потери" in club.protection_summary(v)
    c = load_config("config.yaml", "/x", symbol="SNDK",
                    hedge_venue="lighter-rh")
    assert c.exec_mode == "volume" and c.max_excess_bps == 6
    assert c.volume_narrow_bps == 1.5 and c.volume_max_cost_usd == 1
    assert c.slipgate_enabled
    # a narrowing that would eat the whole band is refused, nothing saved
    with pytest.raises(RuntimeError):
        club.save_config([("execution", "volume_narrow_bps", "9")], pair)
    assert club.cfg_values(pair)["vol_narrow"] == 1.5


def test_each_execution_mode_learns_its_own_slippage(tmp_path):
    from entropy_arb.engine import Engine
    from entropy_arb.slipgate import slip_file_name
    assert slip_file_name("lighter-rh", "SNDK", "simultaneous") == \
        "slip_lighter-rh_SNDK.json"
    assert slip_file_name("lighter-rh", "SNDK", "entropy_first") == \
        "slip_lighter-rh_SNDK_entropy_first.json"
    eng = _arb_engine(tmp_path)
    for _ in range(5):
        eng.slip.add("entropy", 9.0)
    eng.cfg.exec_mode = "entropy_first"
    fresh = Engine(eng.cfg)
    assert fresh.slip.recent("entropy") == []      # new mode, clean history
    eng.cfg.exec_mode = "simultaneous"
    back = Engine(eng.cfg)
    assert len(back.slip.recent("entropy")) == 5   # old history kept
