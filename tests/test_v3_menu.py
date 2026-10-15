"""Version 3 menu: one position/order limit for all pairs (migrated from the
pair files), bot nickname, tick recording always on, no trade.xyz."""
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_tickers import BASE, RH, CORE, club_dir  # noqa: E402,F401

OLD_PAIR = """\
ticker:
  entropy_symbol: "OAI"
  hedge_symbols: "OPENAI, OAI"
thresholds:
  midline_bps: -163
  upper_bps: 5
  lower_bps: 5
entropy:
  taker_fee_bps: 0
  max_position_usd: 150      # лимит позиции, $
hedge:
  taker_fee_bps: 0
  max_position_usd: 150      # лимит позиции, $
sizing:
  max_order_notional_usd: 40   # максимум на одну сделку, $
  min_order_notional_usd: 10
recorder:
  csv: logs/minutes_lighter-rh_OAI.csv
"""


def test_new_pair_files_have_no_limits(club_dir):
    text = open(club_dir.pair_path((CORE, "NBIS"))).read()
    assert "max_position_usd" not in text
    assert "max_order_notional_usd" not in text


def test_old_pair_limits_move_to_config_yaml(tmp_path, monkeypatch):
    import club
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(BASE)
    d = tmp_path / "tickers" / RH
    d.mkdir(parents=True)
    (d / "OAI.yaml").write_text(OLD_PAIR)
    (d / "ANTH.yaml").write_text(OLD_PAIR.replace("150", "60")
                                 .replace("OAI", "ANTH"))
    (tmp_path / "tickers" / ".current").write_text(f"{RH}/OAI\n")
    monkeypatch.setattr(club, "find_bots", lambda: [])
    club.ensure_ticker_files()
    base = yaml.safe_load(open("config.yaml"))
    # the current pair's limits win and now apply to every pair
    assert base["entropy"]["max_position_usd"] == 150
    assert base["hedge"]["max_position_usd"] == 150
    assert base["sizing"]["max_order_notional_usd"] == 40
    for t in ("OAI", "ANTH"):
        text = (d / f"{t}.yaml").read_text()
        assert "max_position_usd" not in text
        assert "max_order_notional_usd" not in text
        v = club.cfg_values((RH, t))
        assert (v["pos_entropy"], v["pos_hedge"], v["order"]) == (150, 150, 40)
    assert club.cfg_values((RH, "OAI"))["midline_bps"] == -163


def test_limits_saved_for_all_pairs(club_dir):
    club = club_dir
    club.set_current_pair((RH, "ANTH"))
    club.save_config([("entropy", "max_position_usd", "200"),
                      ("hedge", "max_position_usd", "200"),
                      ("sizing", "max_order_notional_usd", "30")])
    for pair in ((RH, "ANTH"), (CORE, "SNDK")):
        v = club.cfg_values(pair)
        assert (v["pos_entropy"], v["order"]) == (200, 30)
    assert "max_position_usd" not in open(club.pair_path((RH, "ANTH"))).read()


def test_nickname(club_dir):
    club = club_dir
    assert club.bot_nickname() == ""
    club.save_config([("bot", "nickname", '"bot 1 OAI"')])
    assert club.bot_nickname() == "bot 1 OAI"
    from entropy_arb.config import load_config
    cfg = load_config("config.yaml", "/nonexistent-env", symbol="SNDK",
                      hedge_venue=RH, record_only=True)
    assert cfg.nickname == "bot 1 OAI"


def test_ticks_recording_turned_back_on(club_dir):
    club = club_dir
    with open("config.yaml", "a") as fh:
        fh.write("  ticks_sec: 0\n")       # appended to the recorder section
    club._ensure_ticks_on()
    assert yaml.safe_load(open("config.yaml"))["recorder"]["ticks_sec"] == 2.0


def test_trade_xyz_gone(club_dir):
    club = club_dir
    assert "tradexyz" not in club.VENUES
    (open(os.path.join("tickers", ".current"), "w")
     .write("tradexyz/OAI\n"))
    os.makedirs(os.path.join("tickers", "tradexyz"), exist_ok=True)
    open(os.path.join("tickers", "tradexyz", "OAI.yaml"), "w").write("x: 1\n")
    assert club.current_pair()[0] in club.VENUES


def test_main_menu_order():
    import inspect
    import club
    src = inspect.getsource(club.main)
    assert '("1", "Дашборд"), ("2", "Старт"), ("3", "Стоп")' in src
