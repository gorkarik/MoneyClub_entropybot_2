"""Hyperliquid request-budget guard and the unhedged remainder.

Over its address budget Hyperliquid accepts about one action per 10 s; an
arb leg sent into a busy slot is refused while the other venue's leg fills.
The engine must send nothing until the slot is free."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import entropy_arb.engine as engine_mod  # noqa: E402
from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.venue_hl import HLVenue  # noqa: E402
from test_risk import FakeVenue, make_engine  # noqa: E402


def _arb_engine(tmp):
    """Engine whose books show a large sell-entropy edge."""
    eng = make_engine(str(tmp))
    eng.cfg.premium_persist_sec = 0.0
    eng.entropy.kind = "hl"
    eng.hedge = FakeVenue("hedge", "RH", px=99.0)
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    return eng


def test_exhausted_budget_defers_both_legs_until_the_slot_is_free(tmp_path):
    async def go():
        eng = _arb_engine(tmp_path)
        eng._scan(time.time())                       # arms the signal
        assert eng._scan(time.time()) is not None    # normal: would trade
        eng.req_budget["entropy"] = {"used": 63866, "cap": 63525,
                                     "surplus": 0, "headroom": -341}
        eng._last_action_ts["entropy"] = time.time() - 3
        assert eng._scan(time.time()) is None        # slot busy: send nothing
        eng._last_action_ts["entropy"] = time.time() - 12
        assert eng._scan(time.time()) is not None    # slot free: one trade
    asyncio.run(go())


def test_budget_ok_imposes_no_pacing(tmp_path):
    async def go():
        eng = _arb_engine(tmp_path)
        eng.req_budget["entropy"] = {"headroom": 5000}
        eng._last_action_ts["entropy"] = time.time()
        eng._scan(time.time())
        assert eng._scan(time.time()) is not None
    asyncio.run(go())


def test_sends_count_down_and_refusal_switches_to_pacing(tmp_path):
    eng = _arb_engine(tmp_path)
    eng.req_budget["entropy"] = {"headroom": 3}
    eng._record_send(eng.entropy)
    eng._record_send(eng.entropy)
    assert eng.req_budget["entropy"]["headroom"] == 1
    assert eng.req_limited(eng.entropy)
    assert eng._req_slot_wait(eng.entropy) > 10
    eng2 = _arb_engine(tmp_path)
    eng2._on_rate_limited(eng2.entropy)               # budget unknown before
    assert eng2.req_limited(eng2.entropy)
    eng2._on_rate_limited(eng2.hedge)                 # Lighter: no budget
    assert not eng2.req_limited(eng2.hedge)


def test_hl_budget_read_parses_user_rate_limit():
    v = HLVenue.__new__(HLVenue)

    class Acc:
        query_address = "0xabc"
    v.account = Acc()

    async def info(payload):
        assert payload == {"type": "userRateLimit", "user": "0xabc"}
        return {"cumVlm": "53525.0", "nRequestsUsed": 63866,
                "nRequestsCap": 63525, "nRequestsSurplus": 0}
    v._info = info
    b = asyncio.run(v.fetch_request_budget())
    assert b["headroom"] == -341 and b["cum_vlm"] == 53525.0


def test_unhedged_remainder_below_minimum_is_not_hedgeable(tmp_path):
    eng = make_engine(str(tmp_path), epos=0.015, hpos=0.0)     # ~$1.50
    net, usd, hedgeable = eng.unhedged()
    assert abs(net - 0.015) < 1e-12 and abs(usd - 1.5) < 0.01
    assert not hedgeable
    eng._note_unhedged()
    assert abs(eng.max_unhedged_usd - 1.5) < 0.01
    eng2 = make_engine(str(tmp_path), epos=0.2, hpos=0.0)      # ~$20
    assert eng2.unhedged()[2]
    eng3 = make_engine(str(tmp_path), epos=0.2, hpos=-0.2)
    assert eng3.unhedged() is None


def test_dashboard_shows_remainder_as_note_not_alarm(tmp_path):
    from entropy_arb.dashboard import Dashboard
    eng = make_engine(str(tmp_path), epos=0.015, hpos=0.0)
    eng._exec_tasks = set()
    d = Dashboard.__new__(Dashboard)
    d.eng, d.lang = eng, "ru"
    d.log_buffer = None
    assert not any("перекос" in a for a in d.alerts())
    notes = d.notes()
    assert notes and "закроется при Стопе" in notes[0]


def test_budget_constants_match_exchange_pacing():
    # Hyperliquid: one action per 10 s when over the cap
    assert engine_mod.REQ_SLOW_GAP_SEC > 10.0
    assert engine_mod.REQ_BUDGET_MIN_HEADROOM >= 1


def test_report_key_metrics_block(tmp_path, capsys, monkeypatch):
    from entropy_arb import journal
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
    import report
    d = tmp_path / "logs"
    d.mkdir()
    now = time.time()

    def trade(exp, fill, bfill, sfill, bst="filled", sst="filled"):
        row = {k: "" for k in journal.TRADES_HEADER}
        row.update(ts=f"{now:.0f}", direction="sell_entropy", buy_venue="RH",
                   sell_venue="ENTROPY", qty="0.2", buy_notional="20",
                   sell_notional="20", exp_edge_usd=str(exp), ok="1",
                   buy_fill=str(bfill), sell_fill=str(sfill), buy_status=bst,
                   sell_status=sst, fill_edge_usd=str(fill), buy_fee_bps="0",
                   sell_fee_bps="0.86", symbol="SNDK")
        journal.append_row(str(d / "trades.csv"), journal.TRADES_HEADER,
                           [row[k] for k in journal.TRADES_HEADER])

    trade(0.02, -0.01, 0.2, 0.2)          # 10 bps expected, -5 bps got
    trade(0.02, 0.0, 0.2, 0.0, sst="send-failed")
    h = {k: "" for k in journal.HEDGES_HEADER}
    h.update(ts=f"{now:.0f}", reason="hedge", err="RATE_LIMITED: x",
             symbol="SNDK")
    journal.append_row(str(d / "hedges.csv"), journal.HEDGES_HEADER,
                       [h[k] for k in journal.HEDGES_HEADER])
    monkeypatch.setattr(sys, "argv", ["report.py", "--dir", str(d), "--config",
                                      "/nonexistent", "--hours", "0",
                                      "--summary", "--symbol", "SNDK"])
    report.main()
    out = capsys.readouterr().out
    assert "потеря на исполнении 15.00 bps" in out
    assert "только одна нога: 1 из 2" in out
    assert "хеджей): 1" in out and "1 в сделках, 1 в выравниваниях" in out


def test_recording_dashboard_shows_premium_and_hour_range(tmp_path):
    from rich.console import Console
    from entropy_arb.dashboard import Dashboard
    eng = make_engine(str(tmp_path))
    eng.record_only = True
    eng.recorder = None
    eng.markets_ready = True
    eng.hedge = FakeVenue("hedge", "RH", px=100.1)
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    now = time.time()
    for i, b in enumerate([-12.0, -9.0, -8.0, -5.0]):
        eng._prem_hist.append((now - 600 + i, b))
    st = eng.premium_stats()
    assert st[0] == -12.0 and st[2] == -5.0 and st[1] == -8.5
    d = Dashboard.__new__(Dashboard)
    d.eng, d.lang, d.log_buffer = eng, "ru", None
    con = Console(record=True, width=100)
    con.print(d._render())
    out = con.export_text()
    assert "Премия сейчас" in out and "медиана -8.50" in out
