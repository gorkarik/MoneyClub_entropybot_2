"""One Hyperliquid request budget when both legs trade from one address
(engine logic kept for any Hyperliquid hedge venue)."""
import asyncio
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import entropy_arb.engine as engine_mod  # noqa: E402
from test_risk import FakeVenue, make_engine  # noqa: E402


# ------------------------------------------- one address, one budget

def _shared_engine(tmp):
    eng = make_engine(str(tmp))
    eng.cfg.premium_persist_sec = 0.0
    eng.entropy.kind = "hl"
    eng.hedge = FakeVenue("hedge", "XYZ", px=99.0)
    eng.hedge.kind = "hl"
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng.hl_shared = True
    return eng


def test_shared_address_counts_one_budget(tmp_path):
    eng = _shared_engine(tmp_path)
    b = {"used": 100, "cap": 110, "surplus": 0, "headroom": 10}
    for k in eng._budget_keys(eng.entropy):
        eng.req_budget[k] = b
    assert eng.req_budget["hedge"] is eng.req_budget["entropy"]
    eng._record_send(eng.entropy)
    eng._record_send(eng.hedge)
    assert b["headroom"] == 8                    # 2 actions, counted once each
    assert eng._last_action_ts["entropy"] == eng._last_action_ts["hedge"]


def test_shared_exhausted_budget_blocks_arbitrage(tmp_path):
    async def go():
        eng = _shared_engine(tmp_path)
        eng._scan(time.time())                   # arms the signal
        assert eng._scan(time.time()) is not None
        b = {"headroom": 2}                      # room for 2, keep a spare
        eng.req_budget["entropy"] = eng.req_budget["hedge"] = b
        assert eng.shared_budget_blocks_arb(eng.hedge, eng.entropy)
        eng._last_action_ts = {}                 # even with a free slot
        assert eng._scan(time.time()) is None
        b["headroom"] = 3
        assert eng._scan(time.time()) is not None
    asyncio.run(go())


def test_separate_addresses_keep_separate_budgets(tmp_path):
    eng = _shared_engine(tmp_path)
    eng.hl_shared = False
    assert eng._budget_keys(eng.entropy) == ["entropy"]
    eng.req_budget["entropy"] = {"headroom": 0}
    assert not eng.shared_budget_blocks_arb(eng.hedge, eng.entropy)


def test_rate_limit_refusal_marks_both_legs_on_one_address(tmp_path):
    eng = _shared_engine(tmp_path)
    eng._on_rate_limited(eng.hedge)
    assert eng.req_limited(eng.entropy) and eng.req_limited(eng.hedge)


def test_dashboard_says_shared_budget_once(tmp_path):
    from entropy_arb.dashboard import BufferLogHandler, Dashboard
    eng = _shared_engine(tmp_path)
    b = {"headroom": -5}
    eng.req_budget["entropy"] = eng.req_budget["hedge"] = b
    d = Dashboard(eng, BufferLogHandler(), "x.log", lang="ru")
    alerts = [a for a in d.alerts() if "лимит запросов" in a.lower()]
    assert len(alerts) == 1 and "одном адресе" in alerts[0]


# ------------------------------------------------- quieter remainder log

def test_remainder_note_not_repeated_every_reconcile(tmp_path, caplog):
    async def go():
        eng = make_engine(str(tmp_path), epos=0.05, hpos=0.0)  # $5 < $10 min
        caplog.set_level(logging.WARNING, logger="engine")
        for _ in range(5):
            await eng._hedge(0.05)
        n1 = sum("below hedgeable minimum" in r.message for r in caplog.records)
        await eng._hedge(0.06)                   # remainder changed: log it
        n2 = sum("below hedgeable minimum" in r.message for r in caplog.records)
        eng._last_dust_log = (0.06, time.time() - engine_mod.DUST_LOG_SEC - 1)
        await eng._hedge(0.06)                   # same, but 5 minutes later
        n3 = sum("below hedgeable minimum" in r.message for r in caplog.records)
        return n1, n2, n3
    assert asyncio.run(go()) == (1, 2, 3)
