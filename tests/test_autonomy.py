"""Autonomy: floating centre, exit relief, calm opening.

Run:  python3 -m pytest tests/
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from entropy_arb import autonomy  # noqa: E402
from entropy_arb.autonomy import AutonomyConf, Centre  # noqa: E402
from entropy_arb.config import ConfigError, load_config  # noqa: E402
from test_engine import NO_ENV, approx, make_engine  # noqa: E402


def fill(c, start, minutes, value, step=10.0):
    t = start
    while t < start + minutes * 60.0:
        c.add(t, value)
        t += step
    return t


def test_centre_needs_min_data_then_follows_median():
    c = Centre(AutonomyConf(window_min=60, min_min=20), manual=-160.0)
    end = fill(c, 0.0, 10, -170.0)
    assert c.live is None                     # 10 min < 20 min
    end = fill(c, end, 15, -170.0)
    approx(c.live, -170.0)


def test_centre_window_forgets_old_level():
    c = Centre(AutonomyConf(window_min=60, min_min=20), manual=-160.0)
    end = fill(c, 0.0, 60, -170.0)
    end = fill(c, end, 61, -150.0)
    approx(c.live, -150.0)


def test_centre_leash():
    c = Centre(AutonomyConf(window_min=60, min_min=20, max_drift_bps=10),
               manual=-160.0)
    fill(c, 0.0, 30, -200.0)
    approx(c.live, -170.0)
    assert c.at_leash


def test_jump_detection():
    c = Centre(AutonomyConf(window_min=60, min_min=20, jump_bps=15,
                            jump_window_min=5), manual=-160.0)
    end = fill(c, 0.0, 55, -160.0)
    assert not c.jumped()
    fill(c, end, 5, -185.0)
    assert c.jumped()                         # 5-min median 25 bps away


def test_exit_relief():
    conf = AutonomyConf(exit_hours=6, exit_floor_bps=0, size_weight=0.5)
    assert autonomy.exit_relief_bps(conf, 5.0, None, 0.0) == 0.0
    approx(autonomy.exit_relief_bps(conf, 5.0, 3 * 3600, 0.0), 2.5)
    approx(autonomy.exit_relief_bps(conf, 5.0, 12 * 3600, 0.0), 5.0)
    # size part starts at half the cap; full cap = size_weight of the band
    approx(autonomy.exit_relief_bps(conf, 5.0, 0, 0.5), 0.0)
    approx(autonomy.exit_relief_bps(conf, 5.0, 0, 1.0), 2.5)
    # floor: never closer to the centre than exit_floor_bps
    conf2 = AutonomyConf(exit_hours=6, exit_floor_bps=1.0)
    approx(autonomy.exit_relief_bps(conf2, 5.0, 24 * 3600, 0.0), 4.0)


def _cfg_text(extra=""):
    return f"""
thresholds:
  midline_bps: -160
  upper_bps: 5
  lower_bps: 5
{extra}
"""


def _load(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return load_config(f.name, NO_ENV, symbol="OAI", hedge_venue="lighter-rh")


def test_config_defaults_on_and_ranges():
    cfg = _load(_cfg_text())
    assert cfg.autonomy.enabled and cfg.autonomy.window_min == 60.0
    cfg = _load(_cfg_text("autonomy:\n  enabled: false\n  window_min: 120"))
    assert not cfg.autonomy.enabled and cfg.autonomy.window_min == 120.0
    with pytest.raises(ConfigError):
        _load(_cfg_text("autonomy:\n  size_weight: 2"))
    with pytest.raises(ConfigError):
        _load(_cfg_text("autonomy:\n  window_min: 30\n  min_min: 40"))


def test_engine_uses_floating_centre():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    e, h = eng.entropy, eng.hedge
    e.set_book(100.0, 100.0)
    h.set_book(100.0, 100.0)
    fill(eng.centre, 0.0, 30, 15.0)
    approx(eng.mid_bps(), 15.0)
    approx(eng._eff_threshold(buy=h, sell=e), 19.0)     # 15 + 4


def test_engine_exit_relief_on_closing_side_only():
    eng = make_engine(midline=0.0, upper=4.0, lower=4.0)
    e, h = eng.entropy, eng.hedge
    e.set_book(100.0, 100.0)
    h.set_book(100.0, 100.0)
    e.position, h.position = 0.2, -0.2          # long Entropy $20 of $10k
    eng.hold_age_sec()
    eng.pos_open_ts -= 3 * 3600                 # opened 3 h ago
    # closing = sell Entropy: 4 → 2 (half of exit_hours)
    assert 1.9 < eng._eff_threshold(buy=h, sell=e) < 2.1
    # adding = buy Entropy: no relief
    approx(eng._eff_threshold(buy=e, sell=h), 4.0)


def test_engine_open_cooldown_and_jump():
    asyncio.run(_open_cooldown_and_jump())


async def _open_cooldown_and_jump():
    eng = make_engine(midline=0.0, upper=4.0, lower=4.0)
    e, h = eng.entropy, eng.hedge
    e.set_book(100.0, 100.0)
    h.set_book(100.0, 100.0)
    eng.last_open_ts = 100.0
    assert eng._open_blocked(h, e, 110.0, "sell_entropy")      # 10 s < 30 s
    assert not eng._open_blocked(h, e, 140.0, "sell_entropy")
    end = fill(eng.centre, 0.0, 55, 0.0)
    fill(eng.centre, end, 5, 30.0)
    assert eng._open_blocked(h, e, 10_000.0, "sell_entropy")   # jump
    e.position, h.position = -0.2, 0.2                         # short Entropy
    assert not eng._open_blocked(e, h, 10_000.0, "buy_entropy")  # closing ok


def test_closing_trade_never_flips_the_position():
    from test_engine import run_scan
    eng = make_engine(midline=0.0, upper=4.0, lower=4.0)
    e, h = eng.entropy, eng.hedge
    e.set_book(100.14, 100.16, sz=50.0)       # 15 bps rich: sell Entropy
    h.set_book(99.99, 100.01, sz=50.0)
    e.position, h.position = 0.15, -0.15      # long Entropy ≈ $15
    best = run_scan(eng)
    assert best is not None
    buy, sell, plan = best
    assert sell is e and plan.qty <= 0.15 + 1e-9
