"""trade.xyz as the hedge venue: pair files, market check, fee defaults, its
optional own keys, and one Hyperliquid request budget when both legs trade
from one address."""
import asyncio
import logging
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import entropy_arb.engine as engine_mod  # noqa: E402
from entropy_arb.config import ConfigError, load_config  # noqa: E402
from test_risk import FakeVenue, make_engine  # noqa: E402
from test_tickers import BASE  # noqa: E402

XYZ = "tradexyz"
PK1 = "0x" + "11" * 32
PK2 = "0x" + "22" * 32
ADDR1 = "0x" + "aa" * 20
ADDR2 = "0x" + "bb" * 20
ENV_NAMES = ("HL_PRIVATE_KEY", "HL_ACCOUNT_ADDRESS", "HL_PRIVATE_KEY_XYZ",
             "HL_ACCOUNT_ADDRESS_XYZ")


@pytest.fixture
def club_dir(tmp_path, monkeypatch):
    root = os.path.join(os.path.dirname(__file__), "..")
    import club
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.example.yaml").write_text(
        open(os.path.join(root, "config.example.yaml"), encoding="utf-8").read())
    (tmp_path / "config.yaml").write_text(BASE)
    monkeypatch.setattr(club, "find_bots", lambda: [])
    for k in ENV_NAMES:
        monkeypatch.delenv(k, raising=False)
    club.ensure_ticker_files()
    return club


def _load(symbol, venue=XYZ, **kw):
    return load_config("config.yaml", ".env", symbol=symbol,
                       hedge_venue=venue, **kw)


# ------------------------------------------------------------ pair files

def test_xyz_pairs_created_uncalibrated_with_its_own_fee(club_dir):
    club = club_dir
    assert club.pair_list(XYZ) == ["SNDK", "ANTH", "OAI", "NBIS", "DRAM", "EWY"]
    v = club.cfg_values((XYZ, "SNDK"))
    assert v["midline_bps"] == 0 and not club.is_calibrated(v)
    # NOT the 0 bps of config.yaml (that is Lighter's fee)
    assert v["fee_hedge"] == 1.0 and not v["hedge_fee_checked"]
    assert v["csv"] == "logs/minutes_tradexyz_SNDK.csv"
    assert club.cfg_values((XYZ, "ANTH"))["hedge_symbols"] == "ANTH, ANTHROPIC"
    # Lighter pairs: hedge fee documented 0% — nothing to check
    assert club.cfg_values(("lighter-rh", "SNDK"))["hedge_fee_checked"]
    for t in club.pair_list(XYZ):
        c = _load(t, record_only=True)
        assert c.hedge.kind == "hl" and c.hedge.hl_dex == "xyz"
        assert c.hedge.fee_bps == 1.0 and c.hedge_fee_checked is False


def test_xyz_pair_refused_live_until_calibrated(club_dir):
    with pytest.raises(ConfigError, match="not calibrated"):
        _load("SNDK")


def test_hedge_fee_saved_to_pair_file_only(club_dir):
    club = club_dir
    club.save_config([("hedge", "taker_fee_bps", "0.7"),
                      ("ticker", "hedge_fee_checked", "true")], (XYZ, "SNDK"))
    v = club.cfg_values((XYZ, "SNDK"))
    assert v["fee_hedge"] == 0.7 and v["hedge_fee_checked"]
    assert club.cfg_values((XYZ, "DRAM"))["fee_hedge"] == 1.0
    assert club.cfg_values(("lighter", "SNDK"))["fee_hedge"] == 0.0


def test_market_status_on_xyz(club_dir):
    club = club_dir
    ent = {"SNDK": False, "ANTH": False, "NBIS": False}
    hed = {"SNDK", "ANTHROPIC"}
    assert club.market_status((XYZ, "SNDK"), ent, hed)[0]
    assert club.market_status((XYZ, "ANTH"), ent, hed) == \
        (True, "", "ANTH", "ANTHROPIC")
    assert club.market_status((XYZ, "NBIS"), ent, hed)[1] == "нет на trade.xyz"


