"""Strategy 3 «Объём», strategy names, open/close journal tags, period
totals, the default fee, and the Hyperliquid request purchase."""
import asyncio
import csv
import os
import subprocess
import sys
import time
import types

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb import journal  # noqa: E402
from entropy_arb.strategy import (VOLUME_MAX_CHARGE_BPS, VolumeCost,  # noqa: E402
                                  strategy_number, strategy_title)
from test_guard import _arb_engine  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _signal(eng):
    eng._scan(time.time())
    best = eng._scan(time.time())
    assert best is not None
    return best


def _trade(eng):
    async def go():
        best = _signal(eng)
        await eng._execute(*best)
        return best
    return asyncio.run(go())


def _volume_engine(tmp_path, cost_trades=0, fill=-0.01, notional=20.0):
    eng = _arb_engine(tmp_path)
    eng.cfg.exec_mode = "volume"
    eng.cfg.volume_narrow_bps = 1.0
    eng.cfg.volume_max_cost_usd = 2.0
    eng.vol_cost = VolumeCost()
    for _ in range(cost_trades):
        eng.vol_cost.add(notional, fill)
    return eng


# ---------------------------------------------------------------- names

def test_strategy_names_and_numbers():
    assert strategy_title("simultaneous") == "Стратегия 1 · Обе ноги сразу"
    assert strategy_title("entropy_first") == "Стратегия 2 · Сначала Entropy"
    assert strategy_title("volume") == "Стратегия 3 · Объём"
    assert strategy_number("volume") == 3


def test_dashboard_and_telegram_show_the_strategy(tmp_path):
    from entropy_arb import telegram
    eng = _arb_engine(tmp_path)
    eng.cfg.exec_mode = "entropy_first"
    assert eng.strategy_name == "Стратегия 2 · Сначала Entropy"
    assert "Стратегия: №2 · Сначала Entropy" in telegram.status_text(eng)
    eng.record_only = False
    assert "№2 · Сначала Entropy" in telegram.start_text(eng)
    from entropy_arb.dashboard import Dashboard
    from rich.console import Console
    eng.markets_ready = True
    d = Dashboard(eng, None, "", lang="ru")
    con = Console(record=True, width=120)
    con.print(d._render())
    assert "Стратегия 2 · Сначала Entropy" in con.export_text()


def test_finish_text_names_strategy_and_volume_cost(tmp_path):
    from entropy_arb import telegram
    eng = _volume_engine(tmp_path, cost_trades=12, fill=-0.004)
    s = {"pnl": -0.05, "pnl_pct": -0.03, "turnover": 480, "trades": 12,
         "duration": 3600, "reason": "manual", "closed": True,
         "strategy": eng.strategy_name, "volume_cost": eng.volume_cost()}
    text = telegram.finish_text(eng, s)
    assert "Стратегия: №3 · Объём" in text
    assert "Цена объёма: стоит $2.00 за $10 000" in text


# ---------------------------------------------------------- volume cost

def test_volume_cost_needs_enough_trades_and_caps_the_charge():
    vc = VolumeCost(min_trades=3)
    vc.add(20.0, -0.004)
    vc.add(20.0, -0.004)
    assert vc.cost_bps() == (None, 2)              # too few: not measured
    vc.add(20.0, -0.004)
    cost, n = vc.cost_bps()
    assert n == 3 and cost == pytest.approx(2.0)   # $2 per $10k = 2 bps
    assert vc.charge_bps(2.0) == 0.0               # at the limit: nothing
    assert vc.charge_bps(1.5) == pytest.approx(0.5)
    for _ in range(3):
        vc.add(20.0, -0.2)                         # huge losses
    assert vc.charge_bps(0.0) == VOLUME_MAX_CHARGE_BPS
    earning = VolumeCost(min_trades=1)
    earning.add(20.0, +0.01)
    assert earning.cost_bps()[0] < 0 and earning.charge_bps(0.0) == 0.0


def test_volume_cost_old_trades_drop_out():
    vc = VolumeCost(min_trades=1, window_hours=1)
    vc.add(20.0, -1.0, ts=time.time() - 7200)
    vc.add(20.0, +0.002)
    cost, n = vc.cost_bps()
    assert n == 1 and cost == pytest.approx(-1.0)


def test_volume_cost_seeds_from_this_pairs_trades(tmp_path):
    p = str(tmp_path / "trades.csv")
    H = journal.TRADES_HEADER

    def row(**kw):
        r = {k: "" for k in H}
        r.update({"ts": f"{time.time():.0f}", "ok": "1", "strategy": "volume",
                  "buy_fill": "0.01",
                  "sell_fill": "0.01", "buy_venue": "ENTROPY",
                  "sell_venue": "RH", "buy_notional": "20",
                  "sell_notional": "20", "fill_edge_usd": "-0.004"})
        r.update(kw)
        return [r[k] for k in H]
    for _ in range(10):
        journal.append_row(p, H, row(symbol="SNDK", hedge_venue="lighter-rh"))
    journal.append_row(p, H, row(symbol="ANTH", hedge_venue="lighter-rh",
                                 fill_edge_usd="-5"))       # other pair
    journal.append_row(p, H, row(symbol="", hedge_venue="", ok="0"))  # failed
    journal.append_row(p, H, row(symbol="SNDK", hedge_venue="lighter-rh",
                                 strategy="simultaneous",
                                 fill_edge_usd="-5"))       # other strategy
    vc = VolumeCost()
    assert vc.seed_csv(p, "SNDK", "lighter-rh") == 10
    assert vc.cost_bps()[0] == pytest.approx(2.0)


