"""Dashboard rendering: key numbers appear, no crashes on empty state.

Run:  python3 -m pytest tests/  (or  python3 tests/test_dashboard.py)
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rich.console import Console  # noqa: E402

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.config import load_config  # noqa: E402
from entropy_arb.dashboard import BufferLogHandler, Dashboard  # noqa: E402
from entropy_arb.engine import Engine  # noqa: E402

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def make_cfg():
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write("""
thresholds:
  midline_bps: 2.0
  upper_bps: 4.0
  lower_bps: 3.0
""")
    f.close()
    return load_config(f.name, NO_ENV,
                       symbol="SNDK", hedge_venue="lighter-rh")


class StubVenue:
    def __init__(self, key, label):
        self.key, self.name = key, label
        self.cap_usd, self.fee_bps = 1000.0, 0.0
        self.size_decimals, self.min_base, self.min_quote = 4, 1e-4, 10.0
        self.position, self.cash, self.volume_usd = 0.0, 0.0, 0.0
        self.equity = self.free = self.start_equity = None
        self.orders_per_min = 30
        self.last_traded_ts = 0.0
        self.book = OrderBook()

    def set_book(self, bid, ask):
        self.book.apply_hl([[{"px": str(bid), "sz": "10"}],
                            [{"px": str(ask), "sz": "10"}]])


def render(eng, lang="en") -> str:
    dash = Dashboard(eng, BufferLogHandler(), "logs/engine.log", lang=lang)
    console = Console(record=True, width=120, force_terminal=True)
    console.print(dash._safe_render())
    return console.export_text()


def make_engine():
    eng = Engine(make_cfg())
    eng.entropy = StubVenue("entropy", "ENTROPY")
    eng.hedge = StubVenue("hedge", "RH")
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng.markets_ready = True
    eng.risk_ready = True   # loss limit armed (the risk loop is not run here)
    return eng


def test_renders_before_markets_resolve():
    eng = Engine(make_cfg())
    out = render(eng)
    assert "resolving markets" in out


def _session(eng, base=130.0, now=130.42):
    from entropy_arb.risk import Sample
    eng.session_base_total = base
    eng.risk_last_sample = Sample(now, {"entropy": 1.0, "hedge": 1.0}, 1.0)


def test_renders_key_numbers():
    eng = make_engine()
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)
    eng.entropy.position, eng.hedge.position = -0.2, 0.2
    eng.entropy.equity, eng.hedge.equity = 64.10, 66.32
    eng.entropy.volume_usd, eng.hedge.volume_usd = 620.0, 620.0
    eng.trades = 14
    _session(eng)
    out = render(eng)
    for needle in ("SNDK · Entropy ↔ Lighter RH", "TRADING",
                   "WAITING FOR SIGNAL", "session result, $", "+0.42",
                   "Turnover this session", "$1,240.00", "Trades", "14",
                   "▼ SHORT 0.2 SNDK", "▲ LONG 0.2 SNDK", "$64.10",
                   "$66.32", "Total $130.42 · at start $130.00",
                   "Loss stop: off"):
        assert needle in out, f"{needle!r} missing from render"
    assert "render error" not in out
    assert "⚠" not in out                    # all fine: no alerts
    # the clutter is gone: no premium table, no execution history
    for gone in ("mid premium", "hurdle", "executions", "events"):
        assert gone not in out, gone


def test_renders_in_russian():
    eng = make_engine()
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)
    eng.entropy.position, eng.hedge.position = -0.2, 0.2
    eng.trades = 3
    _session(eng, now=129.0)
    out = render(eng, lang="ru")
    for needle in ("РЕЖИМ ТОРГОВЛИ", "ЖДЁТ СИГНАЛА", "результат сессии, $",
                   "-1.00", "Оборот за сессию", "Сделок", "▼ ШОРТ",
                   "▲ ЛОНГ", "баланс", "Стоп по убытку: выключен"):
        assert needle in out, f"{needle!r} missing from ru render"
    out_en = render(eng, lang="en")
    assert "TRADING" in out_en and "РЕЖИМ" not in out_en


def test_big_digits_and_colours():
    from entropy_arb.dashboard import big_text
    rows = big_text("+0.42")
    assert len(rows) == 3 and all(rows)
    eng = make_engine()
    eng.entropy.set_book(100, 100.02)
    eng.hedge.set_book(100, 100.02)
    _session(eng, now=129.0)
    dash = Dashboard(eng, BufferLogHandler(), "x", lang="en")
    blk = dash._result_block()
    console = Console(record=True, width=80, force_terminal=True)
    console.print(blk)
    assert "\x1b[1;31m" in console.export_text(styles=True)   # red when < 0


def test_states_in_words():
    from entropy_arb.risk import LossGuard
    eng = make_engine()
    eng.entropy.set_book(100, 100.02)
    eng.hedge.set_book(100, 100.02)
    d = Dashboard(eng, BufferLogHandler(), "x", lang="ru")
    eng.risk_ready = False
    assert d.state()[0] == "ПОДГОТОВКА"
    eng.risk_ready, eng.risk_blind = True, True
    assert d.state()[0] == "ПАУЗА: БАЛАНС НЕ ЧИТАЕТСЯ"
    eng.risk_blind = False
    eng.halted, eng.halt_reason = True, "loss limit"
    assert d.state()[0] == "СТОП ПО УБЫТКУ"
    eng.halt_reason = "3 consecutive execution errors"
    assert d.state()[0] == "АВАРИЙНЫЙ СТОП"
    eng.halted = False
    eng.flattening = True
    assert d.state()[0] == "ЗАКРЫВАЕТ ПОЗИЦИИ"
    eng.stopping = True
    assert "ОСТАНАВЛИВАЕТСЯ" in d.state()[0]
    eng.stopping = eng.flattening = False
    assert d.state()[0] == "ЖДЁТ СИГНАЛА"
    # loss stop line once the session baseline is known
    eng.cfg.max_loss_pct = 2.0
    eng.risk_guard = LossGuard(130.0, 2.0, 10.0)
    assert "при −$2.60 (2% от $130.00)" in d._stop_line().plain


def test_alerts_only_when_something_is_wrong():
    eng = make_engine()
    eng.entropy.set_book(100, 100.02)
    eng.hedge.set_book(100, 100.02)
    buf = BufferLogHandler()
    d = Dashboard(eng, buf, "x", lang="ru")
    assert d.alerts() == []
    eng.entropy.position = 0.3                      # one leg only
    eng.consec_errors = 2
    eng.flatten_failed = True
    buf.last_error = (time.time(), "[RH] buy leg: rejected")
    al = " | ".join(d.alerts())
    for needle in ("ноги не сбалансированы", "ошибок подряд: 2",
                   "ПОЗИЦИЯ НЕ ЗАКРЫЛАСЬ", "последняя ошибка: [RH] buy leg"):
        assert needle in al, needle
    buf.last_error = (time.time() - 3600, "old")    # stale: not shown
    assert "последняя ошибка" not in " | ".join(d.alerts())


def test_renders_record_only_and_empty_books():
    eng = Engine(make_cfg(), record_only=True)
    eng.entropy = StubVenue("entropy", "ENTROPY")
    eng.hedge = StubVenue("hedge", "MAIN")
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng.markets_ready = True
    out = render(eng, lang="ru")             # books empty: no crash
    assert "ТЕСТОВАЯ ЗАПИСЬ" in out and "ИДЁТ ЗАПИСЬ РЫНКА" in out
    assert "render error" not in out


def test_live_without_balances_yet():
    eng = make_engine()
    out = render(eng, lang="ru")
    assert "читаю балансы бирж" in out and "render error" not in out
