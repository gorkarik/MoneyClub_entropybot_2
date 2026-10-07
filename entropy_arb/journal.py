"""CSV journals for later analysis (tools/report.py) and the menu.

All files live next to trades.csv (logs/ by default):

  trades.csv    one row per arbitrage execution (both legs)
  hedges.csv    one row per single-leg order: net-delta hedges and the
                emergency / stop-time closes (flatten)
  equity.csv    both venues' equity about once a minute while trading
  sessions.csv  one row per session (Start .. Stop) with its result
  session_open.json  exists only while a live session runs; left behind
                when a session ended abnormally (crash, kill)

Appending is the only write. A file whose header is an older prefix of the
current one (new columns were added at the end) is upgraded in place, so
earlier rows stay in the same file; any other header mismatch moves the old
file aside with a timestamp instead of overwriting it.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import time
from typing import Dict, List, Optional

log = logging.getLogger("journal")

TRADES_HEADER = [
    # original columns (unchanged order)
    "ts", "direction", "buy_venue", "sell_venue", "qty",
    "buy_limit", "sell_limit", "buy_notional", "sell_notional",
    "exp_edge_usd", "gross_edge_usd", "marginal_premium_bps",
    "midline_bps", "inv_add_bps", "ok", "buy_fill", "sell_fill",
    "buy_status", "sell_status", "fill_edge_usd",
    # added in MONEY CLUB: what slippage / fee / latency analysis needs
    "buy_avg_px", "sell_avg_px",        # actual average fill prices
    "buy_exp_px", "sell_exp_px",        # planned average prices (book walk)
    "buy_fee_bps", "sell_fee_bps",      # fee rates in force for this trade
    "buy_ms", "sell_ms", "leg_gap_ms",  # leg latencies, gap between legs
    "buy_spread_bps", "sell_spread_bps",  # top-of-book spread at decision
    "session_id",
    # added with ticker selection: which market (menu ticker; hedge-venue
    # name when it differs). Empty in older rows = SNDK, the only ticker then.
    "symbol", "hedge_symbol",
    # added with Lighter core: which hedge venue. Empty in older rows =
    # lighter-rh, the only venue the menu traded before.
    "hedge_venue",
    # added with strategies: open = the trade adds to (or starts) the Entropy
    # position, close = reduces it; strategy = execution.mode in force
    "action", "strategy",
]
LEGACY_TRADES_HEADER = TRADES_HEADER[:20]

HEDGES_HEADER = ["ts", "session_id", "reason", "venue", "side", "qty",
                 "filled", "avg_px", "limit_px", "status", "err", "symbol",
                 "hedge_venue"]

EQUITY_HEADER = ["ts", "session_id", "entropy_equity", "hedge_equity",
                 "total_equity", "session_pnl", "entropy_pos", "hedge_pos",
                 "symbol", "hedge_venue"]

SESSIONS_HEADER = [
    "session_id", "start_ts", "end_ts", "duration_sec", "symbol",
    "entropy_venue", "hedge_venue", "start_equity", "end_equity",
    "pnl_usd", "pnl_pct", "turnover_usd", "trades", "hedges",
    "fees_est_usd", "pnl_bps_of_turnover", "stop_reason",
    "positions_closed", "max_loss_pct", "entropy_fee_bps", "hedge_fee_bps",
    "midline_bps", "upper_bps", "lower_bps", "hedge_symbol",
    # unhedged remainder: largest $ during the session; $ left after the
    # Stop close (below the venues' minimum order — close by hand)
    "max_unhedged_usd", "dust_left_usd",
    # funding paid (-) / received (+) over the session, per leg, USD; empty =
    # could not be read. Already inside pnl_usd (that is a balance change).
    "funding_entropy_usd", "funding_hedge_usd",
    # arbitrage trades of the session: edge expected at the decision vs
    # realized from the fills (the gap = slippage and latency)
    "exp_edge_usd", "fill_edge_usd",
    # execution.mode in force (simultaneous | entropy_first | volume)
    "strategy",
]


def journal_dir(trades_csv: str) -> str:
    return os.path.dirname(trades_csv) or "."


def path_in(trades_csv: str, name: str) -> str:
    return os.path.join(journal_dir(trades_csv), name)


def _read_header(path: str) -> Optional[List[str]]:
    try:
        with open(path, newline="") as fh:
            row = next(csv.reader(fh), None)
    except FileNotFoundError:
        return None
    return row or []


def ensure_header(path: str, header: List[str]) -> None:
    """Make `path` carry exactly `header`, keeping existing rows.

    * missing file: nothing to do (append_row writes the header);
    * header is a prefix of the new one: rewrite once, padding old rows
      with empty cells for the new columns;
    * anything else: move aside as <name>.old-<timestamp> (never clobber).
    """
    have = _read_header(path)
    if have is None or have == header:
        return
    if have and have == header[:len(have)]:
        tmp = path + ".upgrade"
        with open(path, newline="") as src, open(tmp, "w", newline="") as dst:
            r, w = csv.reader(src), csv.writer(dst)
            next(r, None)
            w.writerow(header)
            pad = [""] * (len(header) - len(have))
            for row in r:
                w.writerow(row + pad)
        os.replace(tmp, path)
        log.info("%s: upgraded to %d columns, old rows kept", path,
                 len(header))
        return
    aside = f"{path}.old-{time.strftime('%Y%m%d-%H%M%S')}"
    os.replace(path, aside)
    log.warning("%s: unexpected header — moved to %s", path, aside)


def append_row(path: str, header: List[str], row: List) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    ensure_header(path, header)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(header)
        w.writerow(row)


def read_rows(path: str) -> List[Dict[str, str]]:
    """All rows as dicts (missing columns of legacy rows read as '')."""
    try:
        with open(path, newline="") as fh:
            return [dict(r) for r in csv.DictReader(fh)]
    except FileNotFoundError:
        return []


def last_session(trades_csv: str) -> Optional[Dict[str, str]]:
    rows = read_rows(path_in(trades_csv, "sessions.csv"))
    return rows[-1] if rows else None


# --------------------------------------------- open-session marker (crash)

def marker_path(trades_csv: str) -> str:
    return path_in(trades_csv, "session_open.json")


def write_marker(trades_csv: str, info: dict) -> None:
    p = marker_path(trades_csv)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(info, fh, ensure_ascii=False)
    os.replace(tmp, p)


def read_marker(trades_csv: str) -> Optional[dict]:
    try:
        with open(marker_path(trades_csv), encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except Exception:
        return {"session_id": "?"}


def clear_marker(trades_csv: str) -> None:
    try:
        os.remove(marker_path(trades_csv))
    except FileNotFoundError:
        pass


def fmt(x, nd=4) -> str:
    if x is None:
        return ""
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)
