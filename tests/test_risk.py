"""Session loss limit, flatten (loss stop and Stop), session summary,
journals, the AI report and the menu helpers.

Run:  python3 -m pytest tests/
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import entropy_arb.engine as engine_mod  # noqa: E402
from entropy_arb import journal  # noqa: E402
from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.config import ConfigError, load_config  # noqa: E402
from entropy_arb.engine import Engine  # noqa: E402
from entropy_arb.risk import (NOISE, OK, PENDING, TRIP, WAIT,  # noqa: E402
                              LossGuard, Sample)

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def make_cfg(tmp, extra="", symbol="SNDK"):
    path = os.path.join(tmp, "config.yaml")
    with open(path, "w") as fh:
        fh.write(f"""
thresholds:
  midline_bps: -7.0
  upper_bps: 4.0
  lower_bps: 4.5
logging:
  trades_csv: {os.path.join(tmp, "logs", "trades.csv")}
risk:
{extra}""")
    return load_config(path, NO_ENV, symbol=symbol, hedge_venue="lighter-rh")


# ---------------------------------------------------------------- config

def test_old_config_without_risk_section_gets_defaults(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("thresholds:\n  midline_bps: -7\n  upper_bps: 4\n"
                 "  lower_bps: 4.5\n")
    c = load_config(str(p), NO_ENV, symbol="SNDK", hedge_venue="lighter-rh")
    assert c.max_loss_pct == 0.0          # off unless the user sets it
    assert c.flatten_on_halt is False


def test_loss_pct_is_percent_and_validated(tmp_path):
    c = make_cfg(str(tmp_path), "  max_loss_pct: 0.05\n")
    assert c.max_loss_pct == 0.05            # 0.05 %, not a fraction
    with pytest.raises(ConfigError):
        make_cfg(str(tmp_path), "  max_loss_pct: 150\n")
    with pytest.raises(ConfigError):
        make_cfg(str(tmp_path), "  bogus_key: 1\n")


# ------------------------------------------------------------ pure rules

def S(total, t, wall):
    return Sample(total, {"entropy": t, "hedge": t}, wall)


def test_guard_single_dip_is_noise_not_trip():
    g = LossGuard(130.0, 2.0, 10.0)          # limit $2.60
    assert g.observe(S(129.0, 1, 0)) == OK
    assert g.observe(S(127.0, 2, 1)) == PENDING
    assert g.observe(S(128.0, 3, 12)) == NOISE   # back under: reset
    assert g.pending is None
    assert g.observe(S(127.0, 4, 13)) == PENDING  # starts over


def test_guard_trip_needs_interval_and_newer_data():
    g = LossGuard(130.0, 2.0, 10.0)
    assert g.observe(S(127.0, 1, 0)) == PENDING
    assert g.observe(S(127.0, 2, 5)) == WAIT     # too soon
    stale = Sample(127.0, {"entropy": 1, "hedge": 9}, 20)
    assert g.observe(stale) == WAIT              # entropy point not newer
    assert g.observe(S(127.0, 3, 20)) == TRIP


# ------------------------------------------------------- engine (fakes)

class FakeVenue:
    def __init__(self, key, name, equity=65.0, pos=0.0, px=100.0,
                 min_quote=10.0):
        self.key, self.name = key, name
        self.cap_usd, self.fee_bps = 1000.0, 0.0
        self.size_decimals, self.min_base, self.min_quote = 4, 1e-4, min_quote
        self.position, self.cash = pos, 0.0
        self.volume_usd = 0.0
        self.orders_per_min = 30
        self.last_traded_ts = 0.0
        self.book = OrderBook()
        self.book.apply_hl([[{"px": str(px - 0.01), "sz": "50"}],
                            [{"px": str(px + 0.01), "sz": "50"}]])
        self.exch_pos = pos
        self.equity = equity
        self.equity_fail = False
        self.close_fails = False
        self.sent = []

    def ready_to_trade(self):
        return True

    def px_round(self, px, round_up):
        return px

    async def fetch_position(self):
        return self.exch_pos

    async def fetch_risk_equity(self):
        if self.equity_fail:
            raise RuntimeError("api down")
        return self.equity, time.time()

    async def send_taker(self, *, is_buy, qty, limit_px, reduce_only=False):
        self.sent.append((is_buy, qty, limit_px, reduce_only))
        if self.close_fails:
            return {"status": "send-failed", "filled_base": 0.0,
                    "avg_px": None, "err": "rejected", "unresolved": False}
        fill = min(qty, abs(self.exch_pos)) if reduce_only else qty
        self.exch_pos += fill if is_buy else -fill
        return {"status": "filled", "filled_base": fill, "avg_px": limit_px,
                "err": None, "unresolved": False}


def make_engine(tmp, epos=0.0, hpos=0.0, extra="", symbol="SNDK"):
    cfg = make_cfg(tmp, extra, symbol=symbol)
    cfg.risk_check_sec = 0.05
    cfg.risk_confirm_interval_sec = 0.1
    eng = Engine(cfg)
    eng.entropy = FakeVenue("entropy", "ENTROPY", pos=epos)
    eng.hedge = FakeVenue("hedge", "RH", pos=hpos)
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng._step, eng._min_base, eng._min_notional = 1e-4, 1e-4, 10.0
    eng.RECONCILE_GRACE_SEC = 0.0
    return eng


@pytest.fixture(autouse=True)
def no_min_gap(monkeypatch):
    monkeypatch.setattr(engine_mod, "RISK_MIN_GAP_SEC", 0.0)


def test_session_baseline_and_limit(tmp_path):
    eng = make_engine(str(tmp_path), extra="  max_loss_pct: 2\n")
    assert eng.risk_ready is False            # trading waits for the base
    assert asyncio.run(eng._risk_init()) is True
    assert eng.risk_ready and not eng.halted
    assert eng.session_base_total == 130.0
    assert abs(eng.risk_guard.limit_usd - 2.6) < 1e-9
    assert eng.session_result() == 0.0


def test_limit_off_by_default_never_blocks_trading(tmp_path):
    eng = make_engine(str(tmp_path))
    assert eng.cfg.max_loss_pct == 0 and eng.risk_ready  # trades at once
    assert asyncio.run(eng._risk_init()) is True
    assert eng.risk_guard is None                         # no stop
    assert eng.session_base_total == 130.0                # PnL still known


def test_each_start_takes_a_fresh_baseline(tmp_path):
    eng = make_engine(str(tmp_path), extra="  max_loss_pct: 2\n")
    asyncio.run(eng._risk_init())
    eng2 = make_engine(str(tmp_path), extra="  max_loss_pct: 2\n")
    eng2.entropy.equity = 80.0                # deposit between sessions
    asyncio.run(eng2._risk_init())
    assert eng2.session_base_total == 145.0   # no reset button needed
    assert eng2.session_result() == 0.0
    assert not os.path.exists(os.path.join(str(tmp_path), "risk_state.json"))


def test_flatten_closes_both_legs_reduce_only(tmp_path, caplog):
    eng = make_engine(str(tmp_path), epos=0.2, hpos=-0.2)
    with caplog.at_level(logging.WARNING):
        ok = asyncio.run(eng._flatten("loss limit"))
    assert ok
    assert eng.entropy.exch_pos == 0 and eng.hedge.exch_pos == 0
    assert eng.entropy.position == 0 and eng.hedge.position == 0
    (b, q, px, ro), = eng.entropy.sent
    assert b is False and ro is True and q == 0.2      # sell the long
    assert px < 99.99                                  # wide slippage cap
    (b, q, px, ro), = eng.hedge.sent
    assert b is True and ro is True                    # buy back the short
    assert "both legs CLOSED" in caplog.text


def test_flatten_one_leg_fails_reports_naked_leg(tmp_path, caplog):
    eng = make_engine(str(tmp_path), epos=0.2, hpos=-0.2)
    eng.hedge.close_fails = True
    with caplog.at_level(logging.CRITICAL):
        ok = asyncio.run(eng._flatten("loss limit"))
    assert not ok
    assert eng.entropy.exch_pos == 0                   # one leg did close
    assert len(eng.hedge.sent) == eng.cfg.flatten_attempts
    assert "FLATTEN FAILED" in caplog.text
    assert "RH SHORT 0.2" in caplog.text               # venue, side, size


def test_flatten_never_sends_blind_when_position_unreadable(tmp_path, caplog):
    eng = make_engine(str(tmp_path), epos=0.2, hpos=-0.2)

    async def boom():
        raise RuntimeError("down")
    eng.hedge.fetch_position = boom
    with caplog.at_level(logging.CRITICAL):
        ok = asyncio.run(eng._flatten("loss limit"))
    assert not ok and eng.hedge.sent == []
    assert "позиция неизвестна" in caplog.text


def test_flatten_reports_sub_minimum_remainder(tmp_path, caplog):
    eng = make_engine(str(tmp_path), epos=0.05)        # $5 < $10 minimum
    eng.entropy.close_fails = True                     # venue rejects it
    with caplog.at_level(logging.CRITICAL):
        ok = asyncio.run(eng._flatten("loss limit"))
    assert ok                                          # not an error...
    assert len(eng.entropy.sent) == 1                  # ...but one try made
    assert "below the venue's minimum" in caplog.text and "ENTROPY LONG 0.05" \
        in caplog.text                                 # and shown with size


def run_loop(eng, seconds):
    async def go():
        t = asyncio.create_task(eng._risk_loop())
        await asyncio.sleep(seconds)
        eng.request_stop()
        await asyncio.wait_for(t, 2)
        if eng._exec_tasks:
            await asyncio.wait(eng._exec_tasks, timeout=2)
    asyncio.run(go())


def test_loop_trips_and_flattens_on_confirmed_loss(tmp_path, caplog):
    eng = make_engine(str(tmp_path), epos=0.2, hpos=-0.2,
                      extra="  max_loss_pct: 2\n")
    asyncio.run(eng._risk_init())
    eng.entropy.equity = 62.0                          # -$3 > $2.60
    with caplog.at_level(logging.WARNING):
        run_loop(eng, 0.6)
    assert eng.halted and eng.halt_reason == "loss limit"
    assert eng.entropy.exch_pos == 0 and eng.hedge.exch_pos == 0
    assert not eng.flatten_failed
    assert "confirming with a second reading" in caplog.text
    assert "СТОП ПО УБЫТКУ" in caplog.text


def test_loop_single_dip_does_not_trip(tmp_path, caplog):
    eng = make_engine(str(tmp_path), epos=0.2, hpos=-0.2,
                      extra="  max_loss_pct: 2\n")
    asyncio.run(eng._risk_init())

    reads = {"n": 0}
    orig = eng.entropy.fetch_risk_equity

    async def dip_once():
        reads["n"] += 1
        eng.entropy.equity = 62.0 if reads["n"] == 1 else 65.0
        return await orig()
    eng.entropy.fetch_risk_equity = dip_once
    with caplog.at_level(logging.WARNING):
        run_loop(eng, 0.5)
    assert not eng.halted and eng.entropy.sent == []
    assert "single dip" in caplog.text


def test_loop_misses_pause_trading_then_resume(tmp_path, caplog):
    eng = make_engine(str(tmp_path),
                      extra="  max_loss_pct: 2\n  max_equity_misses: 3\n")
    asyncio.run(eng._risk_init())
    eng.hedge.equity_fail = True

    async def go():
        t = asyncio.create_task(eng._risk_loop())
        await asyncio.sleep(0.5)
        assert eng.risk_blind and not eng.halted       # paused, no flatten
        eng.hedge.equity_fail = False
        await asyncio.sleep(0.3)
        assert not eng.risk_blind                      # resumed
        eng.request_stop()
        await asyncio.wait_for(t, 2)
    with caplog.at_level(logging.WARNING):
        asyncio.run(go())
    assert eng.entropy.sent == [] and eng.hedge.sent == []
    assert "trading PAUSED" in caplog.text and "RESUMED" in caplog.text


def test_unarmed_or_blind_engine_does_not_trade(tmp_path):
    eng = make_engine(str(tmp_path), extra="  max_loss_pct: 2\n")
    eng.entropy.book.apply_hl([[{"px": "101", "sz": "50"}],
                               [{"px": "101.02", "sz": "50"}]])
    called = []
    eng._scan = lambda now: called.append(1)
    asyncio.run(eng._evaluate())                       # risk not ready
    eng.risk_ready, eng.risk_blind = True, True
    asyncio.run(eng._evaluate())                       # equity blind
    assert called == []
    eng.risk_blind = False
    asyncio.run(eng._evaluate())
    assert called == [1]


# ------------------------------------------------------------------ menu

def test_menu_adds_missing_risk_section_to_old_config():
    import club
    old = ("thresholds:\n  midline_bps: -7.0  # центр\n"
           "recorder:\n  enabled: true\n")
    new = club._set_yaml_value(old, "risk", "max_loss_pct", "0.05")
    import yaml
    d = yaml.safe_load(new)
    assert d["risk"] == {"max_loss_pct": 0.05}
    assert "# центр" in new                            # comments kept
    new2 = club._set_yaml_value(new, "risk", "max_loss_pct", "2")
    assert yaml.safe_load(new2)["risk"] == {"max_loss_pct": 2}
    new3 = club._set_yaml_value(new2, "risk", "flatten_on_halt", "true")
    assert yaml.safe_load(new3)["risk"]["flatten_on_halt"] is True


def test_menu_detects_halt_kind(tmp_path):
    import club
    os.makedirs(tmp_path / "logs")
    log = tmp_path / "logs" / "engine.log"
    b = club.Bot.__new__(club.Bot)
    b.cwd = str(tmp_path)
    log.write_text("x LIVE — real orders will be sent\n")
    assert club.halt_kind(b) is None
    log.write_text("x LIVE — real orders will be sent\n"
                   "y HALTED (loss limit) / СТОП ПО УБЫТКУ: loss $3\n")
    assert club.halt_kind(b) == "loss"
    log.write_text("x LIVE — real orders will be sent\n"
                   "y HALTED after 3 consecutive execution problems\n")
    assert club.halt_kind(b) == "errors"
    log.write_text("y СТОП ПО УБЫТКУ\nx LIVE — real orders will be sent\n")
    assert club.halt_kind(b) is None                   # older run only


def _plan():
    from entropy_arb.book import ArbPlan
    return ArbPlan(qty=0.2, buy_limit=100.0, sell_limit=100.1,
                   buy_notional=20.0, sell_notional=20.02, q_max=0.2,
                   q_max_notional=20.0, top_premium_bps=10.0,
                   marginal_premium_bps=10.0, buy_fee=0.0, sell_fee=0.0)


def _error_halt(tmp, flatten_on_halt):
    extra = f"  flatten_on_halt: {'true' if flatten_on_halt else 'false'}\n"
    eng = make_engine(tmp, epos=0.2, hpos=-0.2, extra=extra)
    eng.cfg.trades_csv = os.path.join(tmp, "trades.csv")
    eng.consec_errors = eng.cfg.max_consecutive_errors - 1
    eng.entropy.close_fails = eng.hedge.close_fails = True  # both legs fail

    async def go():
        await eng._execute(eng.hedge, eng.entropy, _plan())
        eng.entropy.close_fails = eng.hedge.close_fails = False
        if eng._exec_tasks:
            await asyncio.wait(eng._exec_tasks, timeout=2)
    asyncio.run(go())
    return eng


def test_error_halt_default_does_not_flatten(tmp_path):
    eng = _error_halt(str(tmp_path), flatten_on_halt=False)
    assert eng.halted and "execution errors" in eng.halt_reason
    assert eng.entropy.exch_pos == 0.2 and eng.hedge.exch_pos == -0.2


def test_error_halt_flattens_when_enabled(tmp_path):
    eng = _error_halt(str(tmp_path), flatten_on_halt=True)
    assert eng.halted
    assert eng.entropy.exch_pos == 0 and eng.hedge.exch_pos == 0


# ------------------------------------------------- stop, session, journals

def test_stop_closes_positions_and_writes_session(tmp_path, caplog):
    eng = make_engine(str(tmp_path), epos=0.2, hpos=-0.2,
                      extra="  max_loss_pct: 2\n")
    asyncio.run(eng._risk_init())
    journal.write_marker(eng.cfg.trades_csv, {"session_id": eng.session_id})
    eng.entropy.volume_usd = eng.hedge.volume_usd = 100.0
    eng.trades, eng.fees_usd = 5, 0.0086
    eng.entropy.equity = 64.5                           # -$0.50 session

    async def go():
        ok = await eng._flatten("stop")
        await eng._finish_session(ok)
        return ok
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(go()) is True
    assert eng.entropy.exch_pos == 0 and eng.hedge.exch_pos == 0
    row = journal.last_session(eng.cfg.trades_csv)
    assert row["symbol"] == "SNDK" and row["positions_closed"] == "1"
    assert float(row["pnl_usd"]) == -0.5
    assert float(row["start_equity"]) == 130.0
    assert float(row["turnover_usd"]) > 200.0          # + the closing fills
    assert row["stop_reason"] == "manual"
    assert journal.read_marker(eng.cfg.trades_csv) is None   # clean end
    assert "[STOP] / ОСТАНОВКА — both legs CLOSED" in caplog.text
    assert "[SESSION]" in caplog.text
    h = journal.read_rows(journal.path_in(eng.cfg.trades_csv, "hedges.csv"))
    assert {r["reason"] for r in h} == {"flatten:stop"} and len(h) == 2


def test_failed_close_at_stop_is_recorded(tmp_path):
    eng = make_engine(str(tmp_path), epos=0.2, hpos=-0.2)
    asyncio.run(eng._risk_init())
    journal.write_marker(eng.cfg.trades_csv, {"session_id": eng.session_id})
    eng.hedge.close_fails = True
    eng.halted, eng.halt_reason = True, "loss limit"

    async def go():
        ok = await eng._flatten("stop")
        await eng._finish_session(ok)
    asyncio.run(go())
    row = journal.last_session(eng.cfg.trades_csv)
    assert row["positions_closed"] == "0" and row["stop_reason"] == "loss_limit"
    # marker kept: the next Start must warn about the open position
    assert journal.read_marker(eng.cfg.trades_csv) is not None


def test_trades_csv_upgrade_keeps_old_rows(tmp_path):
    p = str(tmp_path / "trades.csv")
    with open(p, "w") as fh:
        fh.write(",".join(journal.LEGACY_TRADES_HEADER) + "\n")
        fh.write(",".join(["1"] * 20) + "\n")
    journal.append_row(p, journal.TRADES_HEADER,
                       ["2"] * len(journal.TRADES_HEADER))
    rows = journal.read_rows(p)
    assert len(rows) == 2 and rows[0]["ts"] == "1"
    assert rows[0]["buy_avg_px"] == "" and rows[1]["buy_avg_px"] == "2"
    # unknown header: moved aside, never overwritten
    q = str(tmp_path / "x.csv")
    with open(q, "w") as fh:
        fh.write("a,b\n1,2\n")
    journal.append_row(q, ["c"], ["3"])
    assert any(n.startswith("x.csv.old-") for n in os.listdir(tmp_path))


def test_trade_row_has_prices_fees_and_latency(tmp_path):
    eng = make_engine(str(tmp_path))
    eng.cfg.trades_csv = os.path.join(str(tmp_path), "logs", "trades.csv")
    eng.hedge.fee_bps, eng.entropy.fee_bps = 0.0, 0.86
    plan = _plan()
    plan.sell_fee = 0.86e-4
    asyncio.run(eng._execute(eng.hedge, eng.entropy, plan))
    r = journal.read_rows(eng.cfg.trades_csv)[-1]
    # the fake venue fills at the limit it was sent: plan limit ± 50 bps cap
    assert abs(float(r["buy_avg_px"]) - 100.0 * 1.005) < 1e-6
    assert abs(float(r["sell_avg_px"]) - 100.1 * 0.995) < 1e-6
    assert abs(float(r["buy_exp_px"]) - 100.0) < 1e-6      # 20.00 / 0.2
    assert r["sell_fee_bps"] == "0.860" and r["buy_fee_bps"] == "0.000"
    assert r["buy_exp_px"] and r["sell_exp_px"]
    assert r["buy_ms"] and r["sell_ms"] and r["leg_gap_ms"]
    assert r["session_id"] == eng.session_id
    assert eng.fees_usd > 0


def test_report_runs_on_old_and_new_rows(tmp_path):
    import subprocess
    logs = tmp_path / "logs"
    logs.mkdir()
    p = str(logs / "trades.csv")
    with open(p, "w") as fh:
        fh.write(",".join(journal.LEGACY_TRADES_HEADER) + "\n")
        fh.write(f"{time.time():.0f},sell_entropy,RH,ENTROPY,0.2,100,100.1,"
                 "20,20.02,0.01,0.02,-3,-7,0,1,0.2,0.2,filled,filled,0.005\n")
    (tmp_path / "c.yaml").write_text(
        "thresholds:\n  midline_bps: -7\n  upper_bps: 4\n  lower_bps: 4.5\n"
        "entropy:\n  taker_fee_bps: 0.86\n")
    tool = os.path.join(os.path.dirname(__file__), "..", "tools", "report.py")
    out = subprocess.run([sys.executable, tool, "--dir", str(logs),
                          "--config", str(tmp_path / "c.yaml"),
                          "--hours", "0"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    for needle in ("Итог сделок", "досчитанная комиссия Entropy",
                   "нет сделок нового формата", "По направлению",
                   "Вопрос для анализа"):
        assert needle in out.stdout, needle
    short = subprocess.run([sys.executable, tool, "--dir", str(logs),
                            "--config", str(tmp_path / "c.yaml"),
                            "--hours", "0", "--summary"],
                           capture_output=True, text=True)
    assert "Сделки бота за период: 1" in short.stdout


def test_menu_session_report_lines():
    import club
    row = {"symbol": "SNDK", "hedge_venue": "lighter-rh",
           "duration_sec": "3725", "start_equity": "130", "end_equity": "130.4",
           "pnl_usd": "0.4", "pnl_pct": "0.31", "turnover_usd": "1240",
           "trades": "14", "fees_est_usd": "0.0533",
           "pnl_bps_of_turnover": "3.2", "stop_reason": "manual",
           "positions_closed": "1"}
    txt = "\n".join(club.session_report_lines(row))
    for needle in ("SNDK", "Entropy ↔ Lighter RH", "1:02:05",
                   "$130.00 → $130.40", "+0.4000", "+0.31%", "$1,240.00",
                   "сделок: 14", "Комиссии (примерно): $0.0533",
                   "+3.20 bps", "вручную", "закрыты"):
        assert needle in txt, needle


def test_menu_warns_before_start_after_unclosed_session(tmp_path, monkeypatch):
    import club
    tcsv = str(tmp_path / "logs" / "trades.csv")
    monkeypatch.setattr(club, "cfg_values", lambda: {"trades_csv": tcsv})
    asked = []
    monkeypatch.setattr(club, "confirm",
                        lambda q, default=True: asked.append(q) or False)
    assert club.previous_session_ok() is True and asked == []   # clean
    journal.write_marker(tcsv, {"session_id": "x"})             # crash
    assert club.previous_session_ok() is False and len(asked) == 1
    journal.clear_marker(tcsv)
    journal.append_row(journal.path_in(tcsv, "sessions.csv"),
                       journal.SESSIONS_HEADER,
                       ["s"] + [""] * 16 + ["0"] + [""] * 6)    # not closed
    assert club.previous_session_ok() is False and len(asked) == 2


class LifecycleVenue(FakeVenue):
    """FakeVenue plus what Engine._run_inner touches."""
    kind = "lighter"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.free = None
        self.start_equity = None
        self.conf = type("C", (), {"symbol": "SNDK"})()

    async def load_market(self):
        pass

    def init_signer(self):
        pass

    def _query_address(self):
        return None

    def start_tasks(self, stop, notify, live):
        return []

    async def warm_http(self):
        pass

    async def fetch_equity(self):
        return self.equity, self.equity

    async def close(self):
        pass


def test_full_lifecycle_stop_closes_and_reports(tmp_path, monkeypatch, caplog):
    """Start → session baseline → Stop → both legs closed → sessions.csv
    row → marker removed → engine done."""
    cfg = make_cfg(str(tmp_path), "  max_loss_pct: 2\n")
    cfg.risk_check_sec = 0.05
    cfg.recorder_enabled = False
    from entropy_arb import config as config_mod
    monkeypatch.setattr(config_mod.Config, "creds_complete",
                        property(lambda self: True))
    eng = Engine(cfg)
    eng.RECONCILE_GRACE_SEC = 0.0
    venues = {"entropy": LifecycleVenue("entropy", "ENTROPY", pos=0.2),
              "hedge": LifecycleVenue("hedge", "RH", pos=-0.2)}
    monkeypatch.setattr(eng, "_make_venue",
                        lambda vc: venues[vc.key])

    async def go():
        t = asyncio.create_task(eng.run())
        for _ in range(100):                    # wait for the baseline
            await asyncio.sleep(0.05)
            if eng.session_base_total is not None:
                break
        assert eng.session_base_total == 130.0
        assert journal.read_marker(cfg.trades_csv) is not None
        venues["entropy"].equity = 64.0         # -$1 during the session
        eng.request_stop()
        await asyncio.wait_for(t, 15)
    with caplog.at_level(logging.WARNING):
        asyncio.run(go())
    assert eng.done
    assert venues["entropy"].exch_pos == 0 and venues["hedge"].exch_pos == 0
    row = journal.last_session(cfg.trades_csv)
    assert row is not None and row["positions_closed"] == "1"
    assert float(row["pnl_usd"]) == -1.0
    assert journal.read_marker(cfg.trades_csv) is None
    assert "closing positions before shutdown" in caplog.text