# ------------------------------------------------------- volume strategy

def test_volume_mode_sends_entropy_first(tmp_path):
    eng = _volume_engine(tmp_path)
    buy, sell, plan = _trade(eng)
    (e_buy, _q, e_px, _), = eng.entropy.sent
    assert e_px == pytest.approx(plan.sell_limit * (1 - 5.0 / 1e4))
    assert len(eng.hedge.sent) == 1 and eng.ef_attempts == 1


def test_volume_mode_narrows_the_band(tmp_path):
    eng = _arb_engine(tmp_path)
    base = eng._eff_threshold(eng.hedge, eng.entropy)
    eng.cfg.exec_mode = "volume"
    eng.cfg.volume_narrow_bps = 1.5
    assert eng._eff_threshold(eng.hedge, eng.entropy) == \
        pytest.approx(base - 1.5)


def test_volume_charge_holds_back_opens_only(tmp_path):
    eng = _volume_engine(tmp_path, cost_trades=10, fill=-0.01)   # $5 / $10k
    base = eng._eff_threshold(eng.hedge, eng.entropy)
    assert eng.volume_charge_bps(eng.hedge, eng.entropy) == pytest.approx(3.0)
    eng.vol_cost = None
    no_charge = eng._eff_threshold(eng.hedge, eng.entropy)
    assert base == pytest.approx(no_charge + 3.0)
    eng = _volume_engine(tmp_path, cost_trades=10, fill=-0.01)
    eng.entropy.position = 0.5            # long Entropy: selling it closes
    assert eng.volume_charge_bps(eng.hedge, eng.entropy) == 0.0


def test_volume_trades_feed_the_cost(tmp_path):
    eng = _volume_engine(tmp_path)
    _trade(eng)
    assert len(eng.vol_cost.samples) == 1
    _ts, notional, fill = eng.vol_cost.samples[0]
    assert notional > 0 and fill == pytest.approx(eng.cmp_fill)


def test_other_strategies_have_no_volume_guard(tmp_path):
    eng = _arb_engine(tmp_path)
    assert eng.vol_cost is None and eng.volume_charge_bps() == 0.0
    assert eng.volume_narrow_bps() == 0.0 and eng.volume_cost() == (None, 0)


# --------------------------------------------------- open / close in journal

def test_trades_csv_tags_open_and_close(tmp_path):
    eng = _arb_engine(tmp_path)
    _trade(eng)                                   # flat -> sells Entropy: open
    eng.entropy.position = 1.0                    # pretend long Entropy
    eng.hedge.position = -1.0
    eng.entropy.last_traded_ts = eng.hedge.last_traded_ts = 0
    _trade(eng)                                   # selling Entropy now closes
    rows = list(csv.DictReader(open(eng.cfg.trades_csv)))
    assert [r["action"] for r in rows] == ["open", "close"]
    assert {r["strategy"] for r in rows} == {"simultaneous"}


# ------------------------------------------------------------- totals tool

def _write_logs(d):
    H = journal.TRADES_HEADER
    now = time.time()

    def trade(ts, fill, sym="SNDK", ven="lighter-rh", action="open"):
        r = {k: "" for k in H}
        r.update({"ts": f"{ts:.0f}", "ok": "1", "qty": "0.01",
                  "buy_fill": "0.01", "sell_fill": "0.01",
                  "buy_venue": "ENTROPY", "sell_venue": "RH",
                  "buy_notional": "20", "sell_notional": "20",
                  "buy_avg_px": "2000", "sell_avg_px": "2000",
                  "exp_edge_usd": "0.01", "fill_edge_usd": str(fill),
                  "symbol": sym, "hedge_venue": ven, "action": action,
                  "strategy": "volume"})
        journal.append_row(os.path.join(d, "trades.csv"), H, [r[k] for k in H])
    trade(now - 3600, -0.004)
    trade(now - 3600, +0.010, action="close")
    trade(now - 20 * 86400, -0.020)               # inside the month only
    trade(now - 3600, -0.002, sym="ANTH")
    S = journal.SESSIONS_HEADER
    s = {k: "" for k in S}
    s.update({"end_ts": f"{now - 1800:.0f}", "symbol": "SNDK",
              "hedge_venue": "lighter-rh", "pnl_usd": "-0.05",
              "funding_entropy_usd": "-0.001", "funding_hedge_usd": "0.002",
              "fees_est_usd": "0.003"})
    journal.append_row(os.path.join(d, "sessions.csv"), S, [s[k] for k in S])


