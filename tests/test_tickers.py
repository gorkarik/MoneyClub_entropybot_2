"""Ticker selection: per-ticker profiles (tickers/<TICKER>.yaml), market
names that differ between venues, journals that say which market a row is,
and the menu's routing of settings to the right file."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb import journal  # noqa: E402
from entropy_arb.config import (ConfigError, load_config,  # noqa: E402
                                merge_ticker_profile, split_names)
from entropy_arb.venue_hl import pick_coin  # noqa: E402
from entropy_arb.venue_lighter import pick_market  # noqa: E402

BASE = """\
thresholds:
  midline_bps: -8.6
  upper_bps: 3.5
  lower_bps: 4.0
entropy:
  taker_fee_bps: 0.86
  max_position_usd: 25
hedge:
  taker_fee_bps: 0.0
  max_position_usd: 25
risk:
  max_loss_pct: 1.0
recorder:
  csv: logs/minutes.csv
"""

ANTH_UNCAL = """\
ticker:
  entropy_symbol: "ANTH"
  hedge_symbols: "ANTH, ANTHROPIC"
  fee_checked: false
thresholds:
  midline_bps: 0
  upper_bps: 4.0
  lower_bps: 4.5
entropy:
  taker_fee_bps: 0.86
  max_position_usd: 30
