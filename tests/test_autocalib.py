"""Auto-calibration of the midline: off by default, guarded steps, the
anchor set by hand, persistence in the pair file; plus the menu toggles."""
import asyncio
import csv
import os
import sys
import time

import pytest
import yaml

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb import autocalib  # noqa: E402
from entropy_arb.config import ConfigError, load_config  # noqa: E402
from test_risk import make_engine  # noqa: E402
from test_tickers import BASE  # noqa: E402

GUARDS = dict(min_hours=18, max_step_bps=1.0, max_drift_bps=3.0)
DAY = 86400.0


def _prems(n, val):
    return [val] * n


# ------------------------------------------------------------ pure rules

def test_steps_toward_median_at_most_max_step():
    d = autocalib.decide(-7.0, -7.0, _prems(2000, -9.4), **GUARDS)
    assert d.target == -9.4 and d.new_midline == -8.0
    assert "шаг ограничен" in d.reason
    d = autocalib.decide(-7.0, -7.0, _prems(2000, -7.6), **GUARDS)
    assert d.new_midline == -7.6 and d.reason == ""


def test_never_leaves_the_leash_around_the_manual_midline():
    # already 3 bps below the manual -7: no further, whatever the median
    d = autocalib.decide(-10.0, -7.0, _prems(2000, -14.0), **GUARDS)
    assert d.new_midline is None and "предел" in d.reason
    d = autocalib.decide(-9.5, -7.0, _prems(2000, -14.0), **GUARDS)
    assert d.new_midline == -10.0


def test_needs_enough_data_and_skips_tiny_changes():
    d = autocalib.decide(-7.0, -7.0, _prems(18 * 60 - 1, -9.0), **GUARDS)
    assert d.new_midline is None and "мало данных" in d.reason
    d = autocalib.decide(-7.0, -7.0, _prems(2000, -7.04), **GUARDS)
    assert d.new_midline is None


def test_never_writes_zero_which_means_not_calibrated():
    d = autocalib.decide(-0.5, -0.5, _prems(2000, 0.3), **GUARDS)
    assert d.new_midline not in (0.0, None)


def test_window_reads_only_the_period(tmp_path):
    p = tmp_path / "m.csv"
    now = 1_780_000_000.0
    with open(p, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["minute_ts", "premium_close_bps", "samples"])
        w.writerow([now - 4 * DAY, -20, 60])        # outside 72 h
        w.writerow([now - 3600, -8, 60])
        w.writerow([now - 1800, -9, 3])              # too few samples
    assert autocalib.window_premiums(str(p), 72, now) == [-8.0]
    assert autocalib.window_premiums(str(tmp_path / "none.csv"), 72, now) == []


def test_due():
    assert autocalib.due(None, 24, 100.0)
    assert not autocalib.due(1000.0, 24, 1000.0 + 3600)
    assert autocalib.due(1000.0, 24, 1000.0 + DAY)


def test_save_keeps_comments(tmp_path):
    f = tmp_path / "SNDK.yaml"
    f.write_text("ticker:\n  fee_checked: true   # проверена\nthresholds:\n"
                 "  midline_bps: -7.0         # центр премии\n"
                 "  upper_bps: 4\n  lower_bps: 4.5\n")
    autocalib.save(str(f), -7.8, 1234.0)
    text = f.read_text()
    assert "# центр премии" in text and "# проверена" in text
    data = yaml.safe_load(text)
    assert data["thresholds"]["midline_bps"] == -7.8
    assert data["ticker"]["autocalib_last_ts"] == 1234


# --------------------------------------------------------------- config

def _cfg(tmp_path, extra="", ticker_extra=""):
    (tmp_path / "config.yaml").write_text(BASE + extra)
    d = tmp_path / "tickers" / "lighter-rh"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SNDK.yaml").write_text(
        "ticker:\n  fee_checked: true\n" + ticker_extra +
        "thresholds:\n  midline_bps: -7.0\n  upper_bps: 4\n  lower_bps: 4.5\n")
    return str(tmp_path / "config.yaml")