def _totals(*args):
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools",
                                                     "totals.py"), *args],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_totals_per_period_and_pair(tmp_path):
    _write_logs(str(tmp_path))
    out = _totals("--dir", str(tmp_path), "--symbol", "SNDK",
                  "--hedge-venue", "lighter-rh")
    week = out.split("── Неделя ──")[1].split("── Месяц ──")[0]
    month = out.split("── Месяц ──")[1].split("── Всё время ──")[0]
    assert "Сделок: 2 (открытий 1, закрытий 1)" in week
    assert "Entropy     $40.00" in week and "Lighter RH  $40.00" in week
    assert "заработано +$0.0100 в 1 · потеряно -$0.0040 в 1" in week
    assert "Итог по сделкам: +$0.0060" in week
    assert "Результат по балансу (сессий: 1): -$0.0500" in week
    assert "Сделок: 3" in month
    assert "ANTH" not in out


def test_totals_all_pairs_lists_each_pair(tmp_path):
    _write_logs(str(tmp_path))
    out = _totals("--dir", str(tmp_path))
    assert "все пары" in out
    assert "ANTH ↔ Lighter RH: сделок 1" in out
    assert "SNDK ↔ Lighter RH: сделок 2" in out


def test_totals_without_trades(tmp_path):
    assert "Сделок пока нет" in _totals("--dir", str(tmp_path))


# ------------------------------------------------------------ fee default

def test_new_pairs_do_not_count_the_entropy_fee(tmp_path, monkeypatch):
    import club
    monkeypatch.chdir(tmp_path)
    ex = open(os.path.join(ROOT, "config.example.yaml"),
              encoding="utf-8").read()
    (tmp_path / "config.example.yaml").write_text(ex)
    (tmp_path / "config.yaml").write_text(ex)
    monkeypatch.setattr(club, "find_bots", lambda: [])
    club.ensure_ticker_files()
    for pair in (("lighter-rh", "SNDK"), ("lighter-rh", "ANTH"),
                 ("lighter", "NBIS")):
        assert club.cfg_values(pair)["fee_entropy"] == 0.0
    assert club.fee_mode_label(0.0).startswith("Не учитывать")
    assert club.fee_mode_label(0.17).startswith("Учитывать остаток")
    assert club.fee_mode_label(0.86) == "Учитывать полностью"
    assert club.fee_mode_label(0.5) == "своё значение"


# ------------------------------------------------- Hyperliquid requests

def test_request_purchase_signs_reserve_request_weight(monkeypatch):
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import hl_requests
    signed = {}

    def sign_l1_action(wallet, action, vault, nonce, expires, mainnet):
        signed.update(action=action, mainnet=mainnet, vault=vault)
        return {"r": "0x1", "s": "0x2", "v": 27}
    signing = types.SimpleNamespace(sign_l1_action=sign_l1_action)
    monkeypatch.setitem(sys.modules, "hyperliquid",
                        types.ModuleType("hyperliquid"))
    monkeypatch.setitem(sys.modules, "hyperliquid.utils",
                        types.SimpleNamespace(signing=signing))
    monkeypatch.setitem(sys.modules, "hyperliquid.utils.signing", signing)
    acct = types.SimpleNamespace(from_key=lambda k: "wallet")
    monkeypatch.setitem(sys.modules, "eth_account",
                        types.SimpleNamespace(Account=acct))
    sent = {}

    def post(path, payload):
        sent.update(path=path, payload=payload)
        return {"status": "ok"}
    monkeypatch.setattr(hl_requests, "post", post)
    assert hl_requests.buy("0xkey", 2000) == {"status": "ok"}
    assert signed["action"] == {"type": "reserveRequestWeight", "weight": 2000}
    assert signed["mainnet"] is True and signed["vault"] is None
    assert sent["path"] == "/exchange"
    assert sent["payload"]["action"]["weight"] == 2000


# ----------------------------------------------------------- log rotation

def test_engine_log_is_size_capped(tmp_path):
    import logging
    import logging.handlers
    import main
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        main.setup_logging("INFO", log_file=str(tmp_path / "engine.log"))
        added = [h for h in root.handlers if h not in before]
        assert isinstance(added[0], logging.handlers.RotatingFileHandler)
        assert added[0].maxBytes == main.LOG_MAX_BYTES > 0
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()


def test_stderr_log_is_trimmed(tmp_path, monkeypatch):
    import club
    p = tmp_path / "stderr.log"
    p.write_text("old line\n" * 2000 + "last line\n")
    club.trim_log(str(p), max_bytes=1000, keep_bytes=200)
    text = p.read_text()
    assert len(text) <= 200 and text.endswith("last line\n")
    assert text.startswith("old line") or text.startswith("last line")
