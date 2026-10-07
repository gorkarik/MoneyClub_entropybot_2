"""Configuration: strategy from a YAML file, credentials from .env, market
selection (symbol + hedge venue) from the command line.

The split is deliberate: config.yaml IS the strategy (thresholds, sizing,
risk) and is safe to share/commit as an example; .env holds only secrets;
which markets to trade is stated explicitly on every start (--symbol,
--hedge). Every YAML key is validated against the schema below, so a typo
is an error rather than a setting that silently does nothing.

Threshold model (fixed numbers the user derives from recorded minute data):

    premium_bps = (entropy_price / hedge_price - 1) * 10_000

    SELL entropy / BUY hedge  fires when the executable premium
        (entropy bid over hedge ask) >= midline_bps + upper_bps
    BUY entropy / SELL hedge  fires when the executable premium
        (entropy ask under hedge bid) <= midline_bps - lower_bps

    Both hurdles are net of both venues' taker fees, so a full round trip
    nets >= (upper_bps + lower_bps) after fees by construction.

Ticker profiles (MONEY CLUB). When a `tickers/` directory sits next to
config.yaml, each ticker has its own file tickers/<TICKER>.yaml that
overrides the per-market parts of config.yaml: thresholds, fees, position
caps, order sizes and the minute-data file — plus the market's name on each
venue (venues may list the same asset under different names). Thresholds are
NEVER inherited from config.yaml in that mode: they belong to one market. A
ticker whose midline_bps is 0 is not calibrated yet and is refused for live
trading (test recording is allowed). Without a tickers/ directory the
original single-file behaviour is unchanged.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import yaml
from dotenv import load_dotenv

HL_API_URL = "https://api.hyperliquid.xyz"
HL_WS_URL = "wss://api.hyperliquid.xyz/ws"   # official ws — the only HL feed used

HEDGE_VENUES = ("lighter", "lighter-rh", "tradexyz")

# .env name prefix of each Lighter exchange's trading key
LIGHTER_ENV_PREFIX = {"lighter-rh": "LIGHTER_", "lighter": "LIGHTER_CORE_"}

TICKERS_DIR = "tickers"


@dataclass(frozen=True)
class LighterProfile:
    name: str
    api_url: str
    ws_url: str
    chain_id: int


# Endpoint profiles for the two supported zkLighter deployments (these match
# lighter-python's lighter.endpoint_profiles, duplicated here so --record-only
# data collection works without the SDK installed).
LIGHTER_PROFILES: Dict[str, LighterProfile] = {
    "lighter": LighterProfile(
        "mainnet", "https://mainnet.zklighter.elliot.ai",
        "wss://mainnet.zklighter.elliot.ai/stream", 304),
    "lighter-rh": LighterProfile(
        "robinhood", "https://api.rh.lighter.xyz",
        "wss://api.rh.lighter.xyz/stream", 466324),
}


@dataclass
class LighterCreds:
    account_index: Optional[int]
    api_key_index: Optional[int]
    api_private_key: Optional[str]

    @property
    def complete(self) -> bool:
        return (self.account_index is not None and self.api_key_index is not None
                and bool(self.api_private_key))


@dataclass
class HLCreds:
    private_key: Optional[str]
    account_address: Optional[str]

    @property
    def complete(self) -> bool:
        return bool(self.private_key)


@dataclass
class VenueConf:
    key: str                  # "entropy" | "hedge"
    kind: str                 # "hl" | "lighter"
    label: str                # human name for logs, e.g. "ENTROPY", "RH"
    symbol: str               # resolved market name on this venue
    fee_bps: float
    cap_usd: float
    orders_per_min: int
    # names to try on this venue, first one listed wins (load_market sets
    # `symbol` to the one found); empty = just `symbol`
    symbol_aliases: Tuple[str, ...] = ()
    # hl
    hl_dex: str = ""
    hl_creds: Optional[HLCreds] = None
    # lighter
    lighter_profile: Optional[LighterProfile] = None
    lighter_creds: Optional[LighterCreds] = None


@dataclass
class Config:
    symbol: str
    hedge_venue: str
    entropy: VenueConf
    hedge: VenueConf
    # thresholds (the whole signal)
    midline_bps: float
    upper_bps: float
    lower_bps: float
    # sizing
    take_fraction: float
    max_order_notional: float
    min_order_notional: float
    # inventory ladder
    inventory_scale_bps: float
    inventory_floor_frac: float
    # execution
    premium_persist_sec: float
    cooldown_sec: float
    settle_timeout_sec: float
    leg_slippage_bps: float
    hedge_slippage_bps: float
    net_tolerance_base: float
    max_consecutive_errors: int
    rate_limit_pause_sec: float
    staleness_sec: float
    reconcile_sec: float
    venue_probe_sec: float
    http_keepalive_sec: float
    # recorder
    recorder_enabled: bool
    recorder_csv: str
    # logging
    log_level: str
    status_interval_sec: float
    trades_csv: str
    dashboard: bool
    log_file: str
    # loss limit per session (risk section; every key optional — an existing
    # config.yaml without it gets these defaults instead of a start-up error).
    # 0 = off: the user decides whether to set a limit at all.
    max_loss_pct: float = 0.0
    risk_check_sec: float = 15.0
    risk_confirm_interval_sec: float = 10.0
    risk_max_equity_misses: int = 5
    risk_max_read_skew_sec: float = 3.0
    flatten_slippage_bps: float = 100.0
    flatten_attempts: int = 3
    flatten_on_halt: bool = False
    # Telegram (optional, from .env): own bot token + the owner's chat id
    telegram_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    # ticker profile (tickers/<TICKER>.yaml); None = single-file mode
    ticker_file: Optional[str] = None
    # False = midline_bps is 0 in the ticker profile: not calibrated yet
    calibrated: bool = True
    # ticker profile says the Entropy fee was checked on a live trade
    # (None = single-file mode, nothing to say)
    fee_checked: Optional[bool] = None
    # hedge-venue fee confirmed on a live trade (None = nothing to say:
    # single-file mode, or a Lighter venue whose 0% is documented)
    hedge_fee_checked: Optional[bool] = None
    # fast book snapshots for the backtester: every ticks_sec seconds into
    # one file per pair and day under ticks_dir; 0 = off
    ticks_sec: float = 2.0
    ticks_dir: str = "logs/ticks"
    # auto-calibration of the midline (autocalib.py) — off unless the user
    # turns it on; anchor = midline last set by hand (None: the midline at
    # start); last_ts = when it last ran for this pair
    # strategy (execution mode), see strategy.py: "simultaneous" = both legs
    # at once; "entropy_first" = the Entropy IOC first with a tight price
    # limit, the hedge only for what filled (a miss costs nothing and leaves
    # no leg); "volume" = as entropy_first, band narrowed for more trades,
    # held to a price of volume
    exec_mode: str = "simultaneous"
    entropy_first_slip_bps: float = 5.0
    # strategy 3: entry band narrowed by this many bps on each side, and the
    # most a $10 000 of Entropy volume may cost (realized, dollars = bps)
    volume_narrow_bps: float = 1.0
    volume_max_cost_usd: float = 2.0
    # skip signals that beat their entry hurdle by more than this (a premium
    # too good to be real is usually a stale book); 0 = off
    max_excess_bps: float = 0.0
    # realized-slippage gate: opening trades must also clear 2 × the median
    # realized slippage of both legs (round trip), × weight; off by default
    slipgate_enabled: bool = False
    slipgate_weight: float = 1.0
    slipgate_lookback_hours: float = 48.0
    slipgate_min_fills: int = 5
    slipgate_max_samples: int = 50
    autocalib_enabled: bool = False
    autocalib_window_hours: float = 72.0
    autocalib_every_hours: float = 24.0
    autocalib_max_step_bps: float = 1.0
    autocalib_max_drift_bps: float = 3.0
    autocalib_min_hours: float = 18.0
    midline_anchor_bps: Optional[float] = None
    autocalib_last_ts: Optional[float] = None
    # runtime
    hl_api_url: str = HL_API_URL
    hl_ws_url: str = HL_WS_URL

    @property
    def creds_complete(self) -> bool:
        for v in (self.entropy, self.hedge):
            if v.kind == "hl" and not (v.hl_creds and v.hl_creds.complete):
                return False
            if v.kind == "lighter" and not (v.lighter_creds
                                            and v.lighter_creds.complete):
                return False
        return True


# ----------------------------------------------------------------- YAML layer

# Schema: nested dict of key -> type (or nested dict). Unknown keys are errors.
_SCHEMA: Dict[str, Any] = {
    "thresholds": {
        "midline_bps": float,
        "upper_bps": float,
        "lower_bps": float,
    },
    "entropy": {
        "dex": str,
        "taker_fee_bps": float,
        "max_position_usd": float,
        "max_orders_per_min": int,
    },
    "hedge": {
        "taker_fee_bps": float,
        "max_position_usd": float,
        "max_orders_per_min": int,
    },
    "sizing": {
        "take_fraction": float,
        "max_order_notional_usd": float,
        "min_order_notional_usd": float,
    },
    "inventory": {
        "scale_bps": float,
        "floor_frac": float,
    },
    "execution": {
        "premium_persist_sec": float,
        "cooldown_sec": float,
        "settle_timeout_sec": float,
        "leg_slippage_bps": float,
        "hedge_slippage_bps": float,
        "net_tolerance_base": float,
        "max_consecutive_errors": int,
        "rate_limit_pause_sec": float,
        "staleness_sec": float,
        "reconcile_sec": float,
        "venue_probe_sec": float,
        "http_keepalive_sec": float,
        # strategy choices (menu: Settings → Strategy)
        "mode": str,           # simultaneous | entropy_first | volume
        "entropy_first_slip_bps": float,
        "max_excess_bps": float,
        "volume_narrow_bps": float,
        "volume_max_cost_usd": float,
    },
    "slipgate": {
        "enabled": bool,
        "weight": float,
        "lookback_hours": float,
        "min_fills": int,
        "max_samples": int,
    },
    "risk": {
        "max_loss_pct": float,
        "check_sec": float,
        "confirm_interval_sec": float,
        "max_equity_misses": int,
        "max_read_skew_sec": float,
        "flatten_slippage_bps": float,
        "flatten_attempts": int,
        "flatten_on_halt": bool,
    },
    "autocalib": {
        "enabled": bool,
        "window_hours": float,
        "every_hours": float,
        "max_step_bps": float,
        "max_drift_bps": float,
        "min_hours": float,
    },
    "recorder": {
        "enabled": bool,
        "csv": str,
        "ticks_sec": float,
        "ticks_dir": str,
    },
    "logging": {
        "level": str,
        "status_interval_sec": float,
        "trades_csv": str,
        "dashboard": bool,
        "file": str,
    },
}


# A ticker profile may set only the per-market parts of the config.
_TICKER_SCHEMA: Dict[str, Any] = {
    "ticker": {
        "entropy_symbol": str,     # market name on Entropy (default: ticker)
        "hedge_symbols": str,      # names to try on the hedge venue, comma-separated
        "fee_checked": bool,       # Entropy fee confirmed on a live trade
        "hedge_fee_checked": bool,  # hedge-venue fee confirmed (trade.xyz)
        "midline_anchor_bps": float,  # midline last set by hand (autocalib leash)
        "autocalib_last_ts": float,   # when autocalib last ran for this pair
    },
    "thresholds": _SCHEMA["thresholds"],
    "entropy": {k: _SCHEMA["entropy"][k]
                for k in ("taker_fee_bps", "max_position_usd")},
    "hedge": {k: _SCHEMA["hedge"][k]
              for k in ("taker_fee_bps", "max_position_usd")},
    # take_fraction is an execution setting: common to all tickers
    "sizing": {k: _SCHEMA["sizing"][k]
               for k in ("max_order_notional_usd", "min_order_notional_usd")},
    "recorder": {"csv": str},
}


class ConfigError(ValueError):
    pass


def _validate(node: Any, schema: Dict[str, Any], path: str = "") -> None:
    if not isinstance(node, dict):
        raise ConfigError(f"'{path or '<root>'}' must be a mapping")
    for key, val in node.items():
        here = f"{path}.{key}" if path else str(key)
        if key not in schema:
            raise ConfigError(f"unknown config key '{here}' "
                              f"(valid: {', '.join(sorted(schema))})")
        want = schema[key]
        if isinstance(want, dict):
            if val is None:
                continue  # empty section (all lines commented out): defaults
            _validate(val, want, here)
        elif want is float:
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                raise ConfigError(f"'{here}' must be a number, got {val!r}")
        elif want is int:
            if not isinstance(val, int) or isinstance(val, bool):
                raise ConfigError(f"'{here}' must be an integer, got {val!r}")
        elif want is bool:
            if not isinstance(val, bool):
                raise ConfigError(f"'{here}' must be true/false, got {val!r}")
        elif want is str:
            if not isinstance(val, str):
                raise ConfigError(f"'{here}' must be a string, got {val!r}")


def _get(d: dict, section: str, key: str, default):
    return (d.get(section) or {}).get(key, default)


# ------------------------------------------------------------------ env layer

def _env_s(name: str) -> Optional[str]:
    v = os.getenv(name)
    return v.strip() if v not in (None, "") else None


def _env_i(name: str) -> Optional[int]:
    v = os.getenv(name)
    return int(v) if v not in (None, "") else None


# -------------------------------------------------------------------- loading

def split_names(text: Optional[str]) -> Tuple[str, ...]:
    """'ANTH, ANTHROPIC' -> ('ANTH', 'ANTHROPIC'); order kept, duplicates
    and blanks dropped."""
    out: List[str] = []
    for part in (text or "").split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return tuple(out)


def tickers_dir_for(config_file: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(config_file)),
                        TICKERS_DIR)


def ticker_file_for(config_file: str, symbol: str,
                    hedge_venue: str = "lighter-rh") -> str:
    """Settings of one pair: tickers/<hedge venue>/<TICKER>.yaml — the same
    ticker on two venues is two markets with their own premium."""
    return os.path.join(tickers_dir_for(config_file), hedge_venue,
                        f"{symbol}.yaml")


def read_ticker_profile(path: str) -> dict:
    """Load and validate one tickers/<TICKER>.yaml."""
    try:
        with open(path) as fh:
            prof = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        raise ConfigError(f"ticker file '{path}' not found / нет файла тикера")
    try:
        _validate(prof, _TICKER_SCHEMA)
    except ConfigError as e:
        raise ConfigError(f"{os.path.basename(path)}: {e}") from None
    return prof


def merge_ticker_profile(raw: dict, prof: dict) -> dict:
    """config.yaml with the ticker's per-market values on top. Thresholds are
    taken from the profile only — never inherited from config.yaml."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in raw.items()}
    out["thresholds"] = dict(prof.get("thresholds") or {})
    for sec, vals in prof.items():
        if sec in ("ticker", "thresholds") or not vals:
            continue
        merged = dict(out.get(sec) or {})
        merged.update(vals)
        out[sec] = merged
    return out