def test_off_by_default_and_validated(tmp_path):
    c = load_config(_cfg(tmp_path), "/x", symbol="SNDK",
                    hedge_venue="lighter-rh")
    assert c.autocalib_enabled is False and c.autocalib_window_hours == 72
    assert c.midline_anchor_bps is None and c.autocalib_last_ts is None
    c = load_config(_cfg(tmp_path, "autocalib:\n  enabled: true\n",
                         "  midline_anchor_bps: -6.5\n"
                         "  autocalib_last_ts: 1000\n"),
                    "/x", symbol="SNDK", hedge_venue="lighter-rh")
    assert c.autocalib_enabled and c.midline_anchor_bps == -6.5
    assert c.autocalib_last_ts == 1000.0
    with pytest.raises(ConfigError, match="autocalib"):
        load_config(_cfg(tmp_path, "autocalib:\n  max_step_bps: 9\n"), "/x",
                    symbol="SNDK", hedge_venue="lighter-rh")


# --------------------------------------------------------------- engine

def _ac_engine(tmp_path, epos=0.0, hpos=0.0):
    eng = make_engine(str(tmp_path), epos=epos, hpos=hpos)
    pair = tmp_path / "SNDK.yaml"
    pair.write_text("ticker:\n  fee_checked: true\nthresholds:\n"
                    "  midline_bps: -7.0\n  upper_bps: 4.0\n  lower_bps: 4.5\n")
    minutes = tmp_path / "minutes.csv"
    now = time.time()
    with open(minutes, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["minute_ts", "premium_close_bps", "samples"])
        for i in range(3 * 1440):
            w.writerow([now - 3 * DAY + i * 60, -9.0, 60])
    eng.cfg.ticker_file = str(pair)
    eng.cfg.recorder_csv = str(minutes)
    eng.cfg.autocalib_enabled = True
    eng.autocalib_on = True
    eng.autocalib_anchor = -7.0
    return eng, pair


def test_engine_moves_midline_live_and_in_the_file(tmp_path):
    eng, pair = _ac_engine(tmp_path)
    assert eng.is_flat()
    asyncio.run(eng._autocalib_once(time.time()))
    assert eng.cfg.midline_bps == -8.0                  # one 1-bps step
    assert yaml.safe_load(pair.read_text())["thresholds"]["midline_bps"] == -8.0
    log = os.path.join(str(tmp_path), "logs", "autocalib.csv")
    rows = list(csv.DictReader(open(log)))
    assert rows[-1]["old_midline"] == "-7" and rows[-1]["new_midline"] == "-8"
    # the strategy uses it at once: sell threshold = midline + upper
    assert eng._eff_threshold(eng.hedge, eng.entropy) == pytest.approx(-4.0)


def test_engine_waits_while_a_position_is_open(tmp_path):
    async def go():
        eng, _ = _ac_engine(tmp_path, epos=0.5, hpos=-0.5)
        assert not eng.is_flat()
        t = asyncio.create_task(eng._autocalib_loop())
        await asyncio.sleep(0.05)
        eng.request_stop()
        await t
        return eng
    eng = asyncio.run(go())
    assert eng.autocalib_waiting and eng.cfg.midline_bps == -7.0


def test_engine_off_by_default(tmp_path):
    eng = make_engine(str(tmp_path))
    assert not eng.autocalib_on


# ------------------------------------------------------------------ menu

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


def test_manual_midline_becomes_the_anchor(club_dir):
    club = club_dir
    pair = ("lighter-rh", "SNDK")
    before = time.time()
    club.save_config([("thresholds", "midline_bps", "-9.2")], pair)
    v = club.cfg_values(pair)
    assert v["midline_bps"] == -9.2 and v["midline_anchor"] == -9.2
    assert v["autocalib_last_ts"] >= before - 1      # timer restarts
    # other settings do not touch the anchor
    club.save_config([("thresholds", "upper_bps", "5")], pair)
    assert club.cfg_values(pair)["midline_anchor"] == -9.2


def test_menu_toggles_go_to_config_yaml(club_dir):
    club = club_dir
    pair = ("lighter-rh", "SNDK")
    v = club.cfg_values(pair)
    assert not v["autocalib"] and not v["flatten_on_halt"]
    club.save_config([("autocalib", "enabled", "true"),
                      ("autocalib", "window_hours", "168"),
                      ("risk", "flatten_on_halt", "true")], pair)
    v = club.cfg_values(pair)
    assert v["autocalib"] and v["autocalib_window"] == 168
    assert v["flatten_on_halt"]
    base = yaml.safe_load(open("config.yaml"))
    assert base["autocalib"]["enabled"] is True
    c = load_config("config.yaml", "/x", symbol="SNDK",
                    hedge_venue="lighter-rh")
    assert c.autocalib_enabled and c.flatten_on_halt