"""


def _setup(tmp_path, base=BASE, tickers=None):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(base)
    if tickers is not None:
        d = tmp_path / "tickers"
        d.mkdir()
        for name, text in tickers.items():
            (d / f"{name}.yaml").write_text(text)
    return str(cfg)


def _load(cfg, symbol, **kw):
    return load_config(cfg, "/nonexistent-env", symbol=symbol,
                       hedge_venue="lighter-rh", **kw)


# ------------------------------------------------------------ config layer

def test_single_file_mode_unchanged(tmp_path):
    cfg = _setup(tmp_path)
    c = _load(cfg, "SNDK")
    assert c.ticker_file is None and c.calibrated and c.fee_checked is None
    assert c.midline_bps == -8.6 and c.recorder_csv == "logs/minutes.csv"
    assert c.entropy.symbol == "SNDK" and c.hedge.symbol == "SNDK"


def test_uncalibrated_ticker_refused_live_allowed_for_recording(tmp_path):
    cfg = _setup(tmp_path, tickers={"ANTH": ANTH_UNCAL})
    with pytest.raises(ConfigError, match="not calibrated"):
        _load(cfg, "ANTH")
    c = _load(cfg, "ANTH", record_only=True)
    assert not c.calibrated
    # profile overrides the cap; the loss limit stays global
    assert c.entropy.cap_usd == 30 and c.max_loss_pct == 1.0
    # its own minute file, never the shared config.yaml one
    assert c.recorder_csv == "logs/minutes_lighter-rh_ANTH.csv"
    assert c.hedge.symbol_aliases == ("ANTH", "ANTHROPIC")
    assert c.hedge.symbol == "ANTH"          # first guess until load_market


def test_thresholds_never_inherited_from_config_yaml(tmp_path):
    # a ticker file without thresholds must NOT trade on SNDK's numbers
    cfg = _setup(tmp_path, tickers={"NBIS": "ticker:\n  fee_checked: false\n"})
    with pytest.raises(ConfigError):
        _load(cfg, "NBIS")
    assert _load(cfg, "NBIS", record_only=True).midline_bps == 0.0


def test_missing_ticker_file_is_an_error(tmp_path):
    cfg = _setup(tmp_path, tickers={"ANTH": ANTH_UNCAL})
    with pytest.raises(ConfigError, match="no settings for DRAM"):
        _load(cfg, "DRAM", record_only=True)


def test_ticker_file_typo_is_an_error(tmp_path):
    cfg = _setup(tmp_path, tickers={"ANTH": ANTH_UNCAL + "risk:\n  max_loss_pct: 5\n"})
    with pytest.raises(ConfigError, match="ANTH.yaml"):
        _load(cfg, "ANTH", record_only=True)


def test_hedge_symbol_override(tmp_path):
    cfg = _setup(tmp_path, tickers={"ANTH": ANTH_UNCAL})
    c = _load(cfg, "ANTH", record_only=True, hedge_symbol="ANTHROPIC")
    assert c.hedge.symbol_aliases == ("ANTHROPIC",)


def test_split_names_and_merge():
    assert split_names(" ANTH, ANTHROPIC ,ANTH,, ") == ("ANTH", "ANTHROPIC")
    assert split_names(None) == ()
    merged = merge_ticker_profile(
        {"thresholds": {"midline_bps": -7}, "entropy": {"dex": "io",
                                                        "taker_fee_bps": 0}},
        {"entropy": {"taker_fee_bps": 0.9}})
    assert merged["thresholds"] == {}                   # not inherited
    assert merged["entropy"] == {"dex": "io", "taker_fee_bps": 0.9}


# ------------------------------------------------------ market name lookup

def test_pick_market_takes_first_listed_active_name():
    books = [{"symbol": "ANTHROPIC", "status": "active"},
             {"symbol": "SNDK", "status": "active"}]
    assert pick_market(books, ("ANTH", "ANTHROPIC"))["symbol"] == "ANTHROPIC"
    assert pick_market(books, ("NBIS",)) is None
    books.append({"symbol": "ANTH", "status": "inactive"})
    # an inactive earlier name loses to an active later one
    assert pick_market(books, ("ANTH", "ANTHROPIC"))["symbol"] == "ANTHROPIC"


def test_pick_coin_handles_dex_prefix_and_delisting():
    uni = [{"name": "io:OAI", "isDelisted": True}, {"name": "io:ANTH"},
           {"name": "io:SNDK"}]
    idx, a, name = pick_coin(uni, "io", ("ANTH",))
    assert (idx, name) == (1, "ANTH")
    assert pick_coin(uni, "io", ("NBIS",)) is None
    # only a delisted listing: returned so the caller can say "delisted"
    assert pick_coin(uni, "io", ("OAI",))[1]["isDelisted"]


# ------------------------------------------------------------- journals

def test_journal_headers_grow_at_the_end_only():
    # older files are upgraded in place only if their header is a prefix
    assert journal.TRADES_HEADER[-5:] == ["symbol", "hedge_symbol",
                                          "hedge_venue", "action", "strategy"]
    assert journal.HEDGES_HEADER[-2:] == ["symbol", "hedge_venue"]
    assert journal.EQUITY_HEADER[-2:] == ["symbol", "hedge_venue"]
    assert journal.SESSIONS_HEADER[-8:] == ["hedge_symbol", "max_unhedged_usd",
                                            "dust_left_usd", "funding_entropy_usd",
                                            "funding_hedge_usd", "exp_edge_usd",
                                            "fill_edge_usd", "strategy"]


def test_report_filters_rows_by_ticker():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
    import report
    rows = [{"symbol": ""}, {"symbol": "SNDK"}, {"symbol": "ANTH"}]
    assert len(report.for_symbol(rows, "SNDK", "SNDK")) == 2  # legacy = SNDK
    assert len(report.for_symbol(rows, "ANTH", "SNDK")) == 1
    assert len(report.for_symbol(rows, "", "SNDK")) == 3


# ---------------------------------------------------------------- menu

@pytest.fixture
def club_dir(tmp_path, monkeypatch):
    root = os.path.join(os.path.dirname(__file__), "..")
    import club
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.example.yaml").write_text(
        open(os.path.join(root, "config.example.yaml"), encoding="utf-8").read())
    (tmp_path / "config.yaml").write_text(BASE)
    monkeypatch.setattr(club, "find_bots", lambda: [])
    club.ensure_ticker_files()
    return club


RH, CORE = "lighter-rh", "lighter"


def test_menu_creates_pairs_per_venue(club_dir):
    club = club_dir
    assert club.pair_list(RH) == ["SNDK", "ANTH", "OAI"]
    assert club.pair_list(CORE) == ["SNDK", "ANTH", "OAI", "NBIS", "DRAM",
                                    "EWY"]
    s = club.cfg_values((RH, "SNDK"))
    # SNDK on RH keeps exactly what it traded with, and its minute file
    assert (s["midline_bps"], s["upper_bps"], s["lower_bps"]) == (-8.6, 3.5, 4.0)
    assert s["csv"] == "logs/minutes.csv"
    # its Entropy fee comes from config.yaml as it was (never silently reset)
    assert s["fee_entropy"] == 0.86
    # the same ticker on Lighter core is another market: not calibrated
    c = club.cfg_values((CORE, "SNDK"))
    assert c["midline_bps"] == 0 and not club.is_calibrated(c)
    assert c["csv"] == "logs/minutes_lighter_SNDK.csv"
    a = club.cfg_values((RH, "ANTH"))
    assert a["hedge_symbols"] == "ANTHROPIC, ANTH"
    for venue in club.VENUES:
        for t in club.pair_list(venue):
            _load_v("config.yaml", t, venue, record_only=True)


def _load_v(cfg, symbol, venue, **kw):
    return load_config(cfg, "/nonexistent-env", symbol=symbol,
                       hedge_venue=venue, **kw)


def test_flat_files_of_previous_version_are_migrated(tmp_path, monkeypatch):
    import club
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(BASE)
    d = tmp_path / "tickers"
    d.mkdir()
    (d / "SNDK.yaml").write_text("thresholds:\n  midline_bps: -9.1\n"
                                 "  upper_bps: 3\n  lower_bps: 4\n"
                                 "sizing:\n  take_fraction: 0.5\n")
    (d / "NBIS.yaml").write_text("thresholds:\n  midline_bps: 0\n"
                                 "  upper_bps: 4\n  lower_bps: 4.5\n")
    (d / ".current").write_text("SNDK\n")
    monkeypatch.setattr(club, "find_bots", lambda: [])
    club.ensure_ticker_files()
    assert not (d / "SNDK.yaml").exists()
    assert club.cfg_values((RH, "SNDK"))["midline_bps"] == -9.1
    assert "take_fraction" not in (d / RH / "SNDK.yaml").read_text()
    assert (d / "_old" / "NBIS.yaml").exists()       # not on RH: set aside
    assert club.current_pair() == (RH, "SNDK")       # old .current format
    assert club.pair_list(RH) == ["SNDK", "ANTH", "OAI"]


def test_menu_save_routes_market_keys_to_pair_file(club_dir):
    import yaml
    club = club_dir
    club.set_current_pair((CORE, "ANTH"))
    club.save_config([("thresholds", "midline_bps", "-12"),
                      ("risk", "max_loss_pct", "2")])
    base = yaml.safe_load(open("config.yaml"))
    assert base["thresholds"]["midline_bps"] == -8.6     # untouched
    assert base["risk"]["max_loss_pct"] == 2             # global
    assert club.cfg_values((CORE, "ANTH"))["midline_bps"] == -12
    assert club.cfg_values((RH, "ANTH"))["midline_bps"] == 0  # other venue
    assert club.cfg_values((RH, "SNDK"))["midline_bps"] == -8.6


def test_menu_save_rolls_back_both_files_on_error(club_dir):
    club = club_dir
    club.set_current_pair((RH, "ANTH"))
    path = club.pair_path((RH, "ANTH"))
    before = open(path).read(), open("config.yaml").read()
    with pytest.raises(RuntimeError):
        club.save_config([("risk", "max_loss_pct", "3"),
                          ("thresholds", "upper_bps", "-1")])
    assert (open(path).read(), open("config.yaml").read()) == before


def test_existing_pair_files_are_never_overwritten(club_dir):
    club = club_dir
    club.save_config([("thresholds", "midline_bps", "-5")], (CORE, "DRAM"))
    club.ensure_ticker_files()
    assert club.cfg_values((CORE, "DRAM"))["midline_bps"] == -5


def test_market_status(club_dir):
    club = club_dir
    ent = {"ANTH": False, "OAI": True, "NBIS": False, "DRAM": False,
           "SNDK": False}
    hed = {"ANTHROPIC", "SNDK", "DRAM"}
    assert club.market_status((RH, "ANTH"), ent, hed) == \
        (True, "", "ANTH", "ANTHROPIC")
    assert club.market_status((RH, "OAI"), ent, hed)[1] == \
        "снят с торгов на Entropy"
    assert club.market_status((CORE, "NBIS"), ent, hed)[1] == "нет на Lighter"


def test_bot_process_args_give_its_pair(club_dir):
    club = club_dir
    b = club.Bot(1, ["python3", "main.py", "--symbol", "DRAM", "--hedge",
                     "lighter", "--ru"], os.getcwd())
    assert (b.venue, b.ticker) == ("lighter", "DRAM")
    old = club.Bot(2, ["python3", "main.py", "--symbol", "SNDK"], os.getcwd())
    assert (old.venue, old.ticker) == ("lighter-rh", "SNDK")


def test_core_keys_are_separate_from_rh(tmp_path, monkeypatch):
    for k in ("HL_PRIVATE_KEY", "HL_ACCOUNT_ADDRESS", "LIGHTER_ACCOUNT_INDEX",
              "LIGHTER_API_KEY_INDEX", "LIGHTER_API_PRIVATE_KEY",
              "LIGHTER_CORE_ACCOUNT_INDEX", "LIGHTER_CORE_API_KEY_INDEX",
              "LIGHTER_CORE_API_PRIVATE_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LIGHTER_ACCOUNT_INDEX", "111")
    monkeypatch.setenv("LIGHTER_API_KEY_INDEX", "4")
    monkeypatch.setenv("LIGHTER_API_PRIVATE_KEY", "ab" * 40)
    cfg = _setup(tmp_path)
    rh = _load_v(cfg, "SNDK", RH)
    assert rh.hedge.lighter_creds.account_index == 111
    core = _load_v(cfg, "SNDK", CORE)
    # RH keys are NOT used for core — a signature for one is rejected by
    # the other exchange
    assert core.hedge.lighter_creds.account_index is None
    assert not core.creds_complete
    monkeypatch.setenv("LIGHTER_CORE_ACCOUNT_INDEX", "222")
    monkeypatch.setenv("LIGHTER_CORE_API_KEY_INDEX", "5")
    monkeypatch.setenv("LIGHTER_CORE_API_PRIVATE_KEY", "cd" * 40)
    core = _load_v(cfg, "SNDK", CORE)
    assert core.hedge.lighter_creds.account_index == 222
    assert core.hedge.lighter_profile.chain_id == 304


def test_report_filters_by_venue():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
    import report
    rows = [{"symbol": "", "hedge_venue": ""},              # oldest: RH SNDK
            {"symbol": "SNDK", "hedge_venue": ""},          # RH SNDK
            {"symbol": "SNDK", "hedge_venue": "lighter"},
            {"symbol": "ANTH", "hedge_venue": "lighter"}]
    f = lambda s, v: report.for_symbol(rows, s, "SNDK", v, "lighter-rh")  # noqa
    assert len(f("SNDK", "lighter-rh")) == 2
    assert len(f("SNDK", "lighter")) == 1
    assert len(f("ANTH", "lighter")) == 1
    assert len(f("", "")) == 4


def test_execution_menu_matches_config_defaults_and_ranges(tmp_path):
    """The menu's 'author value' must be what the bot uses when the key is
    absent, and every value the menu accepts must load."""
    import club
    cfg = _setup(tmp_path, base="thresholds:\n  midline_bps: -8\n  upper_bps: 4\n"
                                "  lower_bps: 4\n")
    c = _load(cfg, "SNDK")
    attr = {"premium_persist_sec": "premium_persist_sec",
            "cooldown_sec": "cooldown_sec", "take_fraction": "take_fraction",
            "scale_bps": "inventory_scale_bps",
            "floor_frac": "inventory_floor_frac",
            "leg_slippage_bps": "leg_slippage_bps",
            "hedge_slippage_bps": "hedge_slippage_bps",
            "staleness_sec": "staleness_sec"}
    for sec, key, dflt, _u, _s, _l, (lo, hi, lo_ok) in club.EXEC_PARAMS:
        assert getattr(c, attr[key]) == dflt, key
        top = hi * 0.999 if key == "floor_frac" else hi
        for val in ([lo] if lo_ok else []) + [top]:
            text = open(cfg).read() + f"{sec}:\n  {key}: {val}\n"
            p = tmp_path / f"c_{key}.yaml"
            p.write_text(text)
            load_config(str(p), "/nonexistent-env", symbol="SNDK",
                        hedge_venue="lighter-rh")


def test_calibration_widens_band_to_cover_execution_loss():
    import club
    sug = {"midline_bps": -8.6, "upper_bps": 1.5, "lower_bps": 3.0}
    # 5 bps lost per trade -> a round trip (2 trades) needs >= 10 bps band
    adj = club.exec_adjusted(sug, 5.0)
    assert adj["midline_bps"] == -8.6
    assert adj["upper_bps"] + adj["lower_bps"] >= 10.0
    assert adj["upper_bps"] / adj["lower_bps"] == pytest.approx(0.5, rel=0.15)
    # already wide enough / no data / fills better than planned: unchanged
    assert club.exec_adjusted({"midline_bps": -8, "upper_bps": 6,
                               "lower_bps": 6}, 5.0)["upper_bps"] == 6
    assert club.exec_adjusted(sug, None) == sug
    assert club.exec_adjusted(sug, -1.0) == sug