EXEC_MODES = ("simultaneous", "entropy_first", "volume")


def xyz_creds(record_only: bool = False) -> HLCreds:
    """Keys of the trade.xyz leg. Its own pair HL_PRIVATE_KEY_XYZ +
    HL_ACCOUNT_ADDRESS_XYZ (a separate wallet: its own Hyperliquid request
    budget) when set, otherwise the Entropy keys (one account, both dexes).
    Key and address always come as a pair — never one wallet's key with
    another wallet's address. An address without a key is refused for
    trading instead of being silently ignored."""
    key, addr = _env_s("HL_PRIVATE_KEY_XYZ"), _env_s("HL_ACCOUNT_ADDRESS_XYZ")
    if key:
        return HLCreds(key, addr)     # addr None = the key's own wallet
    if addr and not record_only:
        raise ConfigError(
            "HL_ACCOUNT_ADDRESS_XYZ is set without HL_PRIVATE_KEY_XYZ — fill "
            "both or clear both (Settings → Keys) / адрес trade.xyz задан без "
            "ключа: заполните оба или очистите оба (Настройки → Ключи)")
    return HLCreds(_env_s("HL_PRIVATE_KEY"), _env_s("HL_ACCOUNT_ADDRESS"))


def load_config(config_file: str = "config.yaml", env_file: str = ".env", *,
                symbol: str, hedge_venue: str,
                hedge_symbol: Optional[str] = None,
                record_only: bool = False,
                tickers_dir: Optional[str] = None) -> Config:
    """`hedge_symbol` overrides the hedge market name(s) (comma-separated);
    `record_only` relaxes the calibration requirement of ticker profiles
    (test recording runs no strategy, so it needs no thresholds)."""
    load_dotenv(env_file)
    try:
        with open(config_file) as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        raise ConfigError(
            f"config file '{config_file}' not found — copy config.example.yaml "
            f"to config.yaml and edit it / файл настроек не найден — "
            f"скопируйте config.example.yaml в config.yaml")
    _validate(raw, _SCHEMA)

    symbol = (symbol or "").strip()
    if not symbol:
        raise ConfigError("--symbol is required, e.g. --symbol SNDK / "
                          "укажите тикер через --symbol")
    if hedge_venue not in HEDGE_VENUES:
        raise ConfigError(
            f"--hedge must be one of {list(HEDGE_VENUES)}, got "
            f"{hedge_venue!r} / --hedge должен быть одним из {list(HEDGE_VENUES)}")

    # ---- ticker profile (tickers/<TICKER>.yaml), if the directory exists
    tdir = tickers_dir if tickers_dir is not None else \
        tickers_dir_for(config_file)
    ticker_file = None
    prof: dict = {}
    if os.path.isdir(tdir):
        ticker_file = os.path.join(tdir, hedge_venue, f"{symbol}.yaml")
        flat = os.path.join(tdir, f"{symbol}.yaml")
        if not os.path.exists(ticker_file) and hedge_venue == "lighter-rh" \
                and os.path.exists(flat):
            # layout of the previous version (one file per ticker, RH only);
            # the menu moves these into tickers/lighter-rh/ on its next start
            ticker_file = flat
        if not os.path.exists(ticker_file):
            raise ConfigError(
                f"no settings for {symbol} on {hedge_venue}: {ticker_file} is "
                f"missing — choose the pair in the menu (moneyclub) to create "
                f"it / нет настроек пары {symbol} · {hedge_venue} — выберите "
                f"её в меню")
        prof = read_ticker_profile(ticker_file)
        raw = merge_ticker_profile(raw, prof)
    tinfo = prof.get("ticker") or {}

    thr = raw.get("thresholds") or {}
    if ticker_file and record_only:
        # test recording: thresholds unused, a fresh ticker may have none
        thr = {"midline_bps": 0.0, "upper_bps": 1.0, "lower_bps": 1.0,
               **thr}
    for k in ("midline_bps", "upper_bps", "lower_bps"):
        if k not in thr:
            raise ConfigError(f"'thresholds.{k}' is required — derive it from "
                              f"recorded minute data / обязательное поле — "
                              f"рассчитайте по записанным данным")
    calibrated = True
    if ticker_file:
        calibrated = float(thr["midline_bps"]) != 0.0
        if not calibrated and not record_only:
            raise ConfigError(
                f"ticker {symbol} is not calibrated (midline_bps = 0 in "
                f"{os.path.basename(ticker_file)}): run a test recording, then "
                f"Analysis → calibration / тикер {symbol} не откалиброван: "
                f"сначала тестовая запись, затем Анализ → Калибровка")
    upper, lower = float(thr["upper_bps"]), float(thr["lower_bps"])
    if upper <= 0 or lower <= 0:
        raise ConfigError("thresholds.upper_bps and lower_bps must be > 0 "
                          "(the round trip nets upper+lower bps after fees)")

    take_fraction = float(_get(raw, "sizing", "take_fraction", 0.5))
    if not 0.0 < take_fraction <= 1.0:
        raise ConfigError("sizing.take_fraction must be in (0, 1] — taking "
                          "more than the profitable depth loses money on the "
                          "tail / должно быть в диапазоне (0, 1]")

    entropy_dex = _get(raw, "entropy", "dex", "io")
    if hedge_venue == "tradexyz" and entropy_dex == "xyz":
        raise ConfigError("entropy.dex 'xyz' with hedge_venue 'tradexyz' is "
                          "the same market on both legs / обе ноги — один и тот же рынок")

    entropy_names = split_names(tinfo.get("entropy_symbol")) or (symbol,)
    hedge_names = (split_names(hedge_symbol)
                   or split_names(tinfo.get("hedge_symbols")) or (symbol,))

    entropy_hl_creds = HLCreds(_env_s("HL_PRIVATE_KEY"),
                               _env_s("HL_ACCOUNT_ADDRESS"))
    entropy = VenueConf(
        key="entropy", kind="hl", label="ENTROPY",
        symbol=entropy_names[0], symbol_aliases=entropy_names,
        fee_bps=float(_get(raw, "entropy", "taker_fee_bps", 0.0)),
        cap_usd=float(_get(raw, "entropy", "max_position_usd", 1000.0)),
        orders_per_min=int(_get(raw, "entropy", "max_orders_per_min", 120)),
        hl_dex=entropy_dex,
        hl_creds=entropy_hl_creds,
    )

    if hedge_venue == "tradexyz":
        hedge = VenueConf(
            key="hedge", kind="hl", label="XYZ",
            symbol=hedge_names[0], symbol_aliases=hedge_names,
            fee_bps=float(_get(raw, "hedge", "taker_fee_bps", 1.0)),
            cap_usd=float(_get(raw, "hedge", "max_position_usd", 1000.0)),
            orders_per_min=int(_get(raw, "hedge", "max_orders_per_min", 120)),
            hl_dex="xyz",
            hl_creds=xyz_creds(record_only),
        )
    else:
        # Lighter core and Lighter RH are separate exchanges: accounts, API
        # keys, nonces and account indexes do not carry over, and a signature
        # for one is rejected by the other. RH keeps the LIGHTER_* names it
        # has always used here; core reads its own LIGHTER_CORE_* keys.
        env = LIGHTER_ENV_PREFIX[hedge_venue]
        hedge = VenueConf(
            key="hedge", kind="lighter",
            label="LIGHTER" if hedge_venue == "lighter" else "RH",
            symbol=hedge_names[0], symbol_aliases=hedge_names,
            fee_bps=float(_get(raw, "hedge", "taker_fee_bps", 0.0)),
            cap_usd=float(_get(raw, "hedge", "max_position_usd", 1000.0)),
            orders_per_min=int(_get(raw, "hedge", "max_orders_per_min", 30)),
            lighter_profile=LIGHTER_PROFILES[hedge_venue],
            lighter_creds=LighterCreds(_env_i(f"{env}ACCOUNT_INDEX"),
                                       _env_i(f"{env}API_KEY_INDEX"),
                                       _env_s(f"{env}API_PRIVATE_KEY")),
        )

    # minute data: one file per ticker in profile mode — never the shared
    # config.yaml path, so different markets never mix in one file
    if ticker_file:
        recorder_csv = ((prof.get("recorder") or {}).get("csv")
                        or f"logs/minutes_{hedge_venue}_{symbol}.csv")
    else:
        recorder_csv = _get(raw, "recorder", "csv", "logs/minutes.csv")

    # execution settings the menu exposes: reject values the engine cannot
    # use sensibly instead of trading on them
    ex_checks = (
        ("execution", "premium_persist_sec", 0.3, 0.0, 60.0, True),
        ("execution", "cooldown_sec", 0.0, 0.0, 3600.0, True),
        ("execution", "leg_slippage_bps", 50.0, 0.0, 1000.0, False),
        ("execution", "hedge_slippage_bps", 20.0, 0.0, 1000.0, False),
        ("execution", "staleness_sec", 10.0, 0.0, 600.0, False),
        ("inventory", "scale_bps", 10.0, 0.0, 1000.0, True),
    )
    for sec, key, dflt, lo, hi, lo_ok in ex_checks:
        val = float(_get(raw, sec, key, dflt))
        if not ((val >= lo if lo_ok else val > lo) and val <= hi):
            raise ConfigError(f"'{sec}.{key}' = {val:g} is out of range "
                              f"({'>=' if lo_ok else '>'} {lo:g} and <= {hi:g}) "
                              f"/ значение вне допустимого диапазона")
    floor_frac = float(_get(raw, "inventory", "floor_frac", 0.5))
    if not 0.0 <= floor_frac < 1.0:
        raise ConfigError("inventory.floor_frac must be in [0, 1) — the share "
                          "of the position cap where the surcharge starts / "
                          "должно быть от 0 до 1 (не включая 1)")

    ticks_sec = float(_get(raw, "recorder", "ticks_sec", 2.0))
    if not (ticks_sec == 0.0 or 1.0 <= ticks_sec <= 60.0):
        raise ConfigError("recorder.ticks_sec must be 0 (off) or 1..60 seconds "
                          "/ частый срез: 0 — выключен, иначе от 1 до 60 с")

    ac = {k: float(_get(raw, "autocalib", k, d)) for k, d in (
        ("window_hours", 72.0), ("every_hours", 24.0), ("max_step_bps", 1.0),
        ("max_drift_bps", 3.0), ("min_hours", 18.0))}
    if not (6.0 <= ac["window_hours"] <= 720.0
            and 1.0 <= ac["every_hours"] <= 168.0
            and 0.1 <= ac["max_step_bps"] <= 5.0
            and 0.5 <= ac["max_drift_bps"] <= 20.0
            and 1.0 <= ac["min_hours"] <= ac["window_hours"]):
        raise ConfigError("autocalib: window_hours 6..720, every_hours 1..168, "
                          "max_step_bps 0.1..5, max_drift_bps 0.5..20, "
                          "min_hours 1..window_hours / автокалибровка: "
                          "значение вне допустимого диапазона")

    exec_mode = str(_get(raw, "execution", "mode", "simultaneous")).strip()
    if exec_mode not in EXEC_MODES:
        raise ConfigError(f"execution.mode must be one of {list(EXEC_MODES)} "
                          f"/ стратегия: simultaneous, entropy_first или volume")
    ef_slip = float(_get(raw, "execution", "entropy_first_slip_bps", 5.0))
    max_excess = float(_get(raw, "execution", "max_excess_bps", 0.0))
    if not 0.5 <= ef_slip <= 50.0:
        raise ConfigError("execution.entropy_first_slip_bps must be 0.5..50 / "
                          "предел цены Entropy: от 0.5 до 50 bps")
    if max_excess < 0:
        raise ConfigError("execution.max_excess_bps must be >= 0 (0 = off)")
    vol_narrow = float(_get(raw, "execution", "volume_narrow_bps", 1.0))
    vol_cost = float(_get(raw, "execution", "volume_max_cost_usd", 2.0))
    if not 0.0 <= vol_narrow <= 20.0:
        raise ConfigError("execution.volume_narrow_bps must be 0..20 / "
                          "сужение полосы для объёма: от 0 до 20 bps")
    if exec_mode == "volume" and vol_narrow >= min(upper, lower):
        raise ConfigError(
            f"execution.volume_narrow_bps ({vol_narrow:g}) must be smaller than "
            f"upper_bps and lower_bps of this pair / сужение полосы должно "
            f"быть меньше upper и lower пары")
    if not 0.0 <= vol_cost <= 100.0:
        raise ConfigError("execution.volume_max_cost_usd must be 0..100 / "
                          "цена объёма: от 0 до 100 $ за $10 000")
    sg = {k: _get(raw, "slipgate", k, d) for k, d in (
        ("weight", 1.0), ("lookback_hours", 48.0), ("min_fills", 5),
        ("max_samples", 50))}
    if not (0.0 < float(sg["weight"]) <= 3.0
            and 1.0 <= float(sg["lookback_hours"]) <= 720.0
            and 1 <= int(sg["min_fills"]) <= int(sg["max_samples"]) <= 1000):
        raise ConfigError("slipgate: weight 0..3, lookback_hours 1..720, "
                          "1 <= min_fills <= max_samples <= 1000")

    max_loss_pct = float(_get(raw, "risk", "max_loss_pct", 0.0))
    if not 0.0 <= max_loss_pct < 100.0:
        raise ConfigError("risk.max_loss_pct must be in [0, 100) — percent of "
                          "starting equity, 0 = off / в процентах, 0 — выключено")
    risk_check_sec = float(_get(raw, "risk", "check_sec", 15.0))
    confirm_sec = float(_get(raw, "risk", "confirm_interval_sec", 10.0))
    misses = int(_get(raw, "risk", "max_equity_misses", 5))
    skew = float(_get(raw, "risk", "max_read_skew_sec", 3.0))
    fl_slip = float(_get(raw, "risk", "flatten_slippage_bps", 100.0))
    fl_attempts = int(_get(raw, "risk", "flatten_attempts", 3))
    if risk_check_sec < 1.0 or confirm_sec < 1.0 or skew <= 0:
        raise ConfigError("risk.check_sec / confirm_interval_sec must be >= 1 "
                          "and max_read_skew_sec > 0")
    if misses < 1 or fl_attempts < 1 or fl_slip <= 0:
        raise ConfigError("risk.max_equity_misses and flatten_attempts must be "
                          ">= 1, flatten_slippage_bps > 0")

    return Config(
        symbol=symbol,
        hedge_venue=hedge_venue,
        entropy=entropy,
        hedge=hedge,
        midline_bps=float(thr["midline_bps"]),
        upper_bps=upper,
        lower_bps=lower,
        take_fraction=take_fraction,
        max_order_notional=float(_get(raw, "sizing", "max_order_notional_usd", 500.0)),
        min_order_notional=float(_get(raw, "sizing", "min_order_notional_usd", 10.0)),
        inventory_scale_bps=float(_get(raw, "inventory", "scale_bps", 10.0)),
        inventory_floor_frac=float(_get(raw, "inventory", "floor_frac", 0.5)),
        premium_persist_sec=float(_get(raw, "execution", "premium_persist_sec", 0.3)),
        cooldown_sec=float(_get(raw, "execution", "cooldown_sec", 0.0)),
        settle_timeout_sec=float(_get(raw, "execution", "settle_timeout_sec", 5.0)),
        leg_slippage_bps=float(_get(raw, "execution", "leg_slippage_bps", 50.0)),
        hedge_slippage_bps=float(_get(raw, "execution", "hedge_slippage_bps", 20.0)),
        net_tolerance_base=float(_get(raw, "execution", "net_tolerance_base", 0.001)),
        max_consecutive_errors=int(_get(raw, "execution", "max_consecutive_errors", 3)),
        rate_limit_pause_sec=float(_get(raw, "execution", "rate_limit_pause_sec", 10.0)),
        staleness_sec=float(_get(raw, "execution", "staleness_sec", 10.0)),
        reconcile_sec=float(_get(raw, "execution", "reconcile_sec", 15.0)),
        venue_probe_sec=float(_get(raw, "execution", "venue_probe_sec", 30.0)),
        http_keepalive_sec=float(_get(raw, "execution", "http_keepalive_sec", 10.0)),
        recorder_enabled=bool(_get(raw, "recorder", "enabled", True)),
        recorder_csv=recorder_csv,
        log_level=str(_get(raw, "logging", "level", "INFO")).upper(),
        status_interval_sec=float(_get(raw, "logging", "status_interval_sec", 30.0)),
        trades_csv=_get(raw, "logging", "trades_csv", "logs/trades.csv"),
        dashboard=bool(_get(raw, "logging", "dashboard", True)),
        log_file=_get(raw, "logging", "file", "logs/engine.log"),
        max_loss_pct=max_loss_pct,
        risk_check_sec=risk_check_sec,
        risk_confirm_interval_sec=confirm_sec,
        risk_max_equity_misses=misses,
        risk_max_read_skew_sec=skew,
        flatten_slippage_bps=fl_slip,
        flatten_attempts=fl_attempts,
        flatten_on_halt=bool(_get(raw, "risk", "flatten_on_halt", False)),
        telegram_token=_env_s("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env_s("TELEGRAM_CHAT_ID"),
        ticker_file=ticker_file,
        calibrated=calibrated,
        fee_checked=(bool(tinfo.get("fee_checked", False)) if ticker_file
                     else None),
        hedge_fee_checked=(bool(tinfo.get("hedge_fee_checked", False))
                           if ticker_file and hedge_venue == "tradexyz"
                           else None),
        autocalib_enabled=bool(_get(raw, "autocalib", "enabled", False)),
        autocalib_window_hours=ac["window_hours"],
        autocalib_every_hours=ac["every_hours"],
        autocalib_max_step_bps=ac["max_step_bps"],
        autocalib_max_drift_bps=ac["max_drift_bps"],
        autocalib_min_hours=ac["min_hours"],
        midline_anchor_bps=(float(tinfo["midline_anchor_bps"])
                            if tinfo.get("midline_anchor_bps") is not None
                            else None),
        autocalib_last_ts=(float(tinfo["autocalib_last_ts"])
                           if tinfo.get("autocalib_last_ts") is not None
                           else None),
        exec_mode=exec_mode,
        entropy_first_slip_bps=ef_slip,
        volume_narrow_bps=vol_narrow,
        volume_max_cost_usd=vol_cost,
        max_excess_bps=max_excess,
        slipgate_enabled=bool(_get(raw, "slipgate", "enabled", False)),
        slipgate_weight=float(sg["weight"]),
        slipgate_lookback_hours=float(sg["lookback_hours"]),
        slipgate_min_fills=int(sg["min_fills"]),
        slipgate_max_samples=int(sg["max_samples"]),
        ticks_sec=ticks_sec,
        ticks_dir=str(_get(raw, "recorder", "ticks_dir", "logs/ticks")),
    )