def test_fetch_markets_reads_xyz_dex(club_dir, monkeypatch):
    club = club_dir
    calls = []

    def fake(url, payload=None, timeout=8.0):
        calls.append(payload)
        if payload == {"type": "meta", "dex": "io"}:
            return {"universe": [{"name": "io:SNDK"}]}
        assert payload == {"type": "meta", "dex": "xyz"}
        return {"universe": [{"name": "xyz:SNDK"},
                             {"name": "xyz:OAI", "isDelisted": True}]}
    monkeypatch.setattr(club, "_http_json", fake)
    ent, hed, err = club.fetch_markets(XYZ)
    assert err is None and ent == {"SNDK": False} and hed == {"SNDK"}


# ------------------------------------------------------------------ keys

def test_xyz_uses_entropy_keys_by_default(club_dir, monkeypatch):
    monkeypatch.setenv("HL_PRIVATE_KEY", PK1)
    monkeypatch.setenv("HL_ACCOUNT_ADDRESS", ADDR1)
    c = _load("SNDK", record_only=True)
    assert c.hedge.hl_creds.private_key == PK1
    assert c.hedge.hl_creds.account_address == ADDR1


def test_xyz_own_keys_are_a_pair(club_dir, monkeypatch):
    monkeypatch.setenv("HL_PRIVATE_KEY", PK1)
    monkeypatch.setenv("HL_ACCOUNT_ADDRESS", ADDR1)
    monkeypatch.setenv("HL_PRIVATE_KEY_XYZ", PK2)
    monkeypatch.setenv("HL_ACCOUNT_ADDRESS_XYZ", ADDR2)
    c = _load("SNDK", record_only=True)
    assert (c.hedge.hl_creds.private_key,
            c.hedge.hl_creds.account_address) == (PK2, ADDR2)
    assert c.entropy.hl_creds.private_key == PK1
    # own key without own address: that wallet's own address — never the
    # Entropy address with another wallet's key
    monkeypatch.delenv("HL_ACCOUNT_ADDRESS_XYZ")
    c = _load("SNDK", record_only=True)
    assert c.hedge.hl_creds.account_address is None


def test_xyz_address_without_key_is_refused_for_trading(club_dir, monkeypatch):
    club = club_dir
    club.save_config([("thresholds", "midline_bps", "-5")], (XYZ, "SNDK"))
    monkeypatch.setenv("HL_PRIVATE_KEY", PK1)
    monkeypatch.setenv("HL_ACCOUNT_ADDRESS", ADDR1)
    monkeypatch.setenv("HL_ACCOUNT_ADDRESS_XYZ", ADDR2)
    with pytest.raises(ConfigError, match="HL_ACCOUNT_ADDRESS_XYZ"):
        _load("SNDK")
    _load("SNDK", record_only=True)          # recording needs no keys


def test_menu_key_state_for_xyz(club_dir):
    club = club_dir
    club.write_env({"HL_PRIVATE_KEY": PK1, "HL_ACCOUNT_ADDRESS": ADDR1})
    state, ok = club.keys_state(XYZ)
    assert ok and club.xyz_keys_mode() == "shared"   # optional: empty is fine
    club.write_env({"HL_PRIVATE_KEY_XYZ": PK2})
    assert club.xyz_keys_mode() == "broken"
    assert not club.keys_state(XYZ)[1]
    club.write_env({"HL_ACCOUNT_ADDRESS_XYZ": ADDR2})
    assert club.xyz_keys_mode() == "own" and club.keys_state(XYZ)[1]
    # Lighter venues do not depend on the trade.xyz keys
    club.write_env({"HL_ACCOUNT_ADDRESS_XYZ": "bad"})
    assert club.xyz_keys_mode() == "broken"
    assert "HL_PRIVATE_KEY_XYZ" not in club.keys_state("lighter-rh")[0]
    v, err, _ = club.validate_key("HL_PRIVATE_KEY_XYZ", "22" * 32)
    assert err is None and v == "0x" + "22" * 32
    assert club.mask("HL_ACCOUNT_ADDRESS_XYZ", ADDR2).startswith("0xbbbb")
    assert "HL_PRIVATE_KEY_XYZ" in club.SECRET_KEYS


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
